from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest


def encryption_types():
    from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob

    return AesGcmCipher, EncryptedBlob


@pytest.mark.parametrize("key", [b"short", b"x" * 31, b"x" * 33, bytearray(b"x" * 32), "x" * 32])
def test_cipher_requires_an_exact_bytes_256_bit_key_without_echoing_it(key: object) -> None:
    AesGcmCipher, _ = encryption_types()

    with pytest.raises((TypeError, ValueError)) as caught:
        AesGcmCipher(key)  # type: ignore[arg-type]

    assert repr(key) not in str(caught.value)


def test_encrypt_uses_fresh_nonces_and_copies_mutable_inputs() -> None:
    AesGcmCipher, _ = encryption_types()
    cipher = AesGcmCipher(b"k" * 32)
    plaintext = bytearray(b"private diagnostic")
    associated_data = bytearray(b"monitor-7")

    first = cipher.encrypt(plaintext, associated_data)
    second = cipher.encrypt(plaintext, associated_data)
    plaintext[:] = b"x" * len(plaintext)
    associated_data[:] = b"x" * len(associated_data)

    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert cipher.decrypt(first, b"monitor-7") == b"private diagnostic"


def test_encrypted_blob_is_immutable_copied_and_has_a_redacted_repr() -> None:
    _, EncryptedBlob = encryption_types()
    nonce = bytearray(b"n" * 12)
    ciphertext = bytearray(b"ciphertext-with-tag")

    blob = EncryptedBlob(nonce=nonce, ciphertext=ciphertext)
    nonce[0] = ord("x")
    ciphertext[0] = ord("x")

    assert blob.nonce == b"n" * 12
    assert blob.ciphertext == b"ciphertext-with-tag"
    representation = repr(blob)
    assert "nnnn" not in representation
    assert "ciphertext" not in representation
    with pytest.raises(FrozenInstanceError):
        blob.nonce = b"x" * 12  # type: ignore[misc]


@pytest.mark.parametrize("mutation", ["wrong_key", "wrong_aad", "tamper", "truncate", "nonce"])
def test_decrypt_fails_closed_for_every_authentication_or_format_error(mutation: str) -> None:
    AesGcmCipher, EncryptedBlob = encryption_types()
    key = b"k" * 32
    cipher = AesGcmCipher(key)
    aad = b"monitor-secret-id"
    plaintext = b"<html>private secret diagnostic</html>"
    blob = cipher.encrypt(plaintext, aad)
    decryptor = cipher

    if mutation == "wrong_key":
        decryptor = AesGcmCipher(b"z" * 32)
    elif mutation == "wrong_aad":
        aad = b"other-monitor"
    elif mutation == "tamper":
        blob = EncryptedBlob(blob.nonce, blob.ciphertext[:-1] + bytes([blob.ciphertext[-1] ^ 1]))
    elif mutation == "truncate":
        blob = EncryptedBlob(blob.nonce, blob.ciphertext[:8])
    else:
        blob = EncryptedBlob(blob.nonce[:-1], blob.ciphertext)

    with pytest.raises(ValueError) as caught:
        decryptor.decrypt(blob, aad)

    message = str(caught.value)
    for sensitive in (key, aad, plaintext, blob.nonce, blob.ciphertext):
        assert sensitive.hex() not in message
        assert repr(sensitive) not in message


@pytest.mark.parametrize("value", ["text", 7, object()])
def test_encrypt_rejects_non_bytes_like_inputs_without_echoing_them(value: object) -> None:
    AesGcmCipher, _ = encryption_types()
    cipher = AesGcmCipher(b"k" * 32)

    with pytest.raises(TypeError) as caught:
        cipher.encrypt(value, b"aad")  # type: ignore[arg-type]

    assert repr(value) not in str(caught.value)
