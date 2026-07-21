import os
from base64 import urlsafe_b64encode

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from coderus.integrations.forge_credentials import EncryptedForgeCredentialStore
from coderus.integrations.gitcode_credentials import (
    GitCodeCredentialManager,
    GitCodeCredentialValidationError,
)
from coderus.integrations.github_credentials import GitHubCredentialManager
from coderus.models import IntegrationCredential, User
from coderus.security.credentials import CredentialCipher


def make_cipher() -> CredentialCipher:
    return CredentialCipher(urlsafe_b64encode(os.urandom(32)).decode())


def add_admin(session) -> User:
    user = User(username="admin", password_hash="hash", role="admin")
    session.add(user)
    session.flush()
    return user


def test_resolve_prefers_database_and_isolates_provider_fallbacks(session) -> None:
    store = EncryptedForgeCredentialStore(cipher=make_cipher())
    store.save(
        session,
        store.prepare("github", "OctoCat", "db-github"),
        updated_by=add_admin(session),
    )
    session.commit()

    github = store.resolve(session, "github", "env-github")
    gitcode = store.resolve(session, "gitcode", "env-gitcode")

    assert github.token.get_secret_value() == "db-github"
    assert gitcode.token.get_secret_value() == "env-gitcode"
    assert github.source == "database"
    assert gitcode.source == "environment"
    assert "db-github" not in repr(github)


def test_prepared_credential_hides_token_in_repr() -> None:
    prepared = EncryptedForgeCredentialStore(cipher=make_cipher()).prepare(
        "github", "OctoCat", "secret-token"
    )

    assert isinstance(prepared.token, SecretStr)
    assert "secret-token" not in repr(prepared)


def test_resolve_reports_missing_key_without_environment_fallback(session) -> None:
    cipher = make_cipher()
    writer = EncryptedForgeCredentialStore(cipher=cipher)
    writer.save(
        session,
        writer.prepare("github", "OctoCat", "database-token"),
        updated_by=add_admin(session),
    )
    session.commit()

    resolved = EncryptedForgeCredentialStore(cipher=None).resolve(
        session, "github", "environment-token"
    )

    assert resolved.source == "error"
    assert resolved.token is None
    assert resolved.error == "missing credential encryption key"


def test_resolve_reports_decryption_failure_without_token_leak(session) -> None:
    admin = add_admin(session)
    session.add(
        IntegrationCredential(
            provider="gitcode",
            account_name="gitcode-user",
            encrypted_token="v1:broken",
            updated_by=admin.id,
        )
    )
    session.commit()

    resolved = EncryptedForgeCredentialStore(cipher=make_cipher()).resolve(
        session, "gitcode", "environment-token"
    )

    assert resolved.source == "error"
    assert resolved.token is None
    assert resolved.error == "credential cannot be decrypted"
    assert "environment-token" not in repr(resolved)


def test_save_upserts_each_provider_without_cross_provider_replacement(session) -> None:
    store = EncryptedForgeCredentialStore(cipher=make_cipher())
    admin = add_admin(session)
    first = store.save(
        session, store.prepare("github", "OctoCat", "first-token"), updated_by=admin
    )
    store.save(
        session, store.prepare("gitcode", "gitcode-user", "gitcode-token"), updated_by=admin
    )
    second = store.save(
        session, store.prepare("github", "OctoCat", "second-token"), updated_by=admin
    )
    session.commit()

    rows = {row.provider: row for row in session.scalars(select(IntegrationCredential))}
    assert set(rows) == {"github", "gitcode"}
    assert second.id == first.id
    assert store.resolve(session, "github").token.get_secret_value() == "second-token"
    assert store.resolve(session, "gitcode").token.get_secret_value() == "gitcode-token"


def test_github_legacy_ciphertext_uses_the_existing_github_aad(session) -> None:
    cipher = make_cipher()
    admin = add_admin(session)
    session.add(
        IntegrationCredential(
            provider="github",
            account_name="OctoCat",
            encrypted_token=cipher.encrypt("legacy-github-token"),
            updated_by=admin.id,
        )
    )
    session.commit()

    resolved = EncryptedForgeCredentialStore(cipher=cipher).resolve(session, "github")

    assert resolved.token.get_secret_value() == "legacy-github-token"


@pytest.mark.parametrize(
    ("stored_provider", "cipher_provider"),
    [("github", "gitcode"), ("gitcode", "github")],
)
def test_cross_provider_ciphertext_is_rejected_without_environment_fallback(
    session, stored_provider: str, cipher_provider: str
) -> None:
    store = EncryptedForgeCredentialStore(cipher=make_cipher())
    admin = add_admin(session)
    encrypted = store.prepare(cipher_provider, "account", "cross-provider-token")
    session.add(
        IntegrationCredential(
            provider=stored_provider,
            account_name="account",
            encrypted_token=encrypted.encrypted_token,
            updated_by=admin.id,
        )
    )
    session.commit()

    resolved = store.resolve(session, stored_provider, "environment-token")

    assert resolved.source == "error"
    assert resolved.token is None
    assert "environment-token" not in repr(resolved)


def test_unknown_provider_is_rejected_before_credential_access(session) -> None:
    store = EncryptedForgeCredentialStore(cipher=make_cipher())

    with pytest.raises(ValueError, match="unsupported forge provider"):
        store.prepare("unknown", "account", "token")
    with pytest.raises(ValueError, match="unsupported forge provider"):
        store.resolve(session, "unknown", "environment-token")


def test_managers_reject_prepared_credentials_for_the_other_provider(session) -> None:
    cipher = make_cipher()
    store = EncryptedForgeCredentialStore(cipher=cipher)
    admin = add_admin(session)
    github = GitHubCredentialManager(cipher=cipher, client=httpx.Client())
    gitcode = GitCodeCredentialManager(cipher=cipher, client=httpx.Client())
    github_prepared = store.prepare("github", "OctoCat", "github-token")
    gitcode_prepared = store.prepare("gitcode", "gitcode-user", "gitcode-token")
    github.save(session, github_prepared, updated_by=admin)
    session.commit()

    with pytest.raises(ValueError, match="GitHub"):
        github.save(session, gitcode_prepared, updated_by=admin)
    with pytest.raises(ValueError, match="GitCode"):
        gitcode.save(session, github_prepared, updated_by=admin)

    rows = {row.provider: row for row in session.scalars(select(IntegrationCredential))}
    assert set(rows) == {"github"}
    assert store.resolve(session, "github").token.get_secret_value() == "github-token"


def gitcode_client(*, login: str = "GitCodeUser", status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.gitcode.com/api/v5/user"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert not request.url.params
        return httpx.Response(status, json={"login": login})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gitcode_prepare_validates_matching_account_with_bearer_token() -> None:
    manager = GitCodeCredentialManager(cipher=make_cipher(), client=gitcode_client())

    prepared = manager.prepare("gitcodeuser", "secret-token")

    assert prepared.provider == "gitcode"
    assert prepared.account_name == "GitCodeUser"
    assert prepared.token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(prepared)


def test_gitcode_prepare_rejects_mismatched_account_without_leaking_token() -> None:
    manager = GitCodeCredentialManager(
        cipher=make_cipher(), client=gitcode_client(login="secret-token")
    )

    try:
        manager.prepare("gitcodeuser", "secret-token")
    except GitCodeCredentialValidationError as error:
        assert str(error) == "GitCode 用户名与 Token 所属账号不一致"
        assert "secret-token" not in str(error)
    else:
        raise AssertionError("expected GitCode credential validation to fail")


@pytest.mark.parametrize(
    ("status", "expected_message"),
    [
        (401, "GitCode Token 无效或已失效"),
        (403, "GitCode Token 无权验证账号"),
        (429, "GitCode 验证请求过于频繁，请稍后重试"),
        (500, "GitCode 账号验证失败，请稍后重试"),
    ],
)
def test_gitcode_prepare_maps_remote_failures_without_token_leak(
    status: int, expected_message: str
) -> None:
    manager = GitCodeCredentialManager(
        cipher=make_cipher(), client=gitcode_client(status=status)
    )

    with pytest.raises(GitCodeCredentialValidationError) as error:
        manager.prepare("gitcodeuser", "secret-token")

    assert str(error.value) == expected_message
    assert "secret-token" not in str(error.value)


def test_gitcode_prepare_maps_network_failure_without_token_leak() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable")

    manager = GitCodeCredentialManager(
        cipher=make_cipher(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(GitCodeCredentialValidationError) as error:
        manager.prepare("gitcodeuser", "secret-token")

    assert str(error.value) == "无法连接 GitCode，请检查网络后重试"
    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("account_name", "token", "expected_message"),
    [
        ("", "secret-token", "请输入 GitCode 用户名"),
        ("git code", "secret-token", "GitCode 用户名格式无效"),
        ("gitcodeuser", "", "请输入 GitCode Token"),
    ],
)
def test_gitcode_prepare_requires_username_and_token(
    account_name: str, token: str, expected_message: str
) -> None:
    manager = GitCodeCredentialManager(cipher=make_cipher(), client=gitcode_client())

    with pytest.raises(GitCodeCredentialValidationError) as error:
        manager.prepare(account_name, token)

    assert str(error.value) == expected_message
    assert "secret-token" not in str(error.value)


def test_gitcode_prepare_rejects_invalid_account_response_without_leaking_body() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b'{"login": null, "detail": "response-secret"}',
                headers={"Content-Type": "application/json"},
            )
        )
    )
    manager = GitCodeCredentialManager(cipher=make_cipher(), client=client)

    with pytest.raises(GitCodeCredentialValidationError) as error:
        manager.prepare("gitcodeuser", "secret-token")

    assert str(error.value) == "GitCode 返回的账号信息无效"
    assert "secret-token" not in str(error.value)
    assert "response-secret" not in str(error.value)
    assert error.value.__cause__ is None
