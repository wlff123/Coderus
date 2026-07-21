import os
from base64 import urlsafe_b64encode

import pytest
from sqlalchemy import func, select

from coderus.integrations.feishu.settings import (
    FeishuSettingsManager,
    ensure_feishu_bot_user,
)
from coderus.models import FeishuBotSettings, User
from coderus.security.credentials import CredentialCipher, CredentialDecryptionError


def encryption_key() -> str:
    return urlsafe_b64encode(os.urandom(32)).decode()


def make_manager(key: str | None = None) -> FeishuSettingsManager:
    cipher = CredentialCipher.for_feishu_app_secret(key or encryption_key())
    return FeishuSettingsManager(cipher=cipher)


def add_admin(session) -> User:
    admin = User(username="admin", password_hash="hash", role="admin")
    session.add(admin)
    session.flush()
    return admin


def test_feishu_secret_uses_distinct_authenticated_context() -> None:
    key = encryption_key()
    github_cipher = CredentialCipher(key)
    feishu_cipher = CredentialCipher.for_feishu_app_secret(key)

    github_payload = github_cipher.encrypt("shared-secret")
    feishu_payload = feishu_cipher.encrypt("shared-secret")

    with pytest.raises(CredentialDecryptionError):
        github_cipher.decrypt(feishu_payload)
    with pytest.raises(CredentialDecryptionError):
        feishu_cipher.decrypt(github_payload)


def test_settings_are_encrypted_and_resolved_without_exposing_secret(session) -> None:
    manager = make_manager()
    admin = add_admin(session)

    prepared = manager.prepare(
        app_id=" cli_test ",
        app_secret="app-secret-value",
        default_chat_id=" oc_default ",
        enabled=True,
    )
    stored = manager.save(session, prepared, updated_by=admin)
    session.commit()

    assert stored.id == 1
    assert stored.app_id == "cli_test"
    assert stored.default_chat_id == "oc_default"
    assert stored.updated_by == admin.id
    assert stored.encrypted_app_secret.startswith("v1:")
    assert "app-secret-value" not in stored.encrypted_app_secret
    assert "app-secret-value" not in repr(prepared)

    resolved = manager.resolve(session)

    assert resolved.app_id == "cli_test"
    assert resolved.app_secret is not None
    assert resolved.app_secret.get_secret_value() == "app-secret-value"
    assert resolved.default_chat_id == "oc_default"
    assert resolved.enabled is True
    assert resolved.error is None
    assert "app-secret-value" not in repr(resolved)


def test_blank_secret_preserves_existing_ciphertext(session) -> None:
    manager = make_manager()
    admin = add_admin(session)
    original = manager.save(
        session,
        manager.prepare("cli_old", "old-secret", "oc_old", True),
        updated_by=admin,
    )
    session.flush()
    original_ciphertext = original.encrypted_app_secret

    updated = manager.save(
        session,
        manager.prepare("cli_new", "", "oc_new", True),
        updated_by=admin,
    )
    session.flush()

    assert updated.id == original.id
    assert updated.encrypted_app_secret == original_ciphertext
    assert session.scalar(select(func.count()).select_from(FeishuBotSettings)) == 1
    resolved = manager.resolve(session)
    assert resolved.app_id == "cli_new"
    assert resolved.app_secret is not None
    assert resolved.app_secret.get_secret_value() == "old-secret"
    assert resolved.default_chat_id == "oc_new"


def test_disabled_settings_do_not_require_credentials(session) -> None:
    manager = make_manager()
    admin = add_admin(session)

    manager.save(
        session,
        manager.prepare("", "", "", False),
        updated_by=admin,
    )
    session.flush()

    resolved = manager.resolve(session)
    assert resolved.app_id is None
    assert resolved.app_secret is None
    assert resolved.default_chat_id is None
    assert resolved.enabled is False
    assert resolved.error is None


def test_wrong_encryption_key_disables_resolved_settings(session) -> None:
    admin = add_admin(session)
    writer = make_manager()
    writer.save(
        session,
        writer.prepare("cli_test", "app-secret-value", "oc_default", True),
        updated_by=admin,
    )
    session.commit()

    resolved = make_manager().resolve(session)

    assert resolved.app_id == "cli_test"
    assert resolved.app_secret is None
    assert resolved.enabled is False
    assert resolved.error == "飞书 App Secret 无法解密"


def test_ensure_feishu_bot_user_is_idempotent_and_keeps_user_active(session) -> None:
    first = ensure_feishu_bot_user(session)
    session.flush()
    first_id = first.id
    first_password_hash = first.password_hash

    first.is_active = False
    session.flush()
    second = ensure_feishu_bot_user(session)
    session.flush()

    assert second.id == first_id
    assert second.username == "feishu-bot"
    assert second.role == "user"
    assert second.is_active is True
    assert second.password_hash == first_password_hash
    assert session.scalar(
        select(func.count()).select_from(User).where(User.username == "feishu-bot")
    ) == 1
