from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from coderus.model_proxy.broker import CredentialBroker, LeaseRejected

_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_MAX_REQUEST_BYTES = 10 * 1024 * 1024
_MAX_OUTPUT_TOKENS = 32_768
_MAX_TOOLS = 64
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=30, read=None, write=30, pool=30)
_TOOL_FIELDS = {
    "function": frozenset({"type", "name", "description", "parameters", "strict"}),
    "custom": frozenset({"type", "name", "description", "format"}),
    "local_shell": frozenset({"type"}),
}


class _RequestTooLarge(Exception):
    pass


def create_proxy_app(
    broker: CredentialBroker,
    upstream_base_url: str,
    upstream_api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create a local-only model API proxy; binding is the caller's responsibility."""
    if transport is not None and client is not None:
        raise ValueError("provide either transport or client, not both")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    upstream_root = upstream_base_url.rstrip("/")

    @app.post("/v1/responses")
    async def proxy_responses(request: Request):
        token = _bearer_token(request.headers.get("authorization"))
        if token is None or not broker.validate(token):
            return JSONResponse(
                {"detail": "Invalid or expired bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.url.query:
            return JSONResponse({"detail": "Endpoint is not permitted"}, status_code=403)

        content_length = _content_length(request.headers.get("content-length"))
        if content_length is None and request.headers.get("content-length") is not None:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        if content_length is not None and content_length > _MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        try:
            raw_body = await _read_request_body(request)
        except _RequestTooLarge:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        payload = _responses_payload(raw_body, request.headers.get("content-type"))
        if payload is None:
            return JSONResponse(
                {"detail": "Invalid Responses request"}, status_code=400
            )
        try:
            permit = broker.acquire(
                token,
                endpoint="/v1/responses",
                requested_model=payload["model"],
            )
        except LeaseRejected as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
            return JSONResponse(
                {"detail": str(exc)}, status_code=exc.status_code, headers=headers
            )

        if upstream_root.endswith("/v1"):
            url = f"{upstream_root}/responses"
        else:
            url = f"{upstream_root}/v1/responses"
        headers = _forward_headers(request.headers)
        headers["authorization"] = f"Bearer {upstream_api_key}"

        owned_client = client is None
        try:
            proxy_client = client or httpx.AsyncClient(
                transport=transport,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except BaseException as exc:
            permit.release()
            if not isinstance(exc, Exception):
                raise
            return JSONResponse({"detail": "Upstream request failed"}, status_code=502)
        try:
            upstream_request = proxy_client.build_request(
                "POST",
                url,
                headers=headers,
                content=raw_body,
            )
            upstream_response = await proxy_client.send(upstream_request, stream=True)
        except BaseException as exc:
            await _cleanup(permit, client=proxy_client if owned_client else None)
            if not isinstance(exc, Exception):
                raise
            return JSONResponse({"detail": "Upstream request failed"}, status_code=502)

        if upstream_response.is_error:
            status_code = upstream_response.status_code
            await _cleanup(
                permit,
                response=upstream_response,
                client=proxy_client if owned_client else None,
            )
            return JSONResponse({"detail": "Upstream request failed"}, status_code=status_code)

        response_headers = _forward_headers(upstream_response.headers)
        content_type = upstream_response.headers.get("content-type", "")
        if not content_type.lower().startswith("text/event-stream"):
            try:
                content = await _read_upstream_body(upstream_response, permit)
            except BaseException as exc:
                await _cleanup(
                    permit,
                    response=upstream_response,
                    client=proxy_client if owned_client else None,
                )
                if isinstance(exc, LeaseRejected):
                    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
                if not isinstance(exc, Exception):
                    raise
                return JSONResponse({"detail": "Upstream request failed"}, status_code=502)
            await _cleanup(
                permit,
                response=upstream_response,
                client=proxy_client if owned_client else None,
            )
            return Response(
                content=content,
                status_code=upstream_response.status_code,
                headers=response_headers,
            )

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_raw():
                    permit.record_output(len(chunk))
                    yield chunk
            except LeaseRejected:
                yield _output_limit_sse_error()
            finally:
                await _cleanup(
                    permit,
                    response=upstream_response,
                    client=proxy_client if owned_client else None,
                )

        return StreamingResponse(
            body(),
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    @app.post("/v1/{path:path}")
    async def reject_other_endpoint(path: str, request: Request):
        token = _bearer_token(request.headers.get("authorization"))
        if token is None or not broker.validate(token):
            return JSONResponse(
                {"detail": "Invalid or expired bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return JSONResponse({"detail": "Endpoint is not permitted"}, status_code=403)

    return app


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _forward_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "authorization"
    }


def _responses_payload(
    raw_body: bytes, content_type: str | None
) -> dict[str, object] | None:
    if len(raw_body) > _MAX_REQUEST_BYTES:
        return None
    if content_type is None or not content_type.lower().startswith("application/json"):
        return None
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("model"), str) or not payload["model"]:
        return None
    if not isinstance(payload.get("input"), (str, list)):
        return None
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return None
    max_output_tokens = payload.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or not 1 <= max_output_tokens <= _MAX_OUTPUT_TOKENS
    ):
        return None
    if payload.get("background", False) is not False:
        return None
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        return None
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in _TOOL_FIELDS:
            return None
        tool_type = tool["type"]
        if not set(tool).issubset(_TOOL_FIELDS[tool_type]):
            return None
        if tool_type != "local_shell" and (
            not isinstance(tool.get("name"), str) or not tool["name"]
        ):
            return None
    return payload


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def _read_request_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise _RequestTooLarge
        body.extend(chunk)
    return bytes(body)


async def _read_upstream_body(response: httpx.Response, permit) -> bytes:
    if response.is_stream_consumed:
        content = response.content
        permit.record_output(len(content))
        return content
    body = bytearray()
    if isinstance(response.stream, httpx.AsyncByteStream):
        async for chunk in response.aiter_raw():
            permit.record_output(len(chunk))
            body.extend(chunk)
    else:
        for chunk in response.iter_raw():
            permit.record_output(len(chunk))
            body.extend(chunk)
    return bytes(body)


def _output_limit_sse_error() -> bytes:
    payload = json.dumps(
        {
            "type": "error",
            "code": "output_limit_exceeded",
            "message": "Model proxy output limit exceeded",
            "param": None,
        },
        separators=(",", ":"),
    )
    return f"event: error\ndata: {payload}\n\ndata: [DONE]\n\n".encode()


async def _cleanup(
    permit,
    *,
    response: httpx.Response | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    permit.release()
    try:
        if response is not None:
            await response.aclose()
    finally:
        if client is not None:
            await client.aclose()
