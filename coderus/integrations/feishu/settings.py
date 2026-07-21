from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.auth.security import hash_password
from coderus.models import FeishuBotSettings, User
from coderus.security.credentials import CredentialCipher, CredentialDecryptionError


class FeishuSettingsEncryptionUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class PreparedFeishuSettings:
    app_id: str | None
    app_secret: SecretStr | None = field(repr=False)
    encrypted_app_secret: str | None = field(repr=False)
    default_chat_id: str | None
    enabled: bool


@dataclass(frozen=True)
class ResolvedFeishuSettings:
    app_id: str | None
    app_secret: SecretStr | None = field(repr=False)
    default_chat_id: str | None
    enabled: bool
    updated_at: datetime | None = None
    error: str | None = None


class FeishuSettingsManager:
    def __init__(self, *, cipher: CredentialCipher | None) -> None:
        self._cipher = cipher

    def prepare(
        self,
        app_id: str,
        app_secret: str,
        default_chat_id: str,
        enabled: bool,
    ) -> PreparedFeishuSettings:
        normalized_app_id = app_id.strip() or None
        normalized_chat_id = default_chat_id.strip() or None
        secret = SecretStr(app_secret) if app_secret else None
        encrypted_secret = None
        if secret is not None:
            if self._cipher is None:
                raise FeishuSettingsEncryptionUnavailable("凭据加密密钥未配置")
            encrypted_secret = self._cipher.encrypt(secret.get_secret_value())
        return PreparedFeishuSettings(
            app_id=normalized_app_id,
            app_secret=secret,
            encrypted_app_secret=encrypted_secret,
            default_chat_id=normalized_chat_id,
            enabled=enabled,
        )

    def save(
        self,
        session: Session,
        prepared: PreparedFeishuSettings,
        *,
        updated_by: User,
    ) -> FeishuBotSettings:
        stored = session.get(FeishuBotSettings, 1)
        if stored is None:
            stored = FeishuBotSettings(
                id=1,
                app_id=prepared.app_id,
                encrypted_app_secret=prepared.encrypted_app_secret,
                default_chat_id=prepared.default_chat_id,
                enabled=prepared.enabled,
                updated_by=updated_by.id,
            )
            session.add(stored)
        else:
            stored.app_id = prepared.app_id
            if prepared.encrypted_app_secret is not None:
                stored.encrypted_app_secret = prepared.encrypted_app_secret
            stored.default_chat_id = prepared.default_chat_id
            stored.enabled = prepared.enabled
            stored.updated_by = updated_by.id
        session.flush()
        return stored

    def resolve(self, session: Session) -> ResolvedFeishuSettings:
        stored = session.get(FeishuBotSettings, 1)
        if stored is None:
            return ResolvedFeishuSettings(
                app_id=None,
                app_secret=None,
                default_chat_id=None,
                enabled=False,
            )

        secret = None
        if stored.encrypted_app_secret is not None:
            if self._cipher is None:
                return self._resolution_error(stored, "缺少凭据加密密钥")
            try:
                secret = SecretStr(self._cipher.decrypt(stored.encrypted_app_secret))
            except CredentialDecryptionError:
                return self._resolution_error(stored, "飞书 App Secret 无法解密")

        return ResolvedFeishuSettings(
            app_id=stored.app_id,
            app_secret=secret,
            default_chat_id=stored.default_chat_id,
            enabled=stored.enabled,
            updated_at=stored.updated_at,
        )

    @staticmethod
    def _resolution_error(
        stored: FeishuBotSettings,
        error: str,
    ) -> ResolvedFeishuSettings:
        return ResolvedFeishuSettings(
            app_id=stored.app_id,
            app_secret=None,
            default_chat_id=stored.default_chat_id,
            enabled=False,
            updated_at=stored.updated_at,
            error=error,
        )


def ensure_feishu_bot_user(session: Session) -> User:
    user = session.scalar(select(User).where(User.username == "feishu-bot"))
    if user is None:
        user = User(
            username="feishu-bot",
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role="user",
            is_active=True,
        )
        session.add(user)
    else:
        user.role = "user"
        user.is_active = True
    session.flush()
    return user
