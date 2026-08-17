"""Integration tests for the three new Simulation Mode read endpoints:
`GET /config/simulation`, `GET /bots/{id}/portfolio`, and
`GET /bots/{id}/performance`. Mirrors test_bots_api.py's fixture shape.
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import hermes_v2.api.bots_routes as bots_routes
import hermes_v2.api.trading_routes as trading_routes
from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.models import Bot, BotExecutionMode

pytestmark = pytest.mark.database

_ALLOWED_ORIGIN = "https://app.example.com"

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
        self.market_data = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        self.balances: list[dict] = []
        self.create_order_calls: list[dict] = []

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def get_balances(self) -> list[dict]:
        return self.balances

    def get_trades(self, symbol: str) -> list[dict]:
        return []

    def create_order(self, **kwargs) -> dict:  # pragma: no cover - must never run
        raise AssertionError("Simulation must never call create_order")

    def cancel_order(self, **kwargs) -> dict:  # pragma: no cover - must never run
        raise AssertionError("Simulation must never call cancel_order")


@pytest.fixture()
def db_session() -> Session:
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


@pytest.fixture()
def authorized_client(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> tuple[TestClient, _FakeBinanceClient]:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT")

    seed_authorization_data(db_session)
    user = User(email="trader@example.com")
    db_session.add(user)
    db_session.flush()
    super_admin = db_session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    user.roles.append(super_admin)
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    fake_client = _FakeBinanceClient()
    monkeypatch.setattr(bots_routes, "BinanceClient", lambda: fake_client)

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)
    return client, fake_client


def _headers(idempotency_key: str = "test-key-1") -> dict[str, str]:
    return {"Origin": _ALLOWED_ORIGIN, "Idempotency-Key": idempotency_key}


def _create_body(**overrides) -> dict:
    body = {
        "name": "API Test Bot",
        "risk_profile": "SENTINEL",
        "asset_class": "CRYPTO",
        "execution_venue": "BINANCE",
        "instrument": "BTCUSDT",
        "target_quantity": "0.02",
    }
    body.update(overrides)
    return body


# --- GET /config/simulation --------------------------------------------------


def test_simulation_config_requires_no_authentication() -> None:
    client = TestClient(app)
    response = client.get("/config/simulation")
    assert response.status_code == 200
    body = response.json()
    assert body["initial_capital_quote"] == "10000"
    assert body["quote_asset"] == "USDT"


def test_simulation_config_reflects_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", "25000")
    client = TestClient(app)
    response = client.get("/config/simulation")
    assert response.json()["initial_capital_quote"] == "25000"


# --- GET /bots/{id}/portfolio -------------------------------------------------


def test_portfolio_route_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/bots/00000000-0000-0000-0000-000000000000/portfolio")
    assert response.status_code == 401


def test_portfolio_route_unknown_bot_is_404(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/bots/00000000-0000-0000-0000-000000000000/portfolio")
    assert response.status_code == 404


def test_portfolio_route_for_a_fresh_bot_shows_full_virtual_cash(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.get(f"/bots/{bot_id}/portfolio")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["execution_mode"] == "SIMULATION"
    assert Decimal(body["cash_balance_quote"]) == Decimal("10000")
    assert Decimal(body["total_value_quote"]) == Decimal("10000")
    assert Decimal(body["position_value_quote"]) == Decimal("0")
    assert Decimal(body["current_quantity"]) == Decimal("0")
    # No trade has happened yet: return% relative to inception is 0.
    assert Decimal(body["return_pct"]) == Decimal("0")


def test_portfolio_route_after_a_virtual_buy_reflects_the_fill(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    resume_response = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key")
    )
    assert resume_response.json()["status"] == "ACTIVE"

    response = client.get(f"/bots/{bot_id}/portfolio")
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["current_quantity"]) == Decimal("0.02")
    # 0.02 BTC @ 50000 = 1000 quote spent; cash drops below 10000.
    assert Decimal(body["cash_balance_quote"]) < Decimal("10000")
    assert Decimal(body["position_value_quote"]) == Decimal("1000")


def test_portfolio_route_is_not_available_for_live_bots(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    bot = db_session.get(Bot, bot_id)
    bot.execution_mode = BotExecutionMode.LIVE
    db_session.commit()

    response = client.get(f"/bots/{bot_id}/portfolio")
    assert response.status_code == 409
    assert response.json()["detail"]["available"] is False


# --- GET /bots/{id}/performance -----------------------------------------------


def test_performance_route_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/bots/00000000-0000-0000-0000-000000000000/performance")
    assert response.status_code == 401


def test_performance_route_for_a_fresh_bot_has_no_trades_yet(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.get(f"/bots/{bot_id}/performance")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["trade_count"] == 0
    assert body["win_rate_pct"] is None
    assert Decimal(body["realized_pnl_today_quote"]) == Decimal("0")


def test_performance_route_after_a_round_trip_counts_one_trade(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key"))
    fake.market_data["BTCUSDT"] = {"last_price": "51000"}
    client.post(f"/bots/{bot_id}/pause", headers=_headers("pause-key"))

    response = client.get(f"/bots/{bot_id}/performance")
    assert response.status_code == 200
    body = response.json()
    assert body["trade_count"] == 1
    assert body["win_rate_pct"] == "100"
    # Bought at 50000, sold at 51000: a real profit, no invented number.
    assert Decimal(body["realized_pnl_today_quote"]) > Decimal("0")
    assert fake.create_order_calls == []


def test_performance_route_is_not_available_for_live_bots(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    bot = db_session.get(Bot, bot_id)
    bot.execution_mode = BotExecutionMode.LIVE
    db_session.commit()

    response = client.get(f"/bots/{bot_id}/performance")
    assert response.status_code == 409
    assert response.json()["detail"]["available"] is False


# --- Simulation is never visible through the real account endpoints ----------


def test_a_simulation_fill_never_appears_in_the_real_portfolio(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    """GET /portfolio (trading_routes.py, real-Binance-account-backed) and
    GET /bots/{id}/portfolio (bots_routes.py, this bot's own virtual
    ledger) must never cross: a $10,000 virtual BUY fill must not move
    the real account's reported balance by a single unit."""
    client, fake = authorized_client
    monkeypatch.setattr(trading_routes, "BinanceClient", lambda: fake)

    real_before = client.get("/portfolio")
    assert real_before.status_code == 200
    assert Decimal(real_before.json()["total_value_quote"]) == Decimal("0")

    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]
    resume_response = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key")
    )
    assert resume_response.json()["status"] == "ACTIVE"

    sim_portfolio = client.get(f"/bots/{bot_id}/portfolio")
    assert Decimal(sim_portfolio.json()["cash_balance_quote"]) < Decimal("10000")

    real_after = client.get("/portfolio")
    assert real_after.status_code == 200
    assert Decimal(real_after.json()["total_value_quote"]) == Decimal("0")
    assert real_after.json()["balances"] == []
