"""Idempotency for every mutating trading action.

Two layers, per the architecture doc:

1. **Hermes DB** (`reserve`/`finalize` below) — a caller-supplied
   `Idempotency-Key` header is reserved as a row in `idempotency_keys`
   *before* any Binance call happens, using a `SAVEPOINT` (`begin_nested`)
   so a concurrent duplicate request racing the same key hits the table's
   unique constraint rather than a check-then-act gap. A request that loses
   the race either gets the already-completed response back (a true retry)
   or a clear "still in progress" error (a genuine concurrent duplicate) —
   never a second execution.
2. **Binance `newClientOrderId`** (`derive_binance_client_order_id`) —
   deterministic from `(user_id, idempotency_key)`, so even if the Hermes
   layer were somehow bypassed, Binance itself rejects a duplicate
   `clientOrderId`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hermes_v2.trading.models import IdempotencyKey

_PROCESSING_SENTINEL: dict[str, Any] = {"status": "PROCESSING"}
_CLIENT_ORDER_ID_PREFIX = "hm-"
_CLIENT_ORDER_ID_HASH_LENGTH = (
    32  # "hm-" + 32 hex chars = 35, within Binance's 36-char cap
)


class IdempotencyConflictError(RuntimeError):
    """The same (user, endpoint, key) was reused with a different request body."""


class IdempotencyInProgressError(RuntimeError):
    """The same (user, endpoint, key) is currently being processed by another
    in-flight request — never safe to proceed concurrently."""


def compute_request_hash(payload: dict[str, Any]) -> str:
    """A stable hash of a request body, independent of key ordering."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_binance_client_order_id(user_id: uuid.UUID, idempotency_key: str) -> str:
    """Deterministic `newClientOrderId` for a given user + idempotency key.

    Same inputs always produce the same output, so a retried request (same
    key) that somehow reached `BinanceClient.create_order` twice would send
    the identical `clientOrderId` both times — Binance itself then refuses
    to create a second order for it.
    """
    digest = hashlib.sha256(f"{user_id}:{idempotency_key}".encode("utf-8")).hexdigest()
    return f"{_CLIENT_ORDER_ID_PREFIX}{digest[:_CLIENT_ORDER_ID_HASH_LENGTH]}"


@dataclass(frozen=True)
class IdempotencyReservation:
    key_row_id: uuid.UUID
    is_new: bool
    stored_response: dict[str, Any] | None = None


def reserve(
    session: Session,
    user_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> IdempotencyReservation:
    """Claim `idempotency_key` for this (user, endpoint), or return the
    outcome of whoever claimed it first.

    Raises `IdempotencyConflictError` if the same key was already used with a
    *different* request body, and `IdempotencyInProgressError` if another
    request using the same key is still being processed (no stored response
    yet) — both cases must stop the caller before it ever reaches Binance.
    """
    request_hash = compute_request_hash(request_payload)
    reservation = IdempotencyKey(
        user_id=user_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_snapshot=dict(_PROCESSING_SENTINEL),
    )
    try:
        with session.begin_nested():
            session.add(reservation)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.endpoint == endpoint,
                IdempotencyKey.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise  # the unique constraint fired for a reason other than a duplicate key

        if existing.response_snapshot == _PROCESSING_SENTINEL:
            raise IdempotencyInProgressError(
                f"A request with idempotency key {idempotency_key!r} for "
                f"{endpoint!r} is already being processed."
            ) from None

        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                f"Idempotency key {idempotency_key!r} was already used for "
                f"{endpoint!r} with a different request body."
            ) from None

        return IdempotencyReservation(
            key_row_id=existing.id,
            is_new=False,
            stored_response=existing.response_snapshot,
        )

    return IdempotencyReservation(
        key_row_id=reservation.id, is_new=True, stored_response=None
    )


def finalize(
    session: Session, key_row_id: uuid.UUID, response_snapshot: dict[str, Any]
) -> None:
    """Record the final outcome for a reservation made by `reserve()`.

    Must be called exactly once per reservation, whether the outcome was a
    success, a validation/risk rejection, or a Binance failure — an
    unfinalized row stays stuck at `PROCESSING` forever and blocks every
    future retry with `IdempotencyInProgressError`, which is the safe
    failure mode (never silently allowing a second execution) but should
    still never happen in practice; `OrderService` finalizes in a `finally`
    block.
    """
    row = session.get(IdempotencyKey, key_row_id)
    if row is None:
        raise ValueError(f"No idempotency_keys row with id={key_row_id}")
    row.response_snapshot = response_snapshot
    session.flush()


__all__ = [
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "IdempotencyReservation",
    "compute_request_hash",
    "derive_binance_client_order_id",
    "finalize",
    "reserve",
]
