from pathlib import Path

import pytest
from pydantic import ValidationError

from coderus.config import Settings, load_settings

SECRETS = {
    "CODERUS_SESSION_SECRET": "test-session-secret-that-is-long-enough",
    "CODERUS_BOOTSTRAP_ADMIN_PASSWORD": "initial-password",
}


def write_config(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_local_mode_uses_safe_defaults(tmp_path: Path) -> None:
    settings = load_settings(
        write_config(tmp_path / "config.yaml", "server:\n  mode: local\n"),
        SECRETS,
    )

    assert settings.server.bind == "127.0.0.1"
    assert settings.server.port == 18082
    assert settings.server.public_url is None
    assert settings.runner.network_access is True
    assert settings.scheduler.global_task_limit == 8
    assert settings.scheduler.per_user_task_limit == 2
    assert settings.scheduler.max_agent_processes == 16
    assert settings.codex.sandbox_mode == "workspace-write"
    assert settings.codex.auth_mode == "api_proxy"
    assert settings.assistant.enabled is True


def test_assistant_can_be_disabled() -> None:
    settings = Settings.model_validate({**SECRETS, "assistant": {"enabled": False}})

    assert settings.assistant.enabled is False


def test_codex_sandbox_mode_can_be_configured_for_container() -> None:
    settings = Settings.model_validate(
        {**SECRETS, "codex": {"sandbox_mode": "danger-full-access"}}
    )

    assert settings.codex.sandbox_mode == "danger-full-access"


@pytest.mark.parametrize("public_url", [None, "http://coderus.example.com"])
def test_public_mode_requires_https_url(public_url: str | None) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"server": {"mode": "public", "public_url": public_url}, **SECRETS})


def test_manager_never_binds_public_interface() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "server": {
                    "mode": "public",
                    "bind": "0.0.0.0",
                    "public_url": "https://coderus.example.com",
                },
                **SECRETS,
            }
        )


def test_local_mode_rejects_public_url() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "server": {
                    "mode": "local",
                    "public_url": "https://coderus.example.com",
                },
                **SECRETS,
            }
        )


def test_secrets_are_loaded_from_environment_and_hidden(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path / "config.yaml", "{}\n"), SECRETS)

    assert settings.session_secret.get_secret_value() == SECRETS["CODERUS_SESSION_SECRET"]
    assert "test-session-secret" not in repr(settings)
    assert "test-session-secret" not in str(settings.model_dump())


def test_loads_optional_credential_encryption_key(tmp_path: Path) -> None:
    settings = load_settings(
        write_config(tmp_path / "config.yaml", "{}\n"),
        {
            **SECRETS,
            "CODERUS_CREDENTIAL_ENCRYPTION_KEY": "key-value",
        },
    )

    assert settings.credential_encryption_key is not None
    assert settings.credential_encryption_key.get_secret_value() == "key-value"
    assert "key-value" not in repr(settings)


def test_model_api_key_requires_base_url_and_model() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**SECRETS, "CODERUS_MODEL_API_KEY": "real-key"}
        )

    settings = Settings.model_validate(
        {
            **SECRETS,
            "CODERUS_MODEL_API_KEY": "real-key",
            "codex": {"base_url": "https://models.example/v1", "model": "test-model"},
        }
    )
    assert settings.codex.model == "test-model"


def test_long_lived_codex_login_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="auth_mode"):
        Settings.model_validate(
            {
                **SECRETS,
                "codex": {
                    "auth_mode": "dedicated_login",
                },
            }
        )
