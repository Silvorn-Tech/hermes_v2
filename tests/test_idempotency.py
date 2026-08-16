"""Tests for the two-layer idempotency design.

Pure-function tests (hashing, clientOrderId derivation) run everywhere.
`reserve`/`finalize` need a real transaction with SAVEPOINT support, so
those are `@pytest.mark.database` against Postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    compute_request_hash,
    derive_binance_client_order_id,
    finalize,
    reserve,
)

# --- pure functions, no DB ----------------------------------------------------


def test_compute_request_hash_is_stable_regardless_of_key_order() -> None:
    first = compute_request_hash({"symbol": "BTCUSDT", "quantity": "0.01"})
    second = compute_request_hash({"quantity": "0.01", "symbol": "BTCUSDT"})
    assert first == second


def test_compute_request_hash_differs_for_different_payloads() -> None:
    first = compute_request_hash({"symbol": "BTCUSDT", "quantity": "0.01"})
    second = compute_request_hash({"symbol": "BTCUSDT", "quantity": "0.02"})
    assert first != second


def test_derive_binance_client_order_id_is_deterministic() -> None:
    user_id = uuid.uuid4()
    first = derive_binance_client_order_id(user_id, "my-key")
    second = derive_binance_client_order_id(user_id, "my-key")
    assert first == second


def test_derive_binance_client_order_id_differs_per_user() -> None:
    key = "same-key"
    first = derive_binance_client_order_id(uuid.uuid4(), key)
    second = derive_binance_client_order_id(uuid.uuid4(), key)
    assert first != second


def test_derive_binance_client_order_id_fits_binance_length_limit() -> None:
    client_order_id = derive_binance_client_order_id(uuid.uuid4(), "a-fairly-long-key")
    assert len(client_order_id) <= 36
    assert client_order_id.startswith("hm-")


# --- reserve()/finalize() against Postgres ------------------------------------


pytestmark = pytest.mark.database


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE audit_log, idempotency_keys, order_events, orders, "
                "role_permissions, user_roles, identities, sessions, permissions, "
                "roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def _make_user(session: Session) -> User:
    user = User(email="trader@example.com")
    session.add(user)
    session.flush()
    return user


def test_first_reservation_is_new(session: Session) -> None:
    user = _make_user(session)
    session.commit()

    result = reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})
    session.commit()

    assert result.is_new is True
    assert result.stored_response is None


def test_retry_with_same_payload_after_finalize_returns_stored_response(
    session: Session,
) -> None:
    user = _make_user(session)
    session.commit()

    first = reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})
    finalize(session, first.key_row_id, {"status": "FILLED", "order_id": "abc"})
    session.commit()

    second = reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})
    session.commit()

    assert second.is_new is False
    assert second.stored_response == {"status": "FILLED", "order_id": "abc"}


def test_retry_with_a_different_payload_raises_conflict(session: Session) -> None:
    user = _make_user(session)
    session.commit()

    first = reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})
    finalize(session, first.key_row_id, {"status": "FILLED"})
    session.commit()

    with pytest.raises(IdempotencyConflictError):
        reserve(session, user.id, "POST /orders", "key-1", {"symbol": "ETHUSDT"})


def test_reservation_without_finalize_blocks_a_concurrent_duplicate(
    session: Session,
) -> None:
    user = _make_user(session)
    session.commit()

    reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})
    session.commit()  # first request's transaction commits, but never finalizes

    with pytest.raises(IdempotencyInProgressError):
        reserve(session, user.id, "POST /orders", "key-1", {"symbol": "BTCUSDT"})


def test_same_key_is_independent_across_different_endpoints(session: Session) -> None:
    user = _make_user(session)
    session.commit()

    create_reservation = reserve(
        session, user.id, "POST /orders", "same-key", {"symbol": "BTCUSDT"}
    )
    finalize(session, create_reservation.key_row_id, {"status": "FILLED"})
    session.commit()

    cancel_reservation = reserve(
        session, user.id, "POST /orders/{id}/cancel", "same-key", {"order_id": "abc"}
    )
    session.commit()

    assert cancel_reservation.is_new is True


def test_same_key_is_independent_across_different_users(session: Session) -> None:
    first_user = User(email="first@example.com")
    second_user = User(email="second@example.com")
    session.add_all([first_user, second_user])
    session.flush()
    session.commit()

    first_reservation = reserve(
        session, first_user.id, "POST /orders", "shared-key", {"symbol": "BTCUSDT"}
    )
    finalize(session, first_reservation.key_row_id, {"status": "FILLED"})
    session.commit()

    second_reservation = reserve(
        session, second_user.id, "POST /orders", "shared-key", {"symbol": "BTCUSDT"}
    )
    session.commit()

    assert second_reservation.is_new is True
