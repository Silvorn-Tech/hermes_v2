"""True-concurrency tests for idempotency: two separate DB connections, two
separate OS threads, hitting OrderService at (as close as achievable) the
same instant with the same Idempotency-Key. This is deliberately not just
"call reserve() twice in sequence" (already covered in test_idempotency.py
and test_order_service.py) — it exists to empirically prove the specific
claim that matters: `session.begin_nested()`'s SAVEPOINT insert against a
UNIQUE constraint makes a second, truly concurrent transaction *block* on
Postgres's row lock until the first transaction commits or rolls back,
rather than both transactions independently believing they're first.

If this guarantee were false, two concurrent requests with the same key
could both pass reserve() and both call Binance — the exact double-execution
this whole idempotency design exists to prevent.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models import Order
from hermes_v2.trading.order_service import OrderService

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


class _SynchronizedFakeClient:
    """A fake BinanceClient whose create_order() deliberately stalls, so a
    concurrent second reserve() attempt is guaranteed to happen while the
    first transaction is still open and uncommitted — the exact window
    where a broken idempotency implementation would let both through."""

    def __init__(
        self, entered_event: threading.Event, release_event: threading.Event
    ) -> None:
        self.market_data = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        self.balances = [{"asset": "USDT", "free": "100000", "locked": "0"}]
        self.create_order_calls: list[dict] = []
        self._entered_event = entered_event
        self._release_event = release_event

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def get_balances(self) -> list[dict]:
        return self.balances

    def get_trades(self, symbol: str) -> list[dict]:
        return []

    def create_order(self, **kwargs) -> dict:
        self.create_order_calls.append(kwargs)
        self._entered_event.set()
        # Hold this transaction open (uncommitted) long enough for the
        # second thread's reserve() attempt to genuinely race it.
        self._release_event.wait(timeout=5)
        return {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": kwargs.get("client_order_id"),
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "price": "0",
            "orig_qty": "0.01",
            "executed_qty": "0.01",
            "cummulative_quote_qty": "500.00",
            "transact_time": 1700000000000,
        }


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
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
    factory = sessionmaker(engine)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _configure_risk_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT")


def test_two_truly_concurrent_create_order_requests_produce_one_order(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as setup_session:
        user = User(email="concurrent@example.com")
        setup_session.add(user)
        setup_session.commit()
        user_id = user.id

    entered_binance_call = threading.Event()
    release_binance_call = threading.Event()
    client = _SynchronizedFakeClient(entered_binance_call, release_binance_call)

    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def _attempt(name: str) -> None:
        with session_factory() as session:
            service = OrderService(
                session, client, exchange_info_cache=ExchangeInfoCache()
            )
            try:
                results[name] = service.create_order(
                    user_id,
                    "BTCUSDT",
                    "BUY",
                    "MARKET",
                    Decimal("0.01"),
                    None,
                    "race-key",
                )
                session.commit()
            except BaseException as exc:  # noqa: BLE001 - test needs to see everything
                errors[name] = exc
                session.commit()

    thread_a = threading.Thread(target=_attempt, args=("a",))
    thread_a.start()

    # Wait until thread A is deep inside its (uncommitted) transaction,
    # blocked inside the simulated Binance call — this is the exact window
    # a race needs to land in.
    assert entered_binance_call.wait(timeout=5), "thread A never reached create_order"

    thread_b = threading.Thread(target=_attempt, args=("b",))
    thread_b.start()
    # Give thread B's reserve() a real chance to attempt (and block on) the
    # same row before we let A finish and commit.
    time.sleep(0.5)
    release_binance_call.set()

    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "thread A did not finish — deadlock?"
    assert not thread_b.is_alive(), "thread B did not finish — deadlock?"

    # The actual claim under test: Binance's create_order was called exactly
    # once, no matter how the two threads interleaved.
    assert len(client.create_order_calls) == 1

    # Both callers must get a real, matching answer — either their own
    # execution or the other thread's already-committed result — never a
    # silent failure and never two different order ids.
    assert not errors, f"unexpected exceptions: {errors}"
    assert results["a"]["order"]["id"] == results["b"]["order"]["id"]
    assert results["a"]["status"] == "FILLED"
    assert results["b"]["status"] == "FILLED"

    with session_factory() as verify_session:
        orders = verify_session.scalars(
            select(Order).where(Order.user_id == user_id)
        ).all()
        assert len(orders) == 1, (
            f"expected exactly one Order row, found {len(orders)} — "
            "idempotency failed to prevent a duplicate execution"
        )


def test_two_truly_concurrent_close_position_requests_produce_one_order(
    session_factory: sessionmaker[Session],
) -> None:
    """Same race, through close_position() specifically — its own
    idempotency scope (`POST /positions/{symbol}/close`) must be just as
    race-safe as create_order()'s."""
    with session_factory() as setup_session:
        user = User(email="concurrent-close@example.com")
        setup_session.add(user)
        setup_session.commit()
        user_id = user.id

    entered_binance_call = threading.Event()
    release_binance_call = threading.Event()
    client = _SynchronizedFakeClient(entered_binance_call, release_binance_call)
    client.balances = [
        {"asset": "USDT", "free": "100000", "locked": "0"},
        {"asset": "BTC", "free": "0.01", "locked": "0"},
    ]

    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def _attempt(name: str) -> None:
        with session_factory() as session:
            service = OrderService(
                session, client, exchange_info_cache=ExchangeInfoCache()
            )
            try:
                results[name] = service.close_position(
                    user_id, "BTCUSDT", "close-race-key"
                )
                session.commit()
            except BaseException as exc:  # noqa: BLE001
                errors[name] = exc
                session.commit()

    thread_a = threading.Thread(target=_attempt, args=("a",))
    thread_a.start()
    assert entered_binance_call.wait(timeout=5), "thread A never reached create_order"

    thread_b = threading.Thread(target=_attempt, args=("b",))
    thread_b.start()
    time.sleep(0.5)
    release_binance_call.set()

    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert len(client.create_order_calls) == 1
    assert not errors, f"unexpected exceptions: {errors}"
    assert results["a"]["order"]["id"] == results["b"]["order"]["id"]

    with session_factory() as verify_session:
        orders = verify_session.scalars(
            select(Order).where(Order.user_id == user_id)
        ).all()
        assert len(orders) == 1
