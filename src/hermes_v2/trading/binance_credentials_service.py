"""A user's own connected Binance account.

Verify-before-persist: `connect_credentials()` takes a `BinanceClient`
already constructed from whatever the user just submitted (the caller's
job, same dependency-injection convention every other service in this
package already follows — `BotService`/`OrderService`/`PortfolioService`
all take a `client`, none construct their own) and calls
`get_account_info()` against real Binance *before* anything is written —
the same "does this key actually work" philosophy `cli.py`'s
`binance_check()` already applies to the one operator-level key, now
enforced automatically for every user's own key rather than left to an
operator reading a diagnostic by hand. A key with withdrawals enabled is
rejected outright (`CredentialsUnsafeError`) rather than accepted with a
warning — `binance_check()` only ever flagged that case as `UNSAFE`
advisory output; this is the same check with real consequences.

Only `api_key_ciphertext`/`api_secret_ciphertext` (via
`credentials_encryption.py`) are ever stored — plaintext exists only for
the duration of the connect request and the duration of a later
`get_decrypted_client()` call, never persisted, never logged, never
included in any response body.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient, BinanceError
from hermes_v2.trading.credentials_encryption import (
    decrypt_credential,
    encrypt_credential,
)
from hermes_v2.trading.models.binance_credential import UserBinanceCredential


class BinanceCredentialsError(RuntimeError):
    """Base class for every error this module raises."""


class CredentialsNotConfiguredError(BinanceCredentialsError):
    """This user has no connected Binance account."""


class CredentialsVerificationFailedError(BinanceCredentialsError):
    """Binance rejected the submitted key/secret."""


class CredentialsUnsafeError(BinanceCredentialsError):
    """The submitted key has withdrawals enabled -- Hermes only accepts
    trade/read-only keys, the same bar `cli.py`'s `binance_check()`
    already holds the one operator key to."""


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    api_key_last4: str | None
    verified_at: datetime | None
    updated_at: datetime | None


def _get_or_none(session: Session, user_id: uuid.UUID) -> UserBinanceCredential | None:
    return session.scalar(
        select(UserBinanceCredential).where(UserBinanceCredential.user_id == user_id)
    )


def get_credential_status(session: Session, user_id: uuid.UUID) -> CredentialStatus:
    row = _get_or_none(session, user_id)
    if row is None:
        return CredentialStatus(
            configured=False, api_key_last4=None, verified_at=None, updated_at=None
        )
    return CredentialStatus(
        configured=True,
        api_key_last4=row.api_key_last4,
        verified_at=row.verified_at,
        updated_at=row.updated_at,
    )


def connect_credentials(
    session: Session,
    user_id: uuid.UUID,
    client: BinanceClient,
    api_key: str,
    api_secret: str,
) -> CredentialStatus:
    """`client` must already be constructed from `api_key`/`api_secret`
    (the caller's responsibility, matching every other service in this
    package) — passed separately here only because the plaintext values
    themselves need to be encrypted and stored, not because this function
    builds its own client."""
    try:
        account = client.get_account_info()
    except BinanceError as exc:
        raise CredentialsVerificationFailedError(str(exc)) from exc

    if account.get("can_withdraw") is not False:
        raise CredentialsUnsafeError(
            "This API key has withdrawals enabled. Create a key with "
            "withdrawals disabled and try again."
        )

    row = _get_or_none(session, user_id)
    if row is None:
        row = UserBinanceCredential(user_id=user_id)
        session.add(row)

    row.api_key_ciphertext = encrypt_credential(api_key)
    row.api_secret_ciphertext = encrypt_credential(api_secret)
    row.api_key_last4 = api_key[-4:]
    row.verified_at = datetime.now(UTC)
    session.flush()

    return get_credential_status(session, user_id)


def disconnect_credentials(session: Session, user_id: uuid.UUID) -> None:
    """Idempotent -- disconnecting an already-disconnected account is a
    no-op, not an error."""
    row = _get_or_none(session, user_id)
    if row is not None:
        session.delete(row)
        session.flush()


def get_decrypted_client(session: Session, user_id: uuid.UUID) -> BinanceClient:
    """Raises `CredentialsNotConfiguredError` if this user has never
    connected an account -- callers map that to a clear, structured
    "connect your account" response, never a generic 500."""
    row = _get_or_none(session, user_id)
    if row is None:
        raise CredentialsNotConfiguredError(
            f"No Binance credentials for user {user_id}"
        )
    api_key = decrypt_credential(row.api_key_ciphertext)
    api_secret = decrypt_credential(row.api_secret_ciphertext)
    return BinanceClient(api_key=api_key, api_secret=api_secret)


__all__ = [
    "BinanceCredentialsError",
    "CredentialStatus",
    "CredentialsNotConfiguredError",
    "CredentialsUnsafeError",
    "CredentialsVerificationFailedError",
    "connect_credentials",
    "disconnect_credentials",
    "get_credential_status",
    "get_decrypted_client",
]
