import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

GITHUB_TOKEN_AAD = b"coderus:github:token:v1"
GITCODE_TOKEN_AAD = b"coderus:gitcode:token:v1"
FEISHU_APP_SECRET_AAD = b"coderus:feishu:app-secret:v1"
FORGE_TOKEN_AADS = {
    "github": GITHUB_TOKEN_AAD,
    "gitcode": GITCODE_TOKEN_AAD,
}


class CredentialDecryptionError(ValueError):
    pass


class CredentialCipher:
    def __init__(self, key: SecretStr | str, *, aad: bytes = GITHUB_TOKEN_AAD) -> None:
        encoded = key.get_secret_value() if isinstance(key, SecretStr) else key
        try:
            raw = urlsafe_b64decode(encoded.encode())
        except (Base64Error, ValueError) as exc:
            raise ValueError("credential encryption key is invalid") from exc
        if len(raw) != 32:
            raise ValueError("credential encryption key must contain 32 bytes")
        self._cipher = AESGCM(raw)
        self._aad = aad

    @classmethod
    def for_feishu_app_secret(cls, key: SecretStr | str) -> Self:
        return cls(key, aad=FEISHU_APP_SECRET_AAD)

    def encrypt(self, plaintext: str) -> str:
        return self._encrypt(plaintext, self._aad)

    def encrypt_for_provider(self, provider: str, plaintext: str) -> str:
        return self._encrypt(plaintext, self.forge_token_aad(provider))

    def decrypt(self, payload: str) -> str:
        return self._decrypt(payload, self._aad)

    def decrypt_for_provider(self, provider: str, payload: str) -> str:
        return self._decrypt(payload, self.forge_token_aad(provider))

    @staticmethod
    def forge_token_aad(provider: str) -> bytes:
        try:
            return FORGE_TOKEN_AADS[provider]
        except KeyError as exc:
            raise ValueError("unsupported forge provider") from exc

    def _encrypt(self, plaintext: str, aad: bytes) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode(), aad)
        return "v1:" + urlsafe_b64encode(nonce + ciphertext).decode()

    def _decrypt(self, payload: str, aad: bytes) -> str:
        try:
            version, encoded = payload.split(":", 1)
            if version != "v1":
                raise ValueError
            raw = urlsafe_b64decode(encoded.encode())
            if len(raw) < 28:
                raise ValueError
            return self._cipher.decrypt(raw[:12], raw[12:], aad).decode()
        except (Base64Error, InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise CredentialDecryptionError("stored credential cannot be decrypted") from exc
