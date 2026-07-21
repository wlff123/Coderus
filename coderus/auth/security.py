import secrets

from pwdlib import PasswordHash

_PASSWORD_HASH = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _PASSWORD_HASH.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _PASSWORD_HASH.verify(password, password_hash)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(expected: str | None, actual: str | None) -> bool:
    return bool(expected and actual and secrets.compare_digest(expected, actual))
