"""Integration tests for SimulationOrderService against real Postgres:
fee/slippage application, RiskEngine rejection (no fill), insufficient
virtual balance, idempotency (duplicate request never double-fills), and
the kill switch. Mirrors test_order_service.py's fixture shape.
"""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.bot_service import BotService
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models import Bot, SimulationAccount, SimulationOrder
from hermes_v2.trading.order_service import TradingDisabledError
from hermes_v2.trading.risk_engine import RiskLimits
from hermes_v2.trading.simulation_order_service import SimulationOrderService
from hermes_v2.trading.user_risk_settings_service import (
    save_user_simulation_risk_limits,
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


class _FakeClient:
    def __init__(self) -> None:
        self.market_data = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        self.create_order_calls: list[dict] = []
        self.cancel_order_calls: list[dict] = []

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def create_order(self, **kwargs) -> dict:  # pragma: no cover - must never run
        self.create_order_calls.append(kwargs)
        raise AssertionError("SimulationOrderService must never call create_order")

    def cancel_order(self, **kwargs) -> dict:  # pragma: no cover - must never run
        self.cancel_order_calls.append(kwargs)
        raise AssertionError("SimulationOrderService must never call cancel_order")


@pytest.fixture()
def session_factory() -> sessionmaker:
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
def _risk_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")


_PERMISSIVE_SIMULATION_RISK_LIMITS = RiskLimits(
    max_order_notional_quote=Decimal("10000"),
    max_symbol_exposure_pct=Decimal("100"),
    max_total_exposure_pct=Decimal("100"),
    max_daily_loss_pct=Decimal("100"),
    max_open_positions=10,
    allowed_symbols=frozenset({"BTCUSDT"}),
)


def _create_bot(
    session_factory: sessionmaker, client, *, target_quantity="0.02"
) -> tuple:
    with session_factory() as session:
        user = User(email="sim-order-test@example.com")
        session.add(user)
        session.commit()
        user_id = user.id
        save_user_simulation_risk_limits(
            session, user_id, _PERMISSIVE_SIMULATION_RISK_LIMITS
        )
        session.commit()

        bot_service = BotService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        created = bot_service.create_bot(
            user_id=user_id,
            name="Sim Order Test Bot",
            risk_profile="SENTINEL",
            asset_class="CRYPTO",
            execution_venue="BINANCE",
            instrument="BTCUSDT",
            target_quantity=Decimal(target_quantity),
            idempotency_key="setup-create",
        )
        session.commit()
        return user_id, created["bot"]["id"]


# --- fees / slippage ---------------------------------------------------------------


def test_slippage_moves_the_fill_price_against_a_buy(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_SLIPPAGE_RATE_PCT", "1")
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        result = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.02"),
            idempotency_key="buy-1",
        )
        session.commit()

    assert result["status"] == "FILLED"
    # 1% slippage against a BUY: fill price is worse (higher) than 50000.
    assert Decimal(result["order"]["fill_price"]) == Decimal("50500")


def test_slippage_moves_the_fill_price_against_a_sell(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        bot.position.current_quantity = Decimal("0.02")
        session.commit()

    monkeypatch.setenv("HERMES_SIMULATION_SLIPPAGE_RATE_PCT", "1")
    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        result = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="SELL",
            quantity=Decimal("0.02"),
            idempotency_key="sell-1",
        )
        session.commit()

    assert result["status"] == "FILLED"
    # 1% slippage against a SELL: fill price is worse (lower) than 50000.
    assert Decimal(result["order"]["fill_price"]) == Decimal("49500")


def test_fee_is_deducted_from_cash_on_a_buy(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_FEE_RATE_PCT", "0.1")
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        result = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.02"),
            idempotency_key="buy-fee",
        )
        session.commit()

        account = session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot_id)
        )
        # notional = 1000, fee = 0.1% of 1000 = 1; cash = 10000 - 1000 - 1
        assert Decimal(account.cash_balance_quote) == Decimal("8999")
        assert Decimal(result["order"]["fee_quote"]) == Decimal("1")


# --- RiskEngine rejection (no fill) -------------------------------------------------


def test_risk_engine_rejection_produces_no_fill(
    session_factory: sessionmaker,
) -> None:
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        save_user_simulation_risk_limits(
            session,
            user_id,
            replace(
                _PERMISSIVE_SIMULATION_RISK_LIMITS,
                max_order_notional_quote=Decimal("1"),
            ),
        )
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        result = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.02"),
            idempotency_key="risk-reject",
        )
        session.commit()

        assert result["status"] == "REJECTED"
        account = session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot_id)
        )
        assert Decimal(account.cash_balance_quote) == Decimal("10000")
        assert bot.position.current_quantity == Decimal("0")


# --- insufficient virtual balance ---------------------------------------------------


def test_insufficient_virtual_balance_is_rejected_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    # A fee rate that pushes the projected cost over the $10,000 virtual
    # account while the underlying notional (9900) stays comfortably
    # under the $10,000 RiskEngine order-notional limit -- isolating the
    # virtual-balance guard from the (already separately tested)
    # RiskEngine rejection path.
    monkeypatch.setenv("HERMES_SIMULATION_FEE_RATE_PCT", "5")
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        result = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.198"),
            idempotency_key="insufficient",
        )
        session.commit()

    assert result["status"] == "REJECTED"
    assert "Insufficient virtual balance" in result["reason"]


# --- idempotency ---------------------------------------------------------------------


def test_duplicate_request_never_produces_a_second_fill(
    session_factory: sessionmaker,
) -> None:
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        first = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.02"),
            idempotency_key="dup-key",
        )
        session.commit()

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        second = service.place_bot_order(
            user_id=user_id,
            bot=bot,
            side="BUY",
            quantity=Decimal("0.02"),
            idempotency_key="dup-key",
        )
        session.commit()

    assert first == second
    with session_factory() as session:
        orders = session.scalars(
            select(SimulationOrder).where(SimulationOrder.bot_id == bot_id)
        ).all()
        assert len(orders) == 1


# --- kill switch ---------------------------------------------------------------------


def test_kill_switch_off_blocks_a_simulation_fill_too(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    client = _FakeClient()
    user_id, bot_id = _create_bot(session_factory, client)

    monkeypatch.setenv("TRADING_ENABLED", "false")
    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        service = SimulationOrderService(
            session, client, exchange_info_cache=ExchangeInfoCache()
        )
        with pytest.raises(TradingDisabledError):
            service.place_bot_order(
                user_id=user_id,
                bot=bot,
                side="BUY",
                quantity=Decimal("0.02"),
                idempotency_key="kill-switch",
            )
        session.commit()

        account = session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot_id)
        )
        assert Decimal(account.cash_balance_quote) == Decimal("10000")
        assert (
            session.scalars(
                select(SimulationOrder).where(SimulationOrder.bot_id == bot_id)
            ).all()
            == []
        )
