"""Unit tests for credentials_encryption.py — no database needed."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from hermes_v2.trading.credentials_encryption import (
    CredentialsDecryptionError,
    CredentialsEncryptionNotConfiguredError,
    decrypt_credential,
    encrypt_credential,
)

_KEY_A = Fernet.generate_key().decode()
_KEY_B = Fernet.generate_key().decode()


def test_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_A)
    ciphertext = encrypt_credential("my-binance-api-secret")
    assert decrypt_credential(ciphertext) == "my-binance-api-secret"


def test_ciphertext_never_contains_the_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_A)
    ciphertext = encrypt_credential("super-secret-value-12345")
    assert "super-secret-value-12345" not in ciphertext


def test_missing_key_fails_closed_on_encrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(CredentialsEncryptionNotConfiguredError):
        encrypt_credential("anything")


def test_missing_key_fails_closed_on_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_A)
    ciphertext = encrypt_credential("anything")
    monkeypatch.delenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(CredentialsEncryptionNotConfiguredError):
        decrypt_credential(ciphertext)


def test_wrong_key_never_returns_a_fabricated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_A)
    ciphertext = encrypt_credential("anything")
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_B)
    with pytest.raises(CredentialsDecryptionError):
        decrypt_credential(ciphertext)


def test_rotation_decrypts_under_the_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_A)
    ciphertext = encrypt_credential("value-encrypted-under-key-a")

    # Rotate: key B becomes current, key A moves to the previous list.
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_B)
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS", _KEY_A)

    assert decrypt_credential(ciphertext) == "value-encrypted-under-key-a"


def test_new_writes_after_rotation_use_the_new_key_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", _KEY_B)
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS", _KEY_A)
    ciphertext = encrypt_credential("a-fresh-value")

    # Confirm it decrypts under key B alone (no previous key needed).
    monkeypatch.delenv("HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS", raising=False)
    assert decrypt_credential(ciphertext) == "a-fresh-value"


def test_invalid_key_format_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(CredentialsEncryptionNotConfiguredError):
        encrypt_credential("anything")
