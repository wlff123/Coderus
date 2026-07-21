import os
from base64 import urlsafe_b64encode

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from coderus.integrations.github_credentials import (
    GitHubCredentialManager,
    GitHubCredentialValidationError,
    PreparedGitHubCredential,
)
from coderus.models import IntegrationCredential, User
from coderus.security.credentials import CredentialCipher


@pytest.fixture
def cipher() -> CredentialCipher:
    key = urlsafe_b64encode(os.urandom(32)).decode()
    return CredentialCipher(key)


def github_client(*, login: str = "OctoCat", status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/user"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(status, json={"login": login})

    return httpx.Client(transport=httpx.MockTransport(handler))


def add_admin(session) -> User:
    user = User(username="admin", password_hash="hash", role="admin")
    session.add(user)
    session.flush()
    return user


def prepared(cipher: CredentialCipher, token: str = "database-token") -> PreparedGitHubCredential:
    return PreparedGitHubCredential(
        provider="github",
        account_name="OctoCat",
        token=SecretStr(token),
        encrypted_token=cipher.encrypt(token),
    )


def test_resolve_prefers_database_credential(session, cipher: CredentialCipher) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client())
    manager.save(session, prepared(cipher), updated_by=add_admin(session))
    session.commit()

    resolved = manager.resolve(session, fallback_token="environment-token")

    assert resolved.source == "database"
    assert resolved.account_name == "OctoCat"
    assert resolved.token is not None
    assert resolved.token.get_secret_value() == "database-token"


def test_resolve_uses_environment_only_without_database_row(session) -> None:
    manager = GitHubCredentialManager(cipher=None, client=github_client())

    resolved = manager.resolve(session, fallback_token="environment-token")

    assert resolved.source == "environment"
    assert resolved.token is not None
    assert resolved.token.get_secret_value() == "environment-token"


def test_resolve_does_not_fallback_when_database_cipher_fails(
    session, cipher: CredentialCipher
) -> None:
    admin = add_admin(session)
    session.add(
        IntegrationCredential(
            provider="github",
            account_name="OctoCat",
            encrypted_token="v1:broken",
            updated_by=admin.id,
        )
    )
    session.commit()

    resolved = GitHubCredentialManager(cipher=cipher, client=github_client()).resolve(
        session, fallback_token="environment-token"
    )

    assert resolved.source == "error"
    assert resolved.token is None
    assert resolved.error == "GitHub 凭据无法解密"


def test_database_never_contains_plaintext_token(session, cipher: CredentialCipher) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client())
    row = manager.save(session, prepared(cipher, "plain-token"), updated_by=add_admin(session))
    session.commit()

    assert row.encrypted_token.startswith("v1:")
    assert "plain-token" not in row.encrypted_token


def test_save_updates_single_github_row(session, cipher: CredentialCipher) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client())
    admin = add_admin(session)
    first = manager.save(session, prepared(cipher, "first-token"), updated_by=admin)
    session.commit()
    first_id = first.id

    second = manager.save(session, prepared(cipher, "second-token"), updated_by=admin)
    session.commit()

    rows = session.scalars(select(IntegrationCredential)).all()
    assert len(rows) == 1
    assert second.id == first_id
    assert cipher.decrypt(second.encrypted_token) == "second-token"


def test_prepare_accepts_case_insensitive_matching_login(cipher: CredentialCipher) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client(login="OctoCat"))

    result = manager.prepare("octocat", "secret-token")

    assert result.account_name == "OctoCat"
    assert result.token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(result)


def test_prepare_rejects_mismatched_login_without_leaking_token(
    cipher: CredentialCipher,
) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client(login="secret-token"))

    with pytest.raises(GitHubCredentialValidationError) as caught:
        manager.prepare("octocat", "secret-token")

    assert "secret-token" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_prepare_maps_remote_errors_to_safe_message(
    cipher: CredentialCipher, status: int
) -> None:
    manager = GitHubCredentialManager(cipher=cipher, client=github_client(status=status))

    with pytest.raises(GitHubCredentialValidationError) as caught:
        manager.prepare("octocat", "secret-token")

    assert "secret-token" not in str(caught.value)
