"""True-concurrency test for BotService: two separate DB connections, two
OS threads, calling `pause()` on the *same* ACTIVE bot with *different*
Idempotency-Key values. This specifically targets the gap plain
idempotency doesn't cover — `reserve()` only serializes requests sharing
the identical key, so two differently-keyed concurrent requests (e.g. two
browser tabs) would both pass their own `reserve()` and could both read
the bot as ACTIVE and both submit a sell, without the `SELECT ... FOR
UPDATE` row lock `BotService._load_bot_for_update` takes.

Mirrors tests/test_idempotency_concurrency.py's threading pattern.
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
from hermes_v2.trading.bot_service import BotService, InvalidBotTransitionError
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models import Bot, BotExecutionMode, Order

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
    """create_order() deliberately stalls, holding its (uncommitted)
    transaction — and therefore the FOR UPDATE row lock — open long
    enough for a second thread's own FOR UPDATE read to genuinely race
    it."""

    def __init__(
        self, entered_event: threading.Event, release_event: threading.Event
    ) -> None:
        self.market_data = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        self.balances = [
            {"asset": "USDT", "free": "100000", "locked": "0"},
            {"asset": "BTC", "free": "0.015", "locked": "0"},
        ]
        self.create_order_calls: list[dict] = []
        self._entered_event = entered_event
        self._release_event = release_event
        self._stall_next = True

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
        if self._stall_next:
            self._stall_next = False
            self._entered_event.set()
            self._release_event.wait(timeout=5)
        return {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": kwargs.get("client_order_id"),
            "status": "FILLED",
            "side": kwargs.get("side"),
            "type": "MARKET",
            "price": "0",
            "orig_qty": kwargs.get("quantity"),
            "executed_qty": kwargs.get("quantity"),
            "cummulative_quote_qty": "750.00",
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
                "TRUNCATE TABLE bot_positions, bots, audit_log, idempotency_keys, "
                "order_events, orders, role_permissions, user_roles, identities, "
                "sessions, permissions, roles, users CASCADE"
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


def test_two_differently_keyed_concurrent_pauses_produce_one_sell(
    session_factory: sessionmaker[Session],
) -> None:
    entered_binance_call = threading.Event()
    release_binance_call = threading.Event()
    client = _SynchronizedFakeClient(entered_binance_call, release_binance_call)

    with session_factory() as setup_session:
        user = User(email="bot-concurrent@example.com")
        setup_session.add(user)
        setup_session.commit()
        user_id = user.id

        setup_service = BotService(
            setup_session, client, exchange_info_cache=ExchangeInfoCache()
        )
        created = setup_service.create_bot(
            user_id=user_id,
            name="Race Bot",
            risk_profile="SENTINEL",
            asset_class="CRYPTO",
            execution_venue="BINANCE",
            instrument="BTCUSDT",
            target_quantity=Decimal("0.015"),
            idempotency_key="setup-create",
        )
        setup_session.commit()
        bot_id = created["bot"]["id"]

        # Every bot is created SIMULATION now — this test specifically
        # targets OrderService's real-Binance-calling FOR UPDATE race (see
        # module docstring), which SimulationOrderService's own client
        # calls don't stall on; flip to LIVE so the stalling
        # _SynchronizedFakeClient.create_order() is actually reached.
        bot_row = setup_session.get(Bot, bot_id)
        bot_row.execution_mode = BotExecutionMode.LIVE
        setup_session.commit()

        activated = setup_service.resume(user_id, bot_id, "setup-activate")
        setup_session.commit()
        assert activated["status"] == "ACTIVE"

    client.create_order_calls.clear()  # drop the setup resume's own BUY call
    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def _attempt(name: str, key: str) -> None:
        with session_factory() as session:
            service = BotService(
                session, client, exchange_info_cache=ExchangeInfoCache()
            )
            try:
                results[name] = service.pause(user_id, bot_id, key)
                session.commit()
            except BaseException as exc:  # noqa: BLE001 - test needs to see everything
                errors[name] = exc
                session.commit()

    thread_a = threading.Thread(target=_attempt, args=("a", "pause-key-a"))
    thread_a.start()

    assert entered_binance_call.wait(timeout=5), "thread A never reached create_order"

    thread_b = threading.Thread(target=_attempt, args=("b", "pause-key-b"))
    thread_b.start()
    # Give thread B's FOR UPDATE read a real chance to attempt (and block
    # on) the same row before we let A finish and commit.
    time.sleep(0.5)
    release_binance_call.set()

    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "thread A did not finish — deadlock?"
    assert not thread_b.is_alive(), "thread B did not finish — deadlock?"

    # The actual claim under test: exactly one sell reached Binance, no
    # matter how the two threads interleaved.
    assert len(client.create_order_calls) == 1

    # Thread A (which won the row lock first) succeeded; thread B, once
    # unblocked, correctly saw the bot was no longer ACTIVE and rejected
    # cleanly rather than attempting a second sell.
    assert "a" in results and results["a"]["status"] == "PAUSED"
    assert "b" in errors
    assert isinstance(errors["b"], InvalidBotTransitionError)

    with session_factory() as verify_session:
        sell_orders = verify_session.scalars(
            select(Order).where(Order.user_id == user_id, Order.side == "SELL")
        ).all()
        assert len(sell_orders) == 1, (
            f"expected exactly one SELL order, found {len(sell_orders)} — "
            "the FOR UPDATE lock failed to serialize the two concurrent pauses"
        )
