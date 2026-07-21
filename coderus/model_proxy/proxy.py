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

        raw_body = await request.body()
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
        proxy_client = client or httpx.AsyncClient(transport=transport)
        try:
            upstream_request = proxy_client.build_request(
                "POST",
                url,
                headers=headers,
                content=raw_body,
            )
            upstream_response = await proxy_client.send(upstream_request, stream=True)
        except Exception:
            permit.release()
            if owned_client:
                await proxy_client.aclose()
            return JSONResponse({"detail": "Upstream request failed"}, status_code=502)

        if upstream_response.is_error:
            status_code = upstream_response.status_code
            await upstream_response.aclose()
            permit.release()
            if owned_client:
                await proxy_client.aclose()
            return JSONResponse({"detail": "Upstream request failed"}, status_code=status_code)

        response_headers = _forward_headers(upstream_response.headers)
        content_type = upstream_response.headers.get("content-type", "")
        if not content_type.lower().startswith("text/event-stream"):
            try:
                content = await upstream_response.aread()
            except Exception:
                await upstream_response.aclose()
                permit.release()
                if owned_client:
                    await proxy_client.aclose()
                return JSONResponse({"detail": "Upstream request failed"}, status_code=502)
            await upstream_response.aclose()
            permit.release()
            if owned_client:
                await proxy_client.aclose()
            return Response(
                content=content,
                status_code=upstream_response.status_code,
                headers=response_headers,
            )

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            except Exception:
                return
            finally:
                await upstream_response.aclose()
                permit.release()
                if owned_client:
                    await proxy_client.aclose()

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
    return payload
