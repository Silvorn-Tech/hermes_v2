"""Static, schema-level, and runtime guards for Simulation Mode's central
safety claim (see docs/architecture/simulation.md's "Isolation
guarantees"): a Simulation order can never reach Binance's write
endpoints, and a Simulation position/order/account can never be read
back through a real-account endpoint.

Four independent layers, each catching a different failure mode:
1. AST — `simulation_order_service.py`'s source never references
   `create_order`/`cancel_order` as an identifier anywhere (catches a
   call that's present but perhaps dead-code-guarded, unlike a plain
   import check).
2. Schema — the simulation tables have no foreign key into `orders`,
   and `orders` has no foreign key into the simulation tables, so
   combining them would require an explicit `UNION`, not a `WHERE`
   someone could forget.
3. Runtime — a `BinanceClient` double that raises if `create_order`/
   `cancel_order` is ever invoked, exercised across a full simulated
   BUY+SELL cycle through the real `SimulationOrderService`.
4. Real endpoints — `trading_routes.py` (home of `GET /portfolio`,
   `GET /positions`) never imports any Simulation model or service.
"""

from __future__ import annotations

import ast
import inspect
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import hermes_v2.api.trading_routes as trading_routes
import hermes_v2.trading.simulation_order_service as simulation_order_service_module
from hermes_v2.auth.models import User
from hermes_v2.database.connection import Base, create_engine_from_environment
from hermes_v2.integrations.binance import BinanceError
from hermes_v2.trading.models import BotPosition
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models.bot import (
    AssetClass,
    Bot,
    BotExecutionMode,
    BotStatus,
    ExecutionVenue,
    RiskProfile,
)
from hermes_v2.trading.models.simulation import SimulationAccount
from hermes_v2.trading.simulation_order_service import SimulationOrderService

_FORBIDDEN_IDENTIFIERS = {"create_order", "cancel_order"}

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


# --- 1. AST: source never references create_order/cancel_order ----------------


def test_simulation_order_service_source_never_names_binance_write_methods() -> None:
    source_file = Path(inspect.getfile(simulation_order_service_module))
    tree = ast.parse(source_file.read_text(), filename=str(source_file))

    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_IDENTIFIERS:
            hits.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_IDENTIFIERS:
            hits.add(node.id)

    assert not hits, (
        f"{source_file.name} references forbidden Binance write method(s): {hits}"
    )


def test_forbidden_identifier_list_is_not_accidentally_empty() -> None:
    assert _FORBIDDEN_IDENTIFIERS == {"create_order", "cancel_order"}


# --- 2. Schema: no FK links simulation tables to the real orders table ---------


def test_simulation_tables_have_no_foreign_key_into_orders_table() -> None:
    for table_name in (
        "simulation_accounts",
        "simulation_orders",
        "simulation_snapshots",
    ):
        table = Base.metadata.tables[table_name]
        referenced_tables = {fk.column.table.name for fk in table.foreign_keys}
        assert "orders" not in referenced_tables, (
            f"{table_name} has a foreign key into orders — combining real and "
            "simulated data would no longer require an explicit UNION."
        )


def test_orders_table_has_no_foreign_key_into_simulation_tables() -> None:
    orders_table = Base.metadata.tables["orders"]
    referenced_tables = {fk.column.table.name for fk in orders_table.foreign_keys}
    simulation_tables = {
        "simulation_accounts",
        "simulation_orders",
        "simulation_snapshots",
    }
    assert not (referenced_tables & simulation_tables)


# --- 3. Runtime: a full simulated cycle never calls create_order/cancel_order --


class _RaisingBinanceClient:
    """Fails the test loudly and immediately if Simulation ever reaches a
    real Binance write endpoint — proof at call-time, not just parse-time."""

    def __init__(self) -> None:
        self.market_data = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info = {"BTCUSDT": _GOOD_EXCHANGE_INFO}

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def create_order(self, **kwargs) -> dict:
        raise AssertionError(
            "SimulationOrderService must never call BinanceClient.create_order()"
        )

    def cancel_order(self, **kwargs) -> dict:
        raise AssertionError(
            "SimulationOrderService must never call BinanceClient.cancel_order()"
        )


@pytest.fixture()
def db_session():
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
    session_factory = sessionmaker(engine)
    with session_factory() as session:
        yield session
        session.rollback()
    engine.dispose()


def test_a_full_simulated_buy_and_sell_never_reaches_binance_write_endpoints(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT")

    user = User(email="isolation@example.com")
    db_session.add(user)
    db_session.flush()

    bot = Bot(
        user_id=user.id,
        name="Isolation Bot",
        risk_profile=RiskProfile.SENTINEL,
        asset_class=AssetClass.CRYPTO,
        execution_venue=ExecutionVenue.BINANCE,
        execution_mode=BotExecutionMode.SIMULATION,
        instrument="BTCUSDT",
        status=BotStatus.PAUSED,
    )
    db_session.add(bot)
    db_session.flush()
    db_session.add(
        BotPosition(
            bot_id=bot.id,
            instrument="BTCUSDT",
            current_quantity=Decimal("0"),
            target_quantity=Decimal("0.02"),
        )
    )
    db_session.add(
        SimulationAccount(
            bot_id=bot.id,
            quote_asset="USDT",
            initial_capital_quote=Decimal("10000"),
            cash_balance_quote=Decimal("10000"),
        )
    )
    db_session.flush()

    client = _RaisingBinanceClient()
    service = SimulationOrderService(
        db_session, client, exchange_info_cache=ExchangeInfoCache()
    )

    buy_result = service.place_bot_order(
        user_id=user.id,
        bot=bot,
        side="BUY",
        quantity=Decimal("0.02"),
        idempotency_key="isolation-buy",
    )
    assert buy_result["status"] == "FILLED"

    sell_result = service.place_bot_order(
        user_id=user.id,
        bot=bot,
        side="SELL",
        quantity=Decimal("0.02"),
        idempotency_key="isolation-sell",
    )
    assert sell_result["status"] == "FILLED"
    # If either call had reached create_order/cancel_order, the double
    # above would have raised AssertionError before either result formed.


def test_market_data_failure_fails_closed_never_a_zero_price_fill(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT")

    user = User(email="isolation-mktdata@example.com")
    db_session.add(user)
    db_session.flush()

    bot = Bot(
        user_id=user.id,
        name="Isolation Bot 2",
        risk_profile=RiskProfile.SENTINEL,
        asset_class=AssetClass.CRYPTO,
        execution_venue=ExecutionVenue.BINANCE,
        execution_mode=BotExecutionMode.SIMULATION,
        instrument="BTCUSDT",
        status=BotStatus.PAUSED,
    )
    db_session.add(bot)
    db_session.flush()
    db_session.add(
        BotPosition(
            bot_id=bot.id,
            instrument="BTCUSDT",
            current_quantity=Decimal("0"),
            target_quantity=Decimal("0.02"),
        )
    )
    db_session.add(
        SimulationAccount(
            bot_id=bot.id,
            quote_asset="USDT",
            initial_capital_quote=Decimal("10000"),
            cash_balance_quote=Decimal("10000"),
        )
    )
    db_session.flush()

    class _NoMarketDataClient(_RaisingBinanceClient):
        def get_market_data(self, symbol: str) -> dict:
            raise BinanceError("market data unavailable")

    service = SimulationOrderService(
        db_session, _NoMarketDataClient(), exchange_info_cache=ExchangeInfoCache()
    )
    result = service.place_bot_order(
        user_id=user.id,
        bot=bot,
        side="BUY",
        quantity=Decimal("0.02"),
        idempotency_key=f"isolation-mktdata-{uuid.uuid4()}",
    )
    assert result["status"] == "FAILED"
    assert result["order"]["fill_price"] is None


# --- 4. Real endpoints never import a Simulation model or service -------------


def test_trading_routes_never_imports_simulation_code() -> None:
    source_file = Path(inspect.getfile(trading_routes))
    tree = ast.parse(source_file.read_text(), filename=str(source_file))

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    simulation_modules = {m for m in imported_modules if "simulation" in m}
    assert not simulation_modules, (
        f"trading_routes.py (home of GET /portfolio, GET /positions) imports "
        f"simulation module(s): {simulation_modules}"
    )
