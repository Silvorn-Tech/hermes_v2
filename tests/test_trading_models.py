"""PostgreSQL integration tests for the trading domain schema."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.models import (
    AuditLogEntry,
    AuditResult,
    IdempotencyKey,
    Order,
    OrderEvent,
    OrderEventType,
    OrderSide,
    OrderStatus,
    OrderType,
)

pytestmark = pytest.mark.database


def test_trading_models_package_exports_expected_tables() -> None:
    expected_tables = {"orders", "order_events", "idempotency_keys", "audit_log"}
    assert {
        table.name for table in Order.__table__.metadata.tables.values()
    } >= expected_tables


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


def _make_user(session: Session, email: str = "trader@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def _make_order(user: User, client_order_id: str = "hm-test-1") -> Order:
    return Order(
        user_id=user.id,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        requested_quantity=Decimal("0.01"),
        binance_client_order_id=client_order_id,
    )


def test_order_can_be_created_with_defaults(session: Session) -> None:
    user = _make_user(session)
    order = _make_order(user)
    session.add(order)
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert order.executed_quantity == Decimal("0")
    assert order.binance_order_id is None


def test_binance_client_order_id_is_unique(session: Session) -> None:
    user = _make_user(session)
    session.add(_make_order(user, client_order_id="hm-dup"))
    session.commit()

    session.add(_make_order(user, client_order_id="hm-dup"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_user_cascades_to_their_orders(session: Session) -> None:
    user = _make_user(session)
    order = _make_order(user)
    session.add(order)
    session.commit()
    order_id = order.id

    session.delete(user)
    session.commit()

    assert session.get(Order, order_id) is None


def test_order_events_belong_to_an_order_and_cascade_on_delete(
    session: Session,
) -> None:
    user = _make_user(session)
    order = _make_order(user)
    session.add(order)
    session.flush()

    event = OrderEvent(
        order_id=order.id,
        event_type=OrderEventType.SUBMITTED,
        detail="Submitted to Binance",
    )
    session.add(event)
    session.commit()

    session.refresh(order)
    assert len(order.events) == 1
    assert order.events[0].event_type == OrderEventType.SUBMITTED

    event_id = event.id
    session.delete(order)
    session.commit()

    assert session.get(OrderEvent, event_id) is None


def test_idempotency_key_is_unique_per_user_endpoint_and_key(session: Session) -> None:
    user = _make_user(session)
    key = IdempotencyKey(
        user_id=user.id,
        endpoint="POST /orders",
        idempotency_key="client-key-1",
        request_hash="a" * 64,
        response_snapshot={"status": "FILLED"},
    )
    session.add(key)
    session.commit()

    duplicate = IdempotencyKey(
        user_id=user.id,
        endpoint="POST /orders",
        idempotency_key="client-key-1",
        request_hash="b" * 64,
        response_snapshot={"status": "FILLED"},
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()


def test_idempotency_key_scoped_per_endpoint_allows_reuse_across_endpoints(
    session: Session,
) -> None:
    """The same caller-chosen key string is fine on two different endpoints —
    the uniqueness scope is (user, endpoint, key), not just (user, key)."""
    user = _make_user(session)
    session.add(
        IdempotencyKey(
            user_id=user.id,
            endpoint="POST /orders",
            idempotency_key="same-key",
            request_hash="a" * 64,
            response_snapshot={},
        )
    )
    session.add(
        IdempotencyKey(
            user_id=user.id,
            endpoint="POST /orders/{id}/cancel",
            idempotency_key="same-key",
            request_hash="b" * 64,
            response_snapshot={},
        )
    )
    session.commit()  # no IntegrityError


def test_audit_log_entry_can_be_created_without_a_hermes_order(
    session: Session,
) -> None:
    """A rejected order (validation/risk failure) may never get a Hermes
    order row at all — audit_log must still be able to record it."""
    user = _make_user(session)
    entry = AuditLogEntry(
        user_id=user.id,
        action="orders.create",
        symbol="BTCUSDT",
        quantity=Decimal("0.01"),
        result=AuditResult.REJECTED,
        detail="HERMES_RISK_ALLOWED_SYMBOLS is not configured",
    )
    session.add(entry)
    session.commit()

    session.refresh(entry)
    assert entry.hermes_order_id is None
    assert entry.binance_order_id is None
    assert entry.result == AuditResult.REJECTED


def test_audit_log_never_stores_a_credential_shaped_value(session: Session) -> None:
    """Not a security scanner — just confirms the model has no column that
    would tempt a future caller into passing a secret through it."""
    column_names = {column.name for column in AuditLogEntry.__table__.columns}
    assert "api_key" not in column_names
    assert "api_secret" not in column_names
    assert "headers" not in column_names
    assert "raw_response" not in column_names
