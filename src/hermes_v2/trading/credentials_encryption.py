"""Encryption at rest for per-user Binance credentials.

Multi-tenancy means many users each connect their own Binance account —
there is no `.env` slot for "user X's API key," so these have to live in
the database. Storing them in plaintext would make the database itself a
credential store for real trading accounts; `MultiFernet` (AES128-CBC +
HMAC-SHA256, versioned, authenticated tokens) closes that gap.

`HERMES_CREDENTIALS_ENCRYPTION_KEY` is a new `.env`-only secret, generated
once (`Fernet.generate_key()`, base64-encoded, printed and copied into
`.env` by hand) and handled exactly like every other secret in
`docs/security/secrets-management.md`'s table — gitignored, `chmod 600`,
never round-tripped through git. Missing it is a configuration error, not
a silent fallback: every credential read/write fails closed until an
operator sets it, mirroring `risk_engine.py`'s own "an unconfigured limit
rejects, it never permits" philosophy.

`MultiFernet` (not bare `Fernet`) buys key rotation for free: to rotate,
generate a new key, set it as `HERMES_CREDENTIALS_ENCRYPTION_KEY`, move
the previous value into `HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS`
(comma-separated, decrypt-only — never used to encrypt new values), and
restart. New writes use the new key; existing ciphertext keeps decrypting
under the previous key until a separate, deliberate re-encryption pass
retires it. No bulk migration is required for the rotation itself to be
safe.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_ENCRYPTION_KEY_VAR = "HERMES_CREDENTIALS_ENCRYPTION_KEY"
_PREVIOUS_ENCRYPTION_KEYS_VAR = "HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS"


class CredentialsEncryptionError(RuntimeError):
    """Base class for every error this module raises."""


class CredentialsEncryptionNotConfiguredError(CredentialsEncryptionError):
    """`HERMES_CREDENTIALS_ENCRYPTION_KEY` is not set on this server."""


class CredentialsDecryptionError(CredentialsEncryptionError):
    """A stored ciphertext could not be decrypted with any configured key
    (wrong/rotated-out key, or the value was tampered with)."""


def _fernet() -> MultiFernet:
    current = os.environ.get(_ENCRYPTION_KEY_VAR)
    if not current:
        raise CredentialsEncryptionNotConfiguredError(
            f"{_ENCRYPTION_KEY_VAR} must be configured"
        )
    previous_raw = os.environ.get(_PREVIOUS_ENCRYPTION_KEYS_VAR, "")
    previous = [key.strip() for key in previous_raw.split(",") if key.strip()]
    try:
        return MultiFernet(
            [Fernet(key.encode("ascii")) for key in [current, *previous]]
        )
    except (ValueError, TypeError) as exc:
        raise CredentialsEncryptionNotConfiguredError(
            f"{_ENCRYPTION_KEY_VAR} (or a previous key) is not a valid Fernet key"
        ) from exc


def encrypt_credential(plaintext: str) -> str:
    """Encrypts `plaintext` under the current key. Always re-read at call
    time (see `_fernet()`) — never cached — so a mid-process key rotation
    (server restart) is the only way a new key ever takes effect, matching
    every other `HERMES_*` env-driven setting in this codebase."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_credential(ciphertext: str) -> str:
    """Decrypts `ciphertext`, trying the current key then every configured
    previous key in order. Raises `CredentialsDecryptionError` — never
    returns a fabricated/empty value — if none of them work."""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialsDecryptionError(
            "Stored credential could not be decrypted with any configured key"
        ) from exc


__all__ = [
    "CredentialsDecryptionError",
    "CredentialsEncryptionError",
    "CredentialsEncryptionNotConfiguredError",
    "decrypt_credential",
    "encrypt_credential",
]
