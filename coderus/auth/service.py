from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.auth.security import hash_password, verify_password
from coderus.models import User

SYSTEM_USERNAMES = frozenset({"feishu-bot"})


def normalize_username(username: str) -> str:
    return username.strip().lower()


def ensure_bootstrap_admin(session: Session, username: str, password: SecretStr) -> User:
    normalized = normalize_username(username)
    existing = session.scalar(select(User).where(User.username == normalized))
    if existing is not None:
        return existing
    admin = User(
        username=normalized,
        password_hash=hash_password(password.get_secret_value()),
        role="admin",
    )
    session.add(admin)
    session.commit()
    return admin


def authenticate(session: Session, username: str, password: str) -> User | None:
    normalized = normalize_username(username)
    if normalized in SYSTEM_USERNAMES:
        return None
    user = session.scalar(select(User).where(User.username == normalized))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(UTC)
    session.commit()
    return user


def create_user(session: Session, username: str, password: str, role: str = "user") -> User:
    normalized = normalize_username(username)
    if not normalized or normalized in SYSTEM_USERNAMES or role not in {"admin", "user"}:
        raise ValueError("invalid user")
    if len(password) < 8:
        raise ValueError("password too short")
    if session.scalar(select(User.id).where(User.username == normalized)) is not None:
        raise ValueError("username already exists")
    user = User(username=normalized, password_hash=hash_password(password), role=role)
    session.add(user)
    session.commit()
    return user
