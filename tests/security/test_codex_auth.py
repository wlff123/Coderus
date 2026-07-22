from coderus.config import Settings
from coderus.security.codex_auth import inspect_codex_auth

SECRETS = {
    "CODERUS_SESSION_SECRET": "s" * 32,
    "CODERUS_BOOTSTRAP_ADMIN_PASSWORD": "password123",
}


def test_api_proxy_requires_model_key() -> None:
    unavailable = inspect_codex_auth(Settings(**SECRETS))
    ready = inspect_codex_auth(
        Settings(
            **SECRETS,
            CODERUS_MODEL_API_KEY="model-key",
            codex={"base_url": "https://models.example/v1", "model": "test-model"},
        )
    )

    assert unavailable.ready is False
    assert "CODERUS_MODEL_API_KEY" in unavailable.detail
    assert ready.ready is True
    assert ready.mode == "api_proxy"
    assert "短期 Token" in ready.detail
