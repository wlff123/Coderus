from __future__ import annotations

import httpx
import pytest

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
