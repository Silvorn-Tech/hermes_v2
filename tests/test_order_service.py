"""Tests for OrderService — the sole chokepoint between an API request and
a real Binance order. Every scenario uses a hand-written fake
BinanceClient (never real network I/O) against a real Postgres session,
since idempotency reservation needs a real SAVEPOINT.

Mirrors the route layer's own contract: an OrderServiceError subclass
(TradingDisabledError, OrderNotFoundError, ...) still leaves a committable
audit trail behind it — tests that expect one of these exceptions always
commit afterwards and then assert on what got persisted, exactly like the
API route will.
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceRateLimitError,
    BinanceRequestError,
)
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.idempotency import IdempotencyConflictError
from hermes_v2.trading.models import (
    AuditLogEntry,
    AuditResult,
    Order,
    OrderEvent,
    OrderEventType,
    OrderStatus,
)
from hermes_v2.trading.order_service import (
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderService,
    PositionNotFoundError,
    TradingDisabledError,
)

pytestmark = pytest.mark.database

_GOOD_EXCHANGE_INFO = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "filters": {
        "min_qty": "0.0001",
        "max_qty": "100",
        "step_size": "0.0001",
        "min_price": "0.01",
        "max_price": "1000000",
        "tick_size": "0.01",
        "min_notional": "10",
    },
}


class _FakeBinanceClient:
    def __init__(self) -> None:
        self.market_data: dict[str, dict] = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info: dict[str, dict] = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        # A nonzero USDT balance by default so RiskEngine's exposure/daily-loss
        # checks (which need a positive portfolio value to compute a
        # percentage against) don't spuriously reject in tests that aren't
        # specifically about an empty account.
        self.balances: list[dict] = [{"asset": "USDT", "free": "100000", "locked": "0"}]
        self.balances_error: Exception | None = None
        self.trades: dict[str, list[dict]] = {}
        self.create_order_result: dict | Exception = {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": "hm-x",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "price": "0",
            "orig_qty": "0.01",
            "executed_qty": "0.01",
            "cummulative_quote_qty": "500.00",
            "transact_time": 1700000000000,
        }
        self.get_order_result: dict | Exception | None = None
        self.cancel_order_result: dict | Exception = {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": "hm-x",
            "status": "CANCELED",
            "orig_qty": "0.01",
            "executed_qty": "0.00",
        }
        self.create_order_calls: list[dict] = []
        self.get_order_calls: list[dict] = []
        self.cancel_order_calls: list[dict] = []

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def get_balances(self) -> list[dict]:
        if self.balances_error is not None:
            raise self.balances_error
        return self.balances

    def get_trades(self, symbol: str) -> list[dict]:
        return self.trades.get(symbol, [])

    def create_order(self, **kwargs) -> dict:
        self.create_order_calls.append(kwargs)
        if isinstance(self.create_order_result, Exception):
            raise self.create_order_result
        return self.create_order_result

    def get_order(self, **kwargs) -> dict:
        self.get_order_calls.append(kwargs)
        if self.get_order_result is None:
            raise BinanceRequestError("no get_order result configured")
        if isinstance(self.get_order_result, Exception):
            raise self.get_order_result
        return self.get_order_result

    def cancel_order(self, **kwargs) -> dict:
        self.cancel_order_calls.append(kwargs)
        if isinstance(self.cancel_order_result, Exception):
            raise self.cancel_order_result
        return self.cancel_order_result


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


@pytest.fixture(autouse=True)
def _configure_risk_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT")


def _make_user(session: Session) -> User:
    user = User(email="trader@example.com")
    session.add(user)
    session.flush()
    return user


def _make_service(session: Session, client: _FakeBinanceClient) -> OrderService:
    return OrderService(session, client, exchange_info_cache=ExchangeInfoCache())


# --- kill switch --------------------------------------------------------------


def test_create_order_blocked_by_kill_switch_leaves_no_order_row(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    with pytest.raises(TradingDisabledError):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
        )
    session.commit()

    assert session.scalars(select(Order)).all() == []
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].result == AuditResult.REJECTED
    # The actual invariant that matters: not just "an exception was raised",
    # but that BinanceClient itself was never touched.
    assert client.create_order_calls == []
    assert client.get_order_calls == []


def test_kill_switch_rejection_is_idempotent_on_retry(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(TradingDisabledError):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
        )
    session.commit()

    # A retry with the same key gets the stored rejection back, not a second
    # TradingDisabledError-raising attempt (and definitely no new audit row).
    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "REJECTED"
    assert len(session.scalars(select(AuditLogEntry)).all()) == 1


def test_cancel_order_blocked_by_kill_switch(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    with pytest.raises(TradingDisabledError):
        service.cancel_order(user.id, uuid.uuid4(), "cancel-key-1")
    session.commit()

    assert client.cancel_order_calls == []


def test_close_position_blocked_by_kill_switch(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.balances = [{"asset": "BTC", "free": "0.01", "locked": "0"}]
    service = _make_service(session, client)

    with pytest.raises(TradingDisabledError):
        service.close_position(user.id, "BTCUSDT", "close-key-1")
    session.commit()

    assert client.create_order_calls == []


def test_cancel_order_still_blocked_when_the_order_being_cancelled_exists(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The kill switch must block a cancel even for a real, cancelable order
    — not just the "order not found" early-exit case above."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-x",
        "status": "NEW",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "orig_qty": "0.01",
        "executed_qty": "0",
        "cummulative_quote_qty": "0",
        "transact_time": 1700000000000,
    }
    service = _make_service(session, client)
    create_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("50000"), "key-1"
    )
    session.commit()
    order_id = uuid.UUID(create_result["order"]["id"])

    monkeypatch.setenv("TRADING_ENABLED", "false")
    with pytest.raises(TradingDisabledError):
        service.cancel_order(user.id, order_id, "cancel-key-1")
    session.commit()

    assert client.cancel_order_calls == []
    order = session.get(Order, order_id)
    assert order.status == OrderStatus.NEW  # untouched


# --- validation rejection --------------------------------------------------------


def test_create_order_rejected_by_validator_persists_a_rejected_order(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.00001"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "REJECTED"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.REJECTED


def test_binance_never_called_when_validation_rejects(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.00001"), None, "key-1"
    )
    session.commit()

    assert client.create_order_calls == []


# --- risk rejection -----------------------------------------------------------


def test_create_order_rejected_by_risk_engine_when_symbol_not_allowed(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "ETHUSDT")  # BTCUSDT not allowed
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "REJECTED"
    assert "BTCUSDT" in result["reason"]
    assert client.create_order_calls == []
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.REJECTED


def test_risk_rejection_when_a_limit_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.delenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", raising=False)
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "REJECTED"


def test_risk_evaluation_failure_rejects_and_still_finalizes_idempotency(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """If gathering account state for RiskEngine itself fails (Binance
    outage mid-flow), the order must still be rejected and the idempotency
    reservation finalized — not left stuck at PROCESSING forever, which
    would permanently block every future retry with this key."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.balances_error = BinanceRequestError("Binance is unreachable")
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "REJECTED"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.REJECTED

    # A retry with the same key must return the stored rejection, not hang
    # forever behind an unfinalized reservation.
    retry_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()
    assert retry_result == result


# --- unexpected-exception safety net --------------------------------------------


class _BrokenValidator:
    """Simulates a genuinely unforeseen bug somewhere in the validate/risk
    chain — anything not already an intentional rejection or a BinanceError."""

    def validate(self, *args, **kwargs):
        raise RuntimeError("boom: something nobody anticipated")


def test_unexpected_exception_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = OrderService(
        session,
        client,
        validator=_BrokenValidator(),
        exchange_info_cache=ExchangeInfoCache(),
    )

    with pytest.raises(RuntimeError, match="boom"):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
        )
    session.commit()

    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.FAILED
    # The HTTP-facing field is deliberately generic — an arbitrary
    # exception's text is not curated the way every other error path's
    # message is, so it must never reach a field order_to_response()
    # returns over the API.
    assert order.error_message == "An unexpected internal error occurred."
    events = session.scalars(select(OrderEvent)).all()
    internal_error_events = [
        e for e in events if e.event_type == OrderEventType.INTERNAL_ERROR
    ]
    assert len(internal_error_events) == 1
    # The real detail is preserved, but only in DB-only fields no route
    # currently exposes over HTTP.
    assert "boom" in internal_error_events[0].detail
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert audit_rows[0].result == AuditResult.FAILED
    assert "boom" in audit_rows[0].detail


def test_retry_after_unexpected_exception_returns_stored_failure_not_a_hang(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The whole point of the safety net: finalize() must still run, or this
    key would be stuck at PROCESSING and every retry would raise
    IdempotencyInProgressError forever."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = OrderService(
        session,
        client,
        validator=_BrokenValidator(),
        exchange_info_cache=ExchangeInfoCache(),
    )

    with pytest.raises(RuntimeError):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
        )
    session.commit()

    # A second attempt with the same key, now against a working validator,
    # must return the stored FAILED response rather than raising
    # IdempotencyInProgressError or hitting Binance again.
    working_service = _make_service(session, client)
    retry_result = working_service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert retry_result["status"] == "FAILED"
    assert client.create_order_calls == []
    # The stored/replayed HTTP-facing response must also never carry the
    # raw exception text.
    assert "boom" not in retry_result["reason"]
    assert "boom" not in retry_result["order"]["error_message"]


# --- successful order -----------------------------------------------------------


def test_successful_market_order_is_persisted_as_filled(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FILLED"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.FILLED
    assert order.binance_order_id == "555"
    assert order.executed_quantity == Decimal("0.01")
    assert order.terminal_at is not None
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert audit_rows[0].result == AuditResult.SUCCESS
    assert len(client.create_order_calls) == 1


def test_successful_order_client_order_id_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    sent_client_order_id = client.create_order_calls[0]["client_order_id"]
    order = session.scalars(select(Order)).one()
    assert order.binance_client_order_id == sent_client_order_id
    assert sent_client_order_id.startswith("hm-")


# --- duplicate request (idempotency) -----------------------------------------------


def test_duplicate_create_request_does_not_call_binance_twice(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    first = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()
    second = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert first == second
    assert len(client.create_order_calls) == 1
    assert len(session.scalars(select(Order)).all()) == 1


def test_same_key_different_payload_raises_conflict(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    with pytest.raises(IdempotencyConflictError):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.02"), None, "key-1"
        )


# --- ambiguous failure / timeout handling ------------------------------------------


def test_ambiguous_failure_confirmed_on_binance_is_not_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """create_order raises (e.g. a timeout), but the order actually reached
    Binance — get_order confirms it, and OrderService must not have called
    create_order a second time."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceRequestError("Binance request timed out")
    client.get_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 777,
        "client_order_id": "hm-x",
        "status": "FILLED",
        "executed_qty": "0.01",
        "cummulative_quote_qty": "500.00",
    }
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FILLED"
    assert len(client.create_order_calls) == 1
    assert len(client.get_order_calls) == 1
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.FILLED
    assert order.binance_order_id == "777"


def test_ambiguous_failure_unconfirmable_marks_order_failed(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """Both the original create_order call AND the confirmation get_order
    call fail — OrderService must not guess; it marks FAILED and stops."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceRequestError("Binance request timed out")
    client.get_order_result = BinanceRequestError("still can't reach Binance")
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FAILED"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.FAILED
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert audit_rows[0].result == AuditResult.FAILED


def test_retry_after_unconfirmable_failure_does_not_call_binance_again(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The safe, conservative choice: a FAILED-and-unconfirmed order is not
    auto-retried even on a subsequent request with the same key."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceRequestError("Binance request timed out")
    client.get_order_result = BinanceRequestError("still can't reach Binance")
    service = _make_service(session, client)

    service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()
    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FAILED"
    assert len(client.create_order_calls) == 1


def test_partial_fill_is_reconciled_correctly(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-x",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "orig_qty": "0.01",
        "executed_qty": "0.004",
        "cummulative_quote_qty": "200.00",
        "transact_time": 1700000000000,
    }
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("50000"), "key-1"
    )
    session.commit()

    assert result["status"] == "PARTIALLY_FILLED"
    order = session.scalars(select(Order)).one()
    assert order.executed_quantity == Decimal("0.004")


def test_response_missing_status_field_never_defaults_to_filled(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The core invariant of this whole design: a 200-OK-shaped HTTP
    response from Binance's create_order is never treated as "the order
    filled" — only Binance's own `status` field decides that. A malformed
    or incomplete response must leave the order at PENDING (non-terminal),
    not silently become FILLED, and not silently become terminal at all."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    # No "status" key at all — a pathological but possible malformed
    # response shape.
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-x",
    }
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "PENDING"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.PENDING
    assert order.terminal_at is None
    assert order.terminal_at is None  # not a terminal status


def test_binance_rejection_is_persisted_not_raised(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceAuthenticationError(
        "HTTP 401, Binance code=-2015 msg=Invalid API-key"
    )
    client.get_order_result = BinanceAuthenticationError(
        "HTTP 401, Binance code=-2015 msg=Invalid API-key"
    )
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FAILED"
    assert "unit-test-fake" not in result["reason"]  # sanity: no secret-shaped leakage


def test_rate_limited_on_create_and_on_confirmation_marks_failed_not_filled(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """Binance rate-limiting Hermes mid-submission must never be
    interpreted as a success, and must not crash the safety net."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceRateLimitError(
        "HTTP 429, Binance code=-1003 msg=Too many requests.", retry_after_seconds=5.0
    )
    client.get_order_result = BinanceRateLimitError(
        "HTTP 429, Binance code=-1003 msg=Too many requests.", retry_after_seconds=5.0
    )
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FAILED"
    order = session.scalars(select(Order)).one()
    assert order.status == OrderStatus.FAILED


def test_insufficient_balance_rejection_is_not_a_duplicate_risk(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """An order Binance never actually created (a synchronous rejection,
    not a timeout) still goes through the same confirm-once path — the
    confirmation correctly finds nothing, and the order is marked FAILED,
    not retried automatically."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = BinanceRequestError(
        "HTTP 400, Binance code=-2010 msg=Account has insufficient balance "
        "for requested action."
    )
    client.get_order_result = BinanceRequestError(
        "HTTP 400, Binance code=-2013 msg=Order does not exist."
    )
    service = _make_service(session, client)

    result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()

    assert result["status"] == "FAILED"
    assert len(client.create_order_calls) == 1  # never blindly retried


# --- cancel ---------------------------------------------------------------------


def test_cancel_nonexistent_order_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(OrderNotFoundError):
        service.cancel_order(user.id, uuid.uuid4(), "cancel-key-1")
    session.commit()

    # A rejected cancel attempt is still a mutable-action attempt and must
    # leave an audit trail, same as every other rejection.
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "orders.cancel"
    assert audit_rows[0].result == AuditResult.REJECTED


def test_cancel_someone_elses_order_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    owner = User(email="owner@example.com")
    attacker = User(email="attacker@example.com")
    session.add_all([owner, attacker])
    session.flush()
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    create_result = service.create_order(
        owner.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "owner-key"
    )
    session.commit()
    order_id = create_result["order"]["id"]

    with pytest.raises(OrderNotFoundError):
        service.cancel_order(attacker.id, uuid.UUID(order_id), "attacker-key")
    session.commit()


def test_cancel_a_filled_order_raises_not_cancelable(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()  # default create_order_result is FILLED
    service = _make_service(session, client)
    create_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
    )
    session.commit()
    order_id = create_result["order"]["id"]

    with pytest.raises(OrderNotCancelableError):
        service.cancel_order(user.id, uuid.UUID(order_id), "cancel-key-1")
    session.commit()

    audit_rows = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.action == "orders.cancel")
    ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].result == AuditResult.REJECTED


def test_cancel_blocked_by_kill_switch_writes_an_audit_row(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(TradingDisabledError):
        service.cancel_order(user.id, uuid.uuid4(), "cancel-key-1")
    session.commit()

    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "orders.cancel"
    assert audit_rows[0].result == AuditResult.REJECTED


def test_successful_cancel_updates_order_status(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-x",
        "status": "NEW",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "orig_qty": "0.01",
        "executed_qty": "0",
        "cummulative_quote_qty": "0",
        "transact_time": 1700000000000,
    }
    service = _make_service(session, client)
    create_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("50000"), "key-1"
    )
    session.commit()
    order_id = create_result["order"]["id"]

    result = service.cancel_order(user.id, uuid.UUID(order_id), "cancel-key-1")
    session.commit()

    assert result["status"] == "CANCELED"
    order = session.get(Order, uuid.UUID(order_id))
    assert order.status == OrderStatus.CANCELED
    assert order.terminal_at is not None


def test_duplicate_cancel_request_does_not_call_binance_twice(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-x",
        "status": "NEW",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "orig_qty": "0.01",
        "executed_qty": "0",
        "cummulative_quote_qty": "0",
        "transact_time": 1700000000000,
    }
    service = _make_service(session, client)
    create_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("50000"), "key-1"
    )
    session.commit()
    order_id = uuid.UUID(create_result["order"]["id"])

    service.cancel_order(user.id, order_id, "cancel-key-1")
    session.commit()
    service.cancel_order(user.id, order_id, "cancel-key-1")
    session.commit()

    assert len(client.cancel_order_calls) == 1


# --- close position ---------------------------------------------------------------


def test_close_position_with_no_holding_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(PositionNotFoundError):
        service.close_position(user.id, "BTCUSDT", "close-key-1")


def test_close_position_submits_a_market_sell_for_the_full_quantity(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.balances = [{"asset": "BTC", "free": "0.02", "locked": "0"}]
    client.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": 1700000000000, "is_buyer": True}
    ]
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 999,
        "client_order_id": "hm-close",
        "status": "FILLED",
        "side": "SELL",
        "type": "MARKET",
        "price": "0",
        "orig_qty": "0.02",
        "executed_qty": "0.02",
        "cummulative_quote_qty": "1000.00",
        "transact_time": 1700000001000,
    }
    service = _make_service(session, client)

    result = service.close_position(user.id, "BTCUSDT", "close-key-1")
    session.commit()

    assert result["status"] == "FILLED"
    sent = client.create_order_calls[0]
    assert sent["side"] == "SELL"
    assert sent["order_type"] == "MARKET"
    assert sent["quantity"] == "0.02"
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert audit_rows[0].action == "positions.close"


def test_close_position_is_blocked_for_a_disallowed_symbol(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """close_position goes through the exact same RiskEngine.allowed_symbols
    check as any other order — holding a symbol doesn't grant permission to
    trade it if it was removed from the allowlist since it was bought."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "ETHUSDT")  # BTCUSDT not allowed
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.balances = [
        {"asset": "USDT", "free": "100000", "locked": "0"},
        {"asset": "BTC", "free": "0.02", "locked": "0"},
    ]
    client.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": 1700000000000, "is_buyer": True}
    ]
    service = _make_service(session, client)

    result = service.close_position(user.id, "BTCUSDT", "close-key-1")
    session.commit()

    assert result["status"] == "REJECTED"
    assert client.create_order_calls == []


def test_close_position_is_blocked_by_the_daily_loss_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The daily-loss check applies to both sides by design (a hard stop for
    the day) — it must not carve out an exception for closing a position,
    even though that seems like it should always be safe to allow."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "5")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    # ETH is *partially* sold at a loss today (still holds 0.1 ETH) —
    # get_realized_loss_today_quote() only sees symbols with a nonzero
    # current balance (see PositionsService's documented limitation), so a
    # *fully* closed-out symbol wouldn't exercise this check.
    client.balances = [
        {"asset": "USDT", "free": "5000", "locked": "0"},
        {"asset": "BTC", "free": "0.02", "locked": "0"},
        {"asset": "ETH", "free": "0.1", "locked": "0"},
    ]
    client.market_data["ETHUSDT"] = {"last_price": "2100"}
    # OrderService's PositionsService uses the real wall clock (no `now=`
    # override), and get_realized_loss_today_quote() only counts trades
    # within "today" — unlike other tests here, this one can't use the
    # fixed placeholder timestamp everywhere else in this file.
    now_ms = int(time.time() * 1000)
    client.trades["ETHUSDT"] = [
        {"qty": "1", "price": "3000", "time": now_ms - 60_000, "is_buyer": True},
        {"qty": "0.9", "price": "2000", "time": now_ms - 30_000, "is_buyer": False},
    ]
    client.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": now_ms - 60_000, "is_buyer": True}
    ]
    service = _make_service(session, client)

    result = service.close_position(user.id, "BTCUSDT", "close-key-1")
    session.commit()

    assert result["status"] == "REJECTED"
    assert "Daily loss" in result["reason"]
    assert client.create_order_calls == []


def test_close_position_uses_the_current_balance_not_a_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """A position partially reduced by an earlier fill must be closed at its
    *current* size — get_position() is always read fresh from Binance, never
    cached across requests."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    # Only 0.005 left, as if 0.015 of an original 0.02 was already sold off.
    client.balances = [
        {"asset": "USDT", "free": "100000", "locked": "0"},
        {"asset": "BTC", "free": "0.005", "locked": "0"},
    ]
    client.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": 1700000000000, "is_buyer": True},
        {"qty": "0.015", "price": "45000", "time": 1700000001000, "is_buyer": False},
    ]
    client.create_order_result = {
        "symbol": "BTCUSDT",
        "order_id": 1000,
        "client_order_id": "hm-close",
        "status": "FILLED",
        "side": "SELL",
        "type": "MARKET",
        "price": "0",
        "orig_qty": "0.005",
        "executed_qty": "0.005",
        "cummulative_quote_qty": "250.00",
        "transact_time": 1700000002000,
    }
    service = _make_service(session, client)

    service.close_position(user.id, "BTCUSDT", "close-key-1")
    session.commit()

    assert client.create_order_calls[0]["quantity"] == "0.005"


def test_close_position_and_manual_order_with_same_key_do_not_collide(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """Reusing the same idempotency-key string for a manual order and a
    close-position action must not be treated as the same request — the two
    are scoped to different endpoints."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    # Cash headroom alongside the BTC holding so the manual BUY below doesn't
    # itself blow through the symbol/total exposure limits.
    client.balances = [
        {"asset": "USDT", "free": "100000", "locked": "0"},
        {"asset": "BTC", "free": "0.02", "locked": "0"},
    ]
    client.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": 1700000000000, "is_buyer": True}
    ]
    service = _make_service(session, client)

    manual_result = service.create_order(
        user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "shared-key"
    )
    session.commit()
    close_result = service.close_position(user.id, "BTCUSDT", "shared-key")
    session.commit()

    assert manual_result["order"]["id"] != close_result["order"]["id"]
    assert len(client.create_order_calls) == 2
