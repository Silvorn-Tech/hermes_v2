"""Integration tests for take_simulation_snapshot against real Postgres:
correct values, restart-safety (the same bucket never gets a second
row), fail-closed on a market-data error, and skipping a bot with no
SimulationAccount (a LIVE bot).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceError
from hermes_v2.trading.bot_service import BotService
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models import Bot, BotExecutionMode, SimulationSnapshot
from hermes_v2.trading.simulation_snapshot_service import take_simulation_snapshot

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

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]


class _NoMarketDataClient(_FakeClient):
    def get_market_data(self, symbol: str) -> dict:
        raise BinanceError("market data unavailable")


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


def _create_bot(session_factory: sessionmaker) -> str:
    with session_factory() as session:
        user = User(email="sim-snapshot-test@example.com")
        session.add(user)
        session.commit()

        bot_service = BotService(
            session, _FakeClient(), exchange_info_cache=ExchangeInfoCache()
        )
        created = bot_service.create_bot(
            user_id=user.id,
            name="Snapshot Test Bot",
            risk_profile="SENTINEL",
            asset_class="CRYPTO",
            execution_venue="BINANCE",
            instrument="BTCUSDT",
            target_quantity=Decimal("0.02"),
            idempotency_key="setup-create",
        )
        session.commit()
        return created["bot"]["id"]


def test_snapshot_of_a_fresh_bot_has_no_position_value(
    session_factory: sessionmaker,
) -> None:
    bot_id = _create_bot(session_factory)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        snapshot = take_simulation_snapshot(
            session, _FakeClient(), bot, interval_minutes=15, now=now
        )
        assert snapshot is not None
        assert Decimal(snapshot.cash_balance_quote) == Decimal("10000")
        assert Decimal(snapshot.position_value_quote) == Decimal("0")
        assert Decimal(snapshot.total_value_quote) == Decimal("10000")
        assert Decimal(snapshot.exposure_pct) == Decimal("0")
        session.commit()


def test_a_second_snapshot_in_the_same_bucket_is_restart_safe(
    session_factory: sessionmaker,
) -> None:
    bot_id = _create_bot(session_factory)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        first = take_simulation_snapshot(
            session, _FakeClient(), bot, interval_minutes=15, now=now
        )
        session.commit()
        assert first is not None

        second = take_simulation_snapshot(
            session, _FakeClient(), bot, interval_minutes=15, now=now
        )
        session.commit()
        assert second is None

        rows = session.scalars(
            select(SimulationSnapshot).where(SimulationSnapshot.bot_id == bot_id)
        ).all()
        assert len(rows) == 1


def test_market_data_failure_fails_closed_never_a_fabricated_snapshot(
    session_factory: sessionmaker,
) -> None:
    bot_id = _create_bot(session_factory)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        bot.position.current_quantity = Decimal("0.02")
        session.commit()

        snapshot = take_simulation_snapshot(
            session, _NoMarketDataClient(), bot, interval_minutes=15, now=now
        )
        session.commit()

    assert snapshot is None
    with session_factory() as session:
        rows = session.scalars(
            select(SimulationSnapshot).where(SimulationSnapshot.bot_id == bot_id)
        ).all()
        assert rows == []


def test_a_live_bot_with_no_simulation_account_is_skipped(
    session_factory: sessionmaker,
) -> None:
    bot_id = _create_bot(session_factory)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        bot = session.get(Bot, bot_id)
        bot.execution_mode = BotExecutionMode.LIVE
        session.commit()

        # A LIVE bot has no SimulationAccount to snapshot from -- this
        # must return None, never raise or fabricate a row.
        session.execute(
            text("DELETE FROM simulation_accounts WHERE bot_id = :bot_id"),
            {"bot_id": bot_id},
        )
        session.commit()

        snapshot = take_simulation_snapshot(
            session, _FakeClient(), bot, interval_minutes=15, now=now
        )
        session.commit()

    assert snapshot is None
