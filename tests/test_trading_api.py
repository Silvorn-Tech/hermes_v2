"""Integration tests for the trading REST API: the full
request -> auth -> RBAC -> risk -> OrderService -> mocked Binance ->
persistence -> response path, through a real Postgres session and a real
session cookie (not a monkeypatched RBAC layer) — only BinanceClient
itself is faked, since that's the actual network boundary.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import hermes_v2.api.trading_routes as trading_routes
from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.models import AuditLogEntry, Order

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
        self.balances = [{"asset": "USDT", "free": "100000", "locked": "0"}]
        self.trades: dict[str, list[dict]] = {}
        self.create_order_result = {
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
        self.cancel_order_result = {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": "hm-x",
            "status": "CANCELED",
            "orig_qty": "0.01",
            "executed_qty": "0.00",
        }
        self.get_order_result = None
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
        if self.get_order_result is None:
            raise RuntimeError("no get_order_result configured")
        return self.get_order_result

    def cancel_order(self, **kwargs) -> dict:
        return self.cancel_order_result


@pytest.fixture()
def db_session() -> Session:
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
    monkeypatch.setattr(trading_routes, "BinanceClient", lambda: fake_client)

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)
    return client, fake_client


def _headers(idempotency_key: str = "test-key-1") -> dict[str, str]:
    return {"Origin": _ALLOWED_ORIGIN, "Idempotency-Key": idempotency_key}


# --- authentication / authorization ---------------------------------------------


def test_unauthenticated_request_to_a_read_route_is_401() -> None:
    client = TestClient(app)
    response = client.get("/portfolio")
    assert response.status_code == 401


def test_unauthenticated_request_to_create_order_is_401() -> None:
    client = TestClient(app)
    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )
    assert response.status_code == 401


def test_authenticated_without_permission_is_403(
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

    response = client.get("/portfolio")
    assert response.status_code == 403


def test_unauthenticated_request_to_cancel_order_is_401() -> None:
    client = TestClient(app)
    response = client.post(
        "/orders/00000000-0000-0000-0000-000000000000/cancel", headers=_headers()
    )
    assert response.status_code == 401


def test_unauthenticated_request_to_close_position_is_401() -> None:
    client = TestClient(app)
    response = client.post("/positions/BTCUSDT/close", headers=_headers())
    assert response.status_code == 401


def test_authenticated_without_permission_cannot_cancel_order(
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

    response = client.post(
        "/orders/00000000-0000-0000-0000-000000000000/cancel", headers=_headers()
    )
    assert response.status_code == 403


def test_authenticated_without_permission_cannot_close_position(
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

    response = client.post("/positions/BTCUSDT/close", headers=_headers())
    assert response.status_code == 403


def test_authenticated_without_permission_cannot_create_order(
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

    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )
    assert response.status_code == 403


# --- create order -----------------------------------------------------------------


def test_successful_create_order_returns_201(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FILLED"
    assert body["order"]["symbol"] == "BTCUSDT"


def test_create_order_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers={"Idempotency-Key": "test-key-1"},
    )
    assert response.status_code == 403


def test_create_order_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers={"Origin": _ALLOWED_ORIGIN},
    )
    assert response.status_code == 422


def test_market_order_with_a_price_is_rejected_by_body_validation(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders",
        json={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "price": "50000",
        },
        headers=_headers(),
    )
    assert response.status_code == 422


def test_duplicate_create_order_request_is_idempotent(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    body = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"}

    first = client.post("/orders", json=body, headers=_headers("dup-key"))
    second = client.post("/orders", json=body, headers=_headers("dup-key"))

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert len(fake.create_order_calls) == 1


def test_kill_switch_disabled_returns_403(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    client, _fake = authorized_client

    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )
    assert response.status_code == 403


def test_risk_rejection_returns_201_with_rejected_status(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    """A risk rejection still creates a (rejected) order resource — the
    request itself was valid, the business outcome was a rejection."""
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "ETHUSDT")
    client, fake = authorized_client

    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "REJECTED"
    assert fake.create_order_calls == []


# --- cancel order -------------------------------------------------------------------


def test_cancel_nonexistent_order_is_404(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders/00000000-0000-0000-0000-000000000000/cancel",
        headers=_headers("cancel-key-1"),
    )
    assert response.status_code == 404


def test_successful_cancel_flow(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    fake.create_order_result = {
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
    create_response = client.post(
        "/orders",
        json={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": "0.01",
            "price": "50000",
        },
        headers=_headers("create-key-1"),
    )
    order_id = create_response.json()["order"]["id"]

    cancel_response = client.post(
        f"/orders/{order_id}/cancel", headers=_headers("cancel-key-1")
    )

    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "CANCELED"


# --- close position -----------------------------------------------------------------


def test_close_position_with_no_holding_is_404(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post("/positions/BTCUSDT/close", headers=_headers("close-key-1"))
    assert response.status_code == 404


def test_successful_close_position_flow(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    fake.balances = [
        {"asset": "USDT", "free": "100000", "locked": "0"},
        {"asset": "BTC", "free": "0.02", "locked": "0"},
    ]
    fake.trades["BTCUSDT"] = [
        {"qty": "0.02", "price": "40000", "time": 1700000000000, "is_buyer": True}
    ]
    fake.create_order_result = {
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

    response = client.post("/positions/BTCUSDT/close", headers=_headers("close-key-1"))

    assert response.status_code == 201
    assert response.json()["status"] == "FILLED"
    assert fake.create_order_calls[0]["side"] == "SELL"


# --- reads ----------------------------------------------------------------------------


def test_get_portfolio(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/portfolio")
    assert response.status_code == 200
    assert response.json()["total_value_quote"] == "100000"


def test_get_balances(authorized_client: tuple[TestClient, _FakeBinanceClient]) -> None:
    client, _fake = authorized_client
    response = client.get("/balances")
    assert response.status_code == 200
    assert response.json()["balances"][0]["asset"] == "USDT"


def test_get_market_data(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/market-data", params={"symbol": "BTCUSDT"})
    assert response.status_code == 200
    assert response.json()["last_price"] == "50000"


def test_get_positions_empty(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/positions")
    assert response.status_code == 200
    assert response.json()["positions"] == []


def test_list_orders_and_get_order_detail(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers("list-key-1"),
    )
    order_id = create_response.json()["order"]["id"]

    list_response = client.get("/orders")
    assert list_response.status_code == 200
    assert list_response.json()["count"] == 1

    detail_response = client.get(f"/orders/{order_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == order_id


def test_get_order_belonging_to_another_user_is_404(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT")

    seed_authorization_data(db_session)
    super_admin = db_session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    owner = User(email="owner@example.com")
    attacker = User(email="attacker@example.com")
    db_session.add_all([owner, attacker])
    db_session.flush()
    owner.roles.append(super_admin)
    attacker.roles.append(super_admin)
    _, owner_token = create_session(db_session, owner, timedelta(hours=1))
    _, attacker_token = create_session(db_session, attacker, timedelta(hours=1))
    db_session.commit()

    fake_client = _FakeBinanceClient()
    monkeypatch.setattr(trading_routes, "BinanceClient", lambda: fake_client)

    owner_client = TestClient(app)
    owner_client.cookies.set("hermes_session", owner_token)
    create_response = owner_client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers("owner-key"),
    )
    order_id = create_response.json()["order"]["id"]

    attacker_client = TestClient(app)
    attacker_client.cookies.set("hermes_session", attacker_token)
    response = attacker_client.get(f"/orders/{order_id}")

    assert response.status_code == 404


# --- audit log --------------------------------------------------------------


def test_successful_order_writes_an_audit_log_row(
    authorized_client: tuple[TestClient, _FakeBinanceClient], db_session: Session
) -> None:
    client, _fake = authorized_client
    client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers("audit-key-1"),
    )

    audit_rows = db_session.scalars(select(AuditLogEntry)).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "orders.create"


def test_no_response_ever_contains_the_word_secret_or_api_key(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )
    assert "api_key" not in response.text.lower()
    assert "api_secret" not in response.text.lower()


def test_order_row_persists_and_is_queryable_directly(
    authorized_client: tuple[TestClient, _FakeBinanceClient], db_session: Session
) -> None:
    client, _fake = authorized_client
    client.post(
        "/orders",
        json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        headers=_headers(),
    )

    orders = db_session.scalars(select(Order)).all()
    assert len(orders) == 1
    assert orders[0].symbol == "BTCUSDT"
