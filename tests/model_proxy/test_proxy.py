from __future__ import annotations

import asyncio

import httpx
import pytest

import coderus.model_proxy.proxy as proxy_module
from coderus.model_proxy import CredentialBroker, create_proxy_app


def responses_payload(
    *, model: str = "test-model", stream: bool = False
) -> dict[str, object]:
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "controlled fixture"}],
            }
        ],
        "stream": stream,
        "include": [],
        "tools": [],
        "parallel_tool_calls": True,
        "store": False,
    }


class SSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"event: response.output_text.delta\n"
        yield b'data: {"delta":"hello"}\n\n'
        yield b"data: [DONE]\n\n"


class TrackedChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


@pytest.mark.asyncio
async def test_post_requires_valid_short_lived_bearer_token() -> None:
    broker = CredentialBroker(configured_model="test-model")
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example/api",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        missing = await client.post("/v1/responses", json=responses_payload())
        invalid = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer invalid-token"},
            json=responses_payload(),
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert calls == 0
    assert "invalid-token" not in invalid.text


@pytest.mark.asyncio
async def test_post_forwards_request_and_replaces_authorization() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    seen: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            url=str(request.url),
            authorization=request.headers["authorization"],
            marker=request.headers["x-request-marker"],
            body=await request.aread(),
        )
        return httpx.Response(200, headers={"x-upstream": "yes"}, json={"id": "resp_1"})

    app = create_proxy_app(
        broker,
        "https://models.example/api/",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}", "X-Request-Marker": "kept"},
            json=responses_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {"id": "resp_1"}
    assert response.headers["x-upstream"] == "yes"
    assert seen == {
        "method": "POST",
        "url": "https://models.example/api/v1/responses",
        "authorization": "Bearer real-secret-key",
        "marker": "kept",
        "body": httpx.Request("POST", "http://test", json=responses_payload()).content,
    }


@pytest.mark.asyncio
async def test_upstream_base_url_may_already_end_in_v1() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    seen_url = ""

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example/v1",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )

    assert response.status_code == 200
    assert seen_url == "https://models.example/v1/responses"


@pytest.mark.asyncio
async def test_sse_response_bytes_are_forwarded_unchanged() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSEStream(),
        )

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(stream=True),
        )

    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == (
        b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\ndata: [DONE]\n\n'
    )


@pytest.mark.asyncio
async def test_non_streaming_response_stops_reading_when_output_limit_is_reached() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_output_bytes=8
    )
    stream = TrackedChunks([b"12345", b"67890", b"must-not-be-read"])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )

    assert response.status_code == 429
    assert response.json() == {"detail": "Lease output limit reached"}
    assert stream.read_count == 2


@pytest.mark.asyncio
async def test_sse_output_limit_emits_error_event_and_terminal_marker() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_output_bytes=42
    )
    stream = TrackedChunks(
        [
            b"event: response.output_text.delta\n",
            b'data: {"delta":"hello"}\n\n',
            b"data: must-not-be-forwarded\n\n",
        ]
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(stream=True),
        )

    assert response.status_code == 200
    assert b"event: error\n" in response.content
    assert b'"code":"output_limit_exceeded"' in response.content
    assert response.content.endswith(b"data: [DONE]\n\n")
    assert b"must-not-be-forwarded" not in response.content


@pytest.mark.asyncio
async def test_upstream_failures_return_only_generic_details(caplog) -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    real_key = "real-secret-key"
    upstream_detail = "provider account disabled"

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=upstream_detail)

    app = create_proxy_app(
        broker,
        "https://models.example",
        real_key,
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )

    exposed = response.text + caplog.text
    assert response.status_code == 403
    assert response.json() == {"detail": "Upstream request failed"}
    assert real_key not in exposed
    assert token not in exposed
    assert upstream_detail not in exposed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/chat/completions", responses_payload()),
        ("/v1/responses?trace=1", responses_payload()),
        ("/v1/responses", responses_payload(model="other-model")),
        ("/v1/responses", {"model": "test-model", "stream": True}),
    ],
)
async def test_proxy_rejects_forbidden_endpoint_model_and_body_shape(
    path: str, payload: dict[str, object]
) -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    assert response.status_code in {400, 403}
    assert calls == 0


@pytest.mark.asyncio
async def test_proxy_enforces_request_limit() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_requests=1, max_concurrency=1
    )
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        first = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )
        exhausted = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )

    assert first.status_code == 200
    assert exhausted.status_code == 429
    assert calls == 1


@pytest.mark.asyncio
async def test_proxy_rejects_content_length_before_reading_body() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(10 * 1024 * 1024 + 1),
            },
            content=b"{}",
        )

    assert response.status_code == 413
    assert calls == 0


class RequestChunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"model":"test-model",'
        yield b'"input":"payload-too-large"}'


@pytest.mark.asyncio
async def test_proxy_stops_incremental_body_read_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_module, "_MAX_REQUEST_BYTES", 24)
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    app = create_proxy_app(broker, "https://models.example", "real-secret-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=RequestChunks(),
        )

    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": 32_769},
        {"background": True},
        {"tools": [{"type": "web_search_preview"}]},
        {"tools": [{"type": "function", "name": "safe", "url": "https://example.com"}]},
        {"tools": "not-a-list"},
    ],
)
async def test_proxy_rejects_unsafe_response_options(change: dict[str, object]) -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    payload = responses_payload()
    payload.update(change)
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    assert response.status_code == 400
    assert calls == 0


@pytest.mark.asyncio
async def test_cancelled_upstream_releases_broker_permit() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_requests=2, max_concurrency=1
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {token}"},
                json=responses_payload(),
            )

    permit = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )
    permit.release()


class CancellingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_cancelled_upstream_closes_owned_client() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    transport = CancellingTransport()
    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=transport,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {token}"},
                json=responses_payload(),
            )

    assert transport.closed is True


@pytest.mark.asyncio
async def test_client_construction_cancellation_releases_broker_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_requests=2, max_concurrency=1
    )
    app = create_proxy_app(broker, "https://models.example", "real-secret-key")

    client_options: dict[str, object] = {}

    def cancelled_client(*args, **kwargs):
        client_options.update(kwargs)
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        monkeypatch.setattr(proxy_module.httpx, "AsyncClient", cancelled_client)
        with pytest.raises(asyncio.CancelledError):
            await client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {token}"},
                json=responses_payload(),
            )

    permit = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )
    permit.release()
    timeout = client_options["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 30
    assert timeout.read is None
    assert timeout.write == 30
    assert timeout.pool == 30


@pytest.mark.asyncio
async def test_proxy_allows_codex_local_shell_tool_and_token_boundary() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    payload = responses_payload()
    payload.update(
        max_output_tokens=32_768,
        tools=[{"type": "local_shell"}],
    )
    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_proxy_returns_429_when_response_exceeds_output_budget() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_requests=2, max_output_bytes=4
    )
    app = create_proxy_app(
        broker,
        "https://models.example",
        "real-secret-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345")
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json=responses_payload(),
        )

    assert response.status_code == 429
