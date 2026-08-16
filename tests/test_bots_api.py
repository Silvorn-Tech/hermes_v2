"""Integration tests for the Bot REST API: the full
request -> auth -> RBAC -> origin -> idempotency -> BotService ->
mocked Binance -> persistence -> response path, through a real Postgres
session and a real session cookie. Mirrors test_trading_api.py's shape.
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
from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment

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
        self.balances = [
            {"asset": "USDT", "free": "100000", "locked": "0"},
            {"asset": "BTC", "free": "0.015", "locked": "0"},
        ]
        self.trades: dict[str, list[dict]] = {}
        self.create_order_result = {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": "hm-x",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "price": "0",
            "orig_qty": "0.015",
            "executed_qty": "0.015",
            "cummulative_quote_qty": "750.00",
            "transact_time": 1700000000000,
        }
        self.create_order_calls: list[dict] = []

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
        return self.create_order_result

    def get_order(self, **kwargs) -> dict:
        raise RuntimeError("no get_order_result configured")


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
        "target_quantity": "0.015",
    }
    body.update(overrides)
    return body


# --- authentication / authorization ---------------------------------------------


def test_unauthenticated_request_to_list_bots_is_401() -> None:
    client = TestClient(app)
    response = client.get("/bots")
    assert response.status_code == 401


def test_unauthenticated_request_to_create_bot_is_401() -> None:
    client = TestClient(app)
    response = client.post("/bots", json=_create_body(), headers=_headers())
    assert response.status_code == 401


def test_unauthenticated_request_to_pause_is_401() -> None:
    client = TestClient(app)
    response = client.post(
        "/bots/00000000-0000-0000-0000-000000000000/pause", headers=_headers()
    )
    assert response.status_code == 401


def test_authenticated_without_permission_cannot_list_bots(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    seed_authorization_data(db_session)
    user = User(email="no-perms@example.com")
    db_session.add(user)
    db_session.flush()
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)

    response = client.get("/bots")
    assert response.status_code == 403


def test_authenticated_without_permission_cannot_create_bot(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    seed_authorization_data(db_session)
    user = User(email="no-perms@example.com")
    db_session.add(user)
    db_session.flush()
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)

    response = client.post("/bots", json=_create_body(), headers=_headers())
    assert response.status_code == 403


def test_create_bot_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/bots", json=_create_body(), headers={"Idempotency-Key": "test-key-1"}
    )
    assert response.status_code == 403


def test_pause_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.post(
        f"/bots/{bot_id}/pause", headers={"Idempotency-Key": "test-key-2"}
    )
    assert response.status_code == 403


def test_create_bot_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/bots", json=_create_body(), headers={"Origin": _ALLOWED_ORIGIN}
    )
    assert response.status_code == 422


# --- end-to-end lifecycle -----------------------------------------------------------


def test_create_list_get_bot(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    assert create_response.status_code == 201
    body = create_response.json()
    assert body["status"] == "PAUSED"
    bot_id = body["bot"]["id"]

    list_response = client.get("/bots")
    assert list_response.status_code == 200
    assert any(b["id"] == bot_id for b in list_response.json()["bots"])

    get_response = client.get(f"/bots/{bot_id}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == bot_id


def test_create_bot_with_invalid_asset_class_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/bots", json=_create_body(asset_class="COMMODITIES"), headers=_headers()
    )
    assert response.status_code == 422


def test_get_unknown_bot_is_404(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/bots/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_full_pause_resume_stop_cycle(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    resume_response = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key")
    )
    assert resume_response.status_code == 200
    resume_body = resume_response.json()
    assert resume_body["status"] == "ACTIVE"
    assert Decimal(resume_body["bot"]["current_quantity"]) == Decimal("0.015")

    pause_response = client.post(f"/bots/{bot_id}/pause", headers=_headers("pause-key"))
    assert pause_response.status_code == 200
    pause_body = pause_response.json()
    assert pause_body["status"] == "PAUSED"
    assert Decimal(pause_body["bot"]["current_quantity"]) == Decimal("0")

    stop_response = client.post(f"/bots/{bot_id}/stop", headers=_headers("stop-key"))
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "STOPPED"

    # Resuming a stopped bot is an invalid transition, not a crash.
    invalid_resume = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key-2")
    )
    assert invalid_resume.status_code == 409

    assert len(fake.create_order_calls) == 2  # one BUY, one SELL — never more


def test_resume_with_kill_switch_off_returns_200_rejected_not_500(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    monkeypatch.setenv("TRADING_ENABLED", "false")
    response = client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["bot"]["status"] == "PAUSED"
    assert fake.create_order_calls == []


def test_update_bot_only_allowed_while_paused(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    ok_response = client.patch(
        f"/bots/{bot_id}",
        json={"name": "Renamed Bot"},
        headers=_headers("update-key-1"),
    )
    assert ok_response.status_code == 200
    assert ok_response.json()["bot"]["name"] == "Renamed Bot"

    client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key"))

    blocked_response = client.patch(
        f"/bots/{bot_id}",
        json={"name": "Should Not Apply"},
        headers=_headers("update-key-2"),
    )
    assert blocked_response.status_code == 409
