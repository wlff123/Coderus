import os
from base64 import urlsafe_b64encode

import pytest

from coderus.security.credentials import CredentialCipher, CredentialDecryptionError


def encryption_key() -> str:
    return urlsafe_b64encode(os.urandom(32)).decode()


def test_cipher_round_trip_uses_random_nonce() -> None:
    cipher = CredentialCipher(encryption_key())

    first = cipher.encrypt("github-token")
    second = cipher.encrypt("github-token")

    assert first.startswith("v1:")
    assert first != second
    assert cipher.decrypt(first) == "github-token"
    assert cipher.decrypt(second) == "github-token"


def test_cipher_rejects_tampered_payload() -> None:
    cipher = CredentialCipher(encryption_key())
    payload = cipher.encrypt("github-token")
    replacement = "A" if payload[-2] != "A" else "B"
    tampered = payload[:-2] + replacement + payload[-1]

    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(tampered)


@pytest.mark.parametrize("value", ["short", "", "not-base64"])
def test_cipher_rejects_invalid_key(value: str) -> None:
    with pytest.raises(ValueError, match="encryption key"):
        CredentialCipher(value)


@pytest.mark.parametrize("payload", ["", "v2:abcd", "v1:not-base64"])
def test_cipher_rejects_invalid_payload(payload: str) -> None:
    cipher = CredentialCipher(encryption_key())

    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(payload)
