from pydantic import SecretStr

from coderus.auth.security import hash_password, verify_password
from coderus.auth.service import authenticate, ensure_bootstrap_admin
from coderus.models import User


def test_password_hash_uses_argon2() -> None:
    encoded = hash_password("correct horse battery staple")

    assert encoded.startswith("$argon2")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_bootstrap_admin_is_idempotent(session) -> None:
    first = ensure_bootstrap_admin(session, "Admin", SecretStr("initial-password"))
    second = ensure_bootstrap_admin(session, "Admin", SecretStr("different-password"))

    assert first.id == second.id
    assert second.username == "admin"
    assert verify_password("initial-password", second.password_hash)


def test_authenticate_rejects_disabled_user(session) -> None:
    user = ensure_bootstrap_admin(session, "admin", SecretStr("initial-password"))
    assert authenticate(session, "ADMIN", "initial-password") == user

    user.is_active = False
    session.commit()
    assert authenticate(session, "admin", "initial-password") is None


def test_authenticate_rejects_feishu_system_user_even_with_valid_password(session) -> None:
    session.add(
        User(
            username="feishu-bot",
            password_hash=hash_password("known-password"),
            role="user",
            is_active=True,
        )
    )
    session.commit()

    assert authenticate(session, "FEISHU-BOT", "known-password") is None
