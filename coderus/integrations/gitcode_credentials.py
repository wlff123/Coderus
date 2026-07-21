from __future__ import annotations

import httpx
from pydantic import SecretStr
from sqlalchemy.orm import Session

from coderus.integrations.forge_credentials import (
    CredentialEncryptionUnavailable,
    EncryptedForgeCredentialStore,
    PreparedForgeCredential,
    ResolvedForgeCredential,
)
from coderus.models import IntegrationCredential, User
from coderus.security.credentials import CredentialCipher

GitCodeCredentialEncryptionUnavailable = CredentialEncryptionUnavailable


class GitCodeCredentialValidationError(ValueError):
    pass


class GitCodeCredentialManager:
    def __init__(self, *, cipher: CredentialCipher | None, client: httpx.Client) -> None:
        self._store = EncryptedForgeCredentialStore(cipher=cipher)
        self._client = client

    def resolve(
        self,
        session: Session,
        fallback_token: SecretStr | str | None = None,
    ) -> ResolvedForgeCredential:
        return self._store.resolve(session, "gitcode", fallback_token)

    def prepare(self, account_name: str, token: str) -> PreparedForgeCredential:
        requested_name = account_name.strip()
        if not requested_name:
            raise GitCodeCredentialValidationError("请输入 GitCode 用户名")
        if requested_name != account_name or any(
            character.isspace() for character in requested_name
        ):
            raise GitCodeCredentialValidationError("GitCode 用户名格式无效")
        if not token.strip():
            raise GitCodeCredentialValidationError("请输入 GitCode Token")
        verified_name = self._verify_account(requested_name, token)
        return self._store.prepare("gitcode", verified_name, token)

    def save(
        self,
        session: Session,
        prepared: PreparedForgeCredential,
        *,
        updated_by: User,
    ) -> IntegrationCredential:
        if prepared.provider != "gitcode":
            raise ValueError("prepared credential is not for GitCode")
        return self._store.save(session, prepared, updated_by=updated_by)

    def _verify_account(self, account_name: str, token: str) -> str:
        try:
            response = self._client.get(
                "https://api.gitcode.com/api/v5/user",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
        except httpx.RequestError:
            raise GitCodeCredentialValidationError(
                "无法连接 GitCode，请检查网络后重试"
            ) from None

        if response.status_code == 401:
            raise GitCodeCredentialValidationError("GitCode Token 无效或已失效")
        if response.status_code == 403:
            raise GitCodeCredentialValidationError("GitCode Token 无权验证账号")
        if response.status_code == 429:
            raise GitCodeCredentialValidationError(
                "GitCode 验证请求过于频繁，请稍后重试"
            )
        if not 200 <= response.status_code < 300:
            raise GitCodeCredentialValidationError("GitCode 账号验证失败，请稍后重试")
        try:
            verified_name = response.json()["login"]
        except (KeyError, TypeError, ValueError):
            raise GitCodeCredentialValidationError("GitCode 返回的账号信息无效") from None
        if not isinstance(verified_name, str) or not verified_name:
            raise GitCodeCredentialValidationError("GitCode 返回的账号信息无效")
        if verified_name.casefold() != account_name.casefold():
            raise GitCodeCredentialValidationError(
                "GitCode 用户名与 Token 所属账号不一致"
            )
        return verified_name
