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
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceRequestError,
)
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.idempotency import IdempotencyConflictError
from hermes_v2.trading.models import AuditLogEntry, AuditResult, Order, OrderStatus
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
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(TradingDisabledError):
        service.create_order(
            user.id, "BTCUSDT", "BUY", "MARKET", Decimal("0.01"), None, "key-1"
        )
    session.commit()

    assert session.scalars(select(Order)).all() == []
    audit_rows = session.scalars(select(AuditLogEntry)).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].result == AuditResult.REJECTED


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
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(TradingDisabledError):
        service.cancel_order(user.id, uuid.uuid4(), "cancel-key-1")
    session.commit()


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
