from __future__ import annotations

import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_TAG_BYTES = 16


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """Authenticated ciphertext whose representation never includes stored bytes."""

    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nonce", _copy_bytes(self.nonce, parameter="nonce"))
        object.__setattr__(
            self,
            "ciphertext",
            _copy_bytes(self.ciphertext, parameter="ciphertext"),
        )


class AesGcmCipher:
    """Reusable AES-256-GCM primitive with fixed, redacted failure behavior."""

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("key must be bytes")
        if len(key) != 32:
            raise ValueError("key must be exactly 32 bytes")
        self._cipher = AESGCM(bytes(key))

    def encrypt(
        self,
        plaintext: bytes | bytearray | memoryview,
        associated_data: bytes | bytearray | memoryview,
    ) -> EncryptedBlob:
        plaintext_copy = _copy_bytes(plaintext, parameter="plaintext")
        associated_data_copy = _copy_bytes(associated_data, parameter="associated_data")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext_copy, associated_data_copy)
        return EncryptedBlob(nonce=nonce, ciphertext=ciphertext)

    def decrypt(
        self,
        blob: EncryptedBlob,
        associated_data: bytes | bytearray | memoryview,
    ) -> bytes:
        if not isinstance(blob, EncryptedBlob):
            raise TypeError("blob must be an EncryptedBlob")
        associated_data_copy = _copy_bytes(associated_data, parameter="associated_data")
        if len(blob.nonce) != _NONCE_BYTES or len(blob.ciphertext) < _TAG_BYTES:
            raise ValueError("encrypted blob authentication failed")
        try:
            return bytes(self._cipher.decrypt(blob.nonce, blob.ciphertext, associated_data_copy))
        except (InvalidTag, ValueError, TypeError):
            raise ValueError("encrypted blob authentication failed") from None


def _copy_bytes(value: object, *, parameter: str) -> bytes:
    if not isinstance(value, bytes | bytearray | memoryview):
        raise TypeError(f"{parameter} must be bytes-like")
    return bytes(value)
