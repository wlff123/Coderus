import httpx
import pytest

from coderus.model_proxy.broker import CredentialBroker
from coderus.model_proxy.proxy import create_proxy_app


@pytest.mark.asyncio
async def test_error_details_sanitized_isolated_and_cleared():
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="one", stage="review")
    other = broker.issue(task_id="two", stage="review")
    responses = [httpx.Response(500, json={"error": {"message":
        f"connection refused real-key {token} https://private.test/?key=hidden"}}),
        httpx.Response(200, json={"id": "ok"})]
    app = create_proxy_app(broker, "https://example.test", "real-key",
                           transport=httpx.MockTransport(lambda request: responses.pop(0)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        response = await client.post("/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "test-model", "input": "hi"})
        message = response.json()["error"]["message"]
        assert "connection refused" in message
        for secret in ("real-key", token, "private.test", "hidden"):
            assert secret not in message
        assert broker.take_upstream_error(other) is None
        assert broker.take_upstream_error(token) == message
        assert broker.take_upstream_error(token) is None
        broker.set_upstream_error(token, "stale")
        await client.post("/v1/responses", headers={"Authorization": f"Bearer {token}"},
                          json={"model": "test-model", "input": "hi"})
        assert broker.take_upstream_error(token) is None
    broker.set_upstream_error(token, "old")
    broker.revoke(token)
    assert broker.take_upstream_error(token) is None
