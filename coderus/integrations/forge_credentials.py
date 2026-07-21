from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.models import IntegrationCredential, User
from coderus.security.credentials import CredentialCipher, CredentialDecryptionError


class CredentialEncryptionUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class PreparedForgeCredential:
    provider: str
    account_name: str
    token: SecretStr = field(repr=False)
    encrypted_token: str


@dataclass(frozen=True)
class ResolvedForgeCredential:
    provider: str
    account_name: str | None
    token: SecretStr | None = field(repr=False)
    source: Literal["database", "environment", "none", "error"]
    updated_at: datetime | None = None
    error: str | None = None


class EncryptedForgeCredentialStore:
    def __init__(self, *, cipher: CredentialCipher | None) -> None:
        self._cipher = cipher

    def prepare(
        self, provider: str, account_name: str, token: str
    ) -> PreparedForgeCredential:
        self._validate_provider(provider)
        if self._cipher is None:
            raise CredentialEncryptionUnavailable("missing credential encryption key")
        return PreparedForgeCredential(
            provider=provider,
            account_name=account_name,
            token=SecretStr(token),
            encrypted_token=self._cipher.encrypt_for_provider(provider, token),
        )

    def resolve(
        self,
        session: Session,
        provider: str,
        fallback_token: SecretStr | str | None = None,
    ) -> ResolvedForgeCredential:
        self._validate_provider(provider)
        stored = session.scalar(
            select(IntegrationCredential).where(IntegrationCredential.provider == provider)
        )
        if stored is not None:
            if self._cipher is None:
                return ResolvedForgeCredential(
                    provider=provider,
                    account_name=stored.account_name,
                    token=None,
                    source="error",
                    updated_at=stored.updated_at,
                    error="missing credential encryption key",
                )
            try:
                token = self._cipher.decrypt_for_provider(provider, stored.encrypted_token)
            except CredentialDecryptionError:
                return ResolvedForgeCredential(
                    provider=provider,
                    account_name=stored.account_name,
                    token=None,
                    source="error",
                    updated_at=stored.updated_at,
                    error="credential cannot be decrypted",
                )
            return ResolvedForgeCredential(
                provider=provider,
                account_name=stored.account_name,
                token=SecretStr(token),
                source="database",
                updated_at=stored.updated_at,
            )

        if fallback_token is not None:
            token = (
                fallback_token.get_secret_value()
                if isinstance(fallback_token, SecretStr)
                else fallback_token
            )
            return ResolvedForgeCredential(
                provider=provider,
                account_name=None,
                token=SecretStr(token),
                source="environment",
            )
        return ResolvedForgeCredential(
            provider=provider,
            account_name=None,
            token=None,
            source="none",
        )

    def save(
        self,
        session: Session,
        prepared: PreparedForgeCredential,
        *,
        updated_by: User,
    ) -> IntegrationCredential:
        self._validate_provider(prepared.provider)
        stored = session.scalar(
            select(IntegrationCredential).where(
                IntegrationCredential.provider == prepared.provider
            )
        )
        if stored is None:
            stored = IntegrationCredential(
                provider=prepared.provider,
                account_name=prepared.account_name,
                encrypted_token=prepared.encrypted_token,
                updated_by=updated_by.id,
            )
            session.add(stored)
        else:
            stored.account_name = prepared.account_name
            stored.encrypted_token = prepared.encrypted_token
            stored.updated_by = updated_by.id
        session.flush()
        return stored

    @staticmethod
    def _validate_provider(provider: str) -> None:
        CredentialCipher.forge_token_aad(provider)
