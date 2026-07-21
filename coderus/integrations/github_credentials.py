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

PreparedGitHubCredential = PreparedForgeCredential
ResolvedGitHubCredential = ResolvedForgeCredential
GitHubCredentialEncryptionUnavailable = CredentialEncryptionUnavailable


class GitHubCredentialValidationError(ValueError):
    pass


class GitHubCredentialManager:
    def __init__(self, *, cipher: CredentialCipher | None, client: httpx.Client) -> None:
        self._store = EncryptedForgeCredentialStore(cipher=cipher)
        self._client = client

    def resolve(
        self,
        session: Session,
        fallback_token: SecretStr | str | None = None,
    ) -> ResolvedForgeCredential:
        resolved = self._store.resolve(session, "github", fallback_token)
        if resolved.error == "missing credential encryption key":
            return ResolvedForgeCredential(
                provider="github",
                account_name=resolved.account_name,
                token=None,
                source="error",
                updated_at=resolved.updated_at,
                error="缺少凭据加密密钥",
            )
        if resolved.error == "credential cannot be decrypted":
            return ResolvedForgeCredential(
                provider="github",
                account_name=resolved.account_name,
                token=None,
                source="error",
                updated_at=resolved.updated_at,
                error="GitHub 凭据无法解密",
            )
        return resolved

    def prepare(self, account_name: str, token: str) -> PreparedForgeCredential:
        requested_name = account_name.strip()
        if not requested_name or not token:
            raise GitHubCredentialValidationError("GitHub username and token are required")
        verified_name = self._verify_account(requested_name, token)
        return self._store.prepare("github", verified_name, token)

    def save(
        self,
        session: Session,
        prepared: PreparedForgeCredential,
        *,
        updated_by: User,
    ) -> IntegrationCredential:
        if prepared.provider != "github":
            raise ValueError("prepared credential is not for GitHub")
        return self._store.save(session, prepared, updated_by=updated_by)

    def _verify_account(self, account_name: str, token: str) -> str:
        try:
            response = self._client.get(
                "https://api.github.com/user",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.RequestError as exc:
            raise GitHubCredentialValidationError("unable to connect to GitHub") from exc

        if response.status_code == 401:
            raise GitHubCredentialValidationError("GitHub token is invalid")
        if response.status_code in {403, 429}:
            raise GitHubCredentialValidationError("GitHub validation is rate limited")
        if not 200 <= response.status_code < 300:
            raise GitHubCredentialValidationError("GitHub validation failed")
        try:
            verified_name = response.json()["login"]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubCredentialValidationError("GitHub returned an invalid account") from exc
        if not isinstance(verified_name, str) or not verified_name:
            raise GitHubCredentialValidationError("GitHub returned an invalid account")
        if verified_name.casefold() != account_name.casefold():
            raise GitHubCredentialValidationError("GitHub account does not match token")
        return verified_name
