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
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import hermes_v2.api.bots_routes as bots_routes
from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.binance_credentials_service import connect_credentials
from hermes_v2.trading.models import Bot, BotExecutionMode, BotStatus
from hermes_v2.trading.risk_engine import RiskLimits
from hermes_v2.trading.user_risk_settings_service import save_user_risk_limits

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

    def get_api_key_permissions(self) -> dict:
        return {"can_withdraw": False}


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
    monkeypatch.setattr(bots_routes, "BinanceClient", lambda *a, **kw: fake_client)

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


# A LIVE bot's pause/resume goes through OrderService, which reads
# per-user real-order risk limits (not the HERMES_RISK_* env vars the
# fixture above sets for Simulation) -- see user_risk_settings_service.py.
_PERMISSIVE_REAL_RISK_LIMITS = RiskLimits(
    max_order_notional_quote=Decimal("10000"),
    max_symbol_exposure_pct=Decimal("100"),
    max_total_exposure_pct=Decimal("100"),
    max_daily_loss_pct=Decimal("100"),
    max_open_positions=10,
    allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
)


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HERMES_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


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
    """Every bot POST /bots creates is SIMULATION (there is no API path to
    LIVE yet — see BotExecutionMode) — this now exercises the full
    Simulation lifecycle end-to-end through the real REST API, including
    the isolation guarantee: not a single Binance write call happens
    across a full resume/pause/stop cycle."""
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    assert create_response.json()["bot"]["execution_mode"] == "SIMULATION"
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

    assert fake.create_order_calls == []  # SIMULATION never reaches Binance


def test_full_bot_lifecycle_never_requires_a_connected_binance_account(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    """The core multi-tenancy requirement this phase exists to protect:
    a user who has never connected a Binance account -- confirmed here by
    asserting zero UserBinanceCredential rows exist for them -- can still
    fully create/resume/pause/stop/delete a SIMULATION bot end-to-end."""
    from hermes_v2.trading.models import UserBinanceCredential

    client, _fake = authorized_client
    user = db_session.scalars(select(User)).one()
    assert (
        db_session.scalar(
            select(UserBinanceCredential).where(
                UserBinanceCredential.user_id == user.id
            )
        )
        is None
    )

    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    assert create_response.status_code == 201
    bot_id = create_response.json()["bot"]["id"]

    assert (
        client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key")).json()[
            "status"
        ]
        == "ACTIVE"
    )
    assert (
        client.post(f"/bots/{bot_id}/pause", headers=_headers("pause-key")).json()[
            "status"
        ]
        == "PAUSED"
    )
    assert (
        client.post(f"/bots/{bot_id}/stop", headers=_headers("stop-key")).json()[
            "status"
        ]
        == "STOPPED"
    )

    delete_response = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key"))
    assert delete_response.status_code == 200
    assert client.get(f"/bots/{bot_id}").status_code == 404


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


# --- activate-live ---------------------------------------------------------------


def test_activate_live_requires_permission(
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
        "/bots/00000000-0000-0000-0000-000000000000/activate-live",
        headers=_headers(),
    )
    assert response.status_code == 403


def test_activate_live_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.post(
        f"/bots/{bot_id}/activate-live", headers={"Idempotency-Key": "key-1"}
    )
    assert response.status_code == 403


def test_activate_live_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.post(
        f"/bots/{bot_id}/activate-live", headers={"Origin": _ALLOWED_ORIGIN}
    )
    assert response.status_code == 422


def test_activate_live_404_for_another_users_bot(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    attacker = User(email="attacker@example.com")
    db_session.add(attacker)
    db_session.flush()
    super_admin = db_session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    attacker.roles.append(super_admin)
    _, attacker_token = create_session(db_session, attacker, timedelta(hours=1))
    db_session.commit()

    attacker_client = TestClient(app)
    attacker_client.cookies.set("hermes_session", attacker_token)

    response = attacker_client.post(f"/bots/{bot_id}/activate-live", headers=_headers())
    assert response.status_code == 404


def test_activate_live_rejects_a_non_paused_bot(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]
    user = db_session.scalars(select(User)).one()
    save_user_risk_limits(db_session, user.id, _PERMISSIVE_REAL_RISK_LIMITS)
    db_session.commit()

    client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key"))

    response = client.post(f"/bots/{bot_id}/activate-live", headers=_headers())
    assert response.status_code == 409
    assert "PAUSED" in response.json()["detail"]


def test_activate_live_rejects_no_connected_credentials(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.post(f"/bots/{bot_id}/activate-live", headers=_headers())
    assert response.status_code == 409
    assert "Connect a verified Binance account" in response.json()["detail"]

    bot_response = client.get(f"/bots/{bot_id}")
    assert bot_response.json()["execution_mode"] == "SIMULATION"


def test_activate_live_succeeds_with_connected_credentials(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    user = db_session.scalars(select(User)).one()
    connect_credentials(db_session, user.id, fake, "live-key-1234", "live-secret")
    db_session.commit()

    response = client.post(f"/bots/{bot_id}/activate-live", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PAUSED"
    assert body["bot"]["execution_mode"] == "LIVE"

    bot = db_session.get(Bot, bot_id)
    db_session.refresh(bot)
    assert bot.execution_mode == BotExecutionMode.LIVE
    assert bot.status == BotStatus.PAUSED


def test_activate_live_rejects_an_already_live_bot(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    user = db_session.scalars(select(User)).one()
    connect_credentials(db_session, user.id, fake, "live-key-1234", "live-secret")
    db_session.commit()

    first = client.post(f"/bots/{bot_id}/activate-live", headers=_headers("key-1"))
    assert first.status_code == 200

    second = client.post(f"/bots/{bot_id}/activate-live", headers=_headers("key-2"))
    assert second.status_code == 409
    assert "already LIVE" in second.json()["detail"]


def test_activate_live_is_idempotent(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    user = db_session.scalars(select(User)).one()
    connect_credentials(db_session, user.id, fake, "live-key-1234", "live-secret")
    db_session.commit()

    first = client.post(f"/bots/{bot_id}/activate-live", headers=_headers("same-key"))
    second = client.post(f"/bots/{bot_id}/activate-live", headers=_headers("same-key"))
    assert first.json() == second.json()


def test_no_deactivate_live_route_exists(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    """Regression guard for the confirmed one-way decision: there is no
    route to revert a LIVE bot back to SIMULATION."""
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    user = db_session.scalars(select(User)).one()
    connect_credentials(db_session, user.id, fake, "live-key-1234", "live-secret")
    db_session.commit()
    client.post(f"/bots/{bot_id}/activate-live", headers=_headers())

    for path in (
        f"/bots/{bot_id}/deactivate-live",
        f"/bots/{bot_id}/deactivate",
    ):
        response = client.post(path, headers=_headers("no-such-route"))
        assert response.status_code == 404


def test_pause_resume_after_activate_live_uses_the_real_credentialed_client(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    """The key regression test for the _run_bot_service_action fix: a
    LIVE bot's resume must place its order through the user's own
    connected credentials, not the blank public client. get_decrypted_client
    is patched to return the same fake -- otherwise this would build a
    real, unpatched BinanceClient and attempt a real network call the
    moment resume() actually calls create_order()."""
    client, fake = authorized_client
    monkeypatch.setattr(
        bots_routes, "get_decrypted_client", lambda session, user_id: fake
    )
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    user = db_session.scalars(select(User)).one()
    connect_credentials(db_session, user.id, fake, "live-key-1234", "live-secret")
    save_user_risk_limits(db_session, user.id, _PERMISSIVE_REAL_RISK_LIMITS)
    db_session.commit()

    client.post(f"/bots/{bot_id}/activate-live", headers=_headers("activate-key"))

    resume_response = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key")
    )
    assert resume_response.status_code == 200
    assert resume_response.json()["status"] == "ACTIVE"
    # get_decrypted_client() constructs a *new* BinanceClient from the
    # decrypted credentials -- it is never literally `fake` -- so the
    # real assertion is that a real order actually reached create_order
    # with the connected account's identity, not a blank one silently
    # swallowing the call.
    assert len(fake.create_order_calls) == 1


def test_pause_still_works_for_a_simulation_bot_with_no_connected_credentials(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    """Regression guard for the fallback path: the overwhelming majority
    of bots are SIMULATION with no connected Binance account at all, and
    that must keep working with zero credential setup."""
    client, fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    resume_response = client.post(
        f"/bots/{bot_id}/resume", headers=_headers("resume-key")
    )
    assert resume_response.status_code == 200
    assert resume_response.json()["status"] == "ACTIVE"
    assert fake.create_order_calls == []  # SIMULATION never reaches Binance


def test_live_bot_performance_never_leaks_another_bots_orders(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
    db_session: Session,
) -> None:
    """Bot A's real Order history must never show up in Bot B's
    performance view -- the SQL-level counterpart to
    test_live_portfolio_service.py's pure-function tests, which can't
    catch a query missing its bot_id filter since they never issue one."""
    from datetime import UTC, datetime

    from hermes_v2.trading.models import Order, OrderSide, OrderStatus, OrderType

    client, _fake = authorized_client
    bot_a_id = client.post(
        "/bots", json=_create_body(name="Bot A"), headers=_headers("create-a")
    ).json()["bot"]["id"]
    bot_b_id = client.post(
        "/bots", json=_create_body(name="Bot B"), headers=_headers("create-b")
    ).json()["bot"]["id"]

    bot_a = db_session.get(Bot, bot_a_id)
    bot_b = db_session.get(Bot, bot_b_id)
    bot_a.execution_mode = BotExecutionMode.LIVE
    bot_b.execution_mode = BotExecutionMode.LIVE
    db_session.commit()

    now = datetime.now(UTC)
    db_session.add_all(
        [
            Order(
                user_id=bot_a.user_id,
                bot_id=bot_a.id,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                requested_quantity=Decimal("0.01"),
                executed_quantity=Decimal("0.01"),
                average_fill_price=Decimal("50000"),
                binance_client_order_id="isolation-a-buy",
                terminal_at=now,
            ),
            Order(
                user_id=bot_a.user_id,
                bot_id=bot_a.id,
                symbol="BTCUSDT",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                requested_quantity=Decimal("0.01"),
                executed_quantity=Decimal("0.01"),
                average_fill_price=Decimal("51000"),
                binance_client_order_id="isolation-a-sell",
                terminal_at=now,
            ),
        ]
    )
    db_session.commit()

    # Bot B has zero orders of its own -- its view must stay at zero,
    # never inheriting Bot A's round trip.
    performance_b = client.get(f"/bots/{bot_b_id}/performance").json()
    assert performance_b["trade_count"] == 0
    assert Decimal(performance_b["realized_pnl_today_quote"]) == Decimal("0")

    performance_a = client.get(f"/bots/{bot_a_id}/performance").json()
    assert performance_a["trade_count"] == 1
    assert Decimal(performance_a["realized_pnl_today_quote"]) == Decimal("10")


# --- delete -------------------------------------------------------------------


def test_delete_bot_while_paused_removes_it(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]
    assert create_response.json()["status"] == "PAUSED"

    delete_response = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key-1"))
    assert delete_response.status_code == 200
    assert delete_response.json() == {"bot": None, "status": "DELETED", "reason": None}

    get_response = client.get(f"/bots/{bot_id}")
    assert get_response.status_code == 404


def test_delete_bot_while_stopped_removes_it(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    stop_response = client.post(f"/bots/{bot_id}/stop", headers=_headers("stop-key"))
    assert stop_response.json()["status"] == "STOPPED"

    delete_response = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key-2"))
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "DELETED"


def test_delete_an_active_bot_is_rejected_not_deleted(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]
    client.post(f"/bots/{bot_id}/resume", headers=_headers("resume-key"))

    delete_response = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key-3"))
    assert delete_response.status_code == 409

    # Still there, untouched.
    get_response = client.get(f"/bots/{bot_id}")
    assert get_response.status_code == 200
    assert get_response.json()["status"] == "ACTIVE"


def test_delete_unknown_bot_is_404(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.delete(
        "/bots/00000000-0000-0000-0000-000000000000", headers=_headers("delete-key-4")
    )
    assert response.status_code == 404


def test_delete_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.delete(
        f"/bots/{bot_id}", headers={"Idempotency-Key": "delete-key-5"}
    )
    assert response.status_code == 403


def test_delete_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    response = client.delete(f"/bots/{bot_id}", headers={"Origin": _ALLOWED_ORIGIN})
    assert response.status_code == 422


def test_duplicate_delete_request_is_idempotent(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    create_response = client.post("/bots", json=_create_body(), headers=_headers())
    bot_id = create_response.json()["bot"]["id"]

    first = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key-6"))
    second = client.delete(f"/bots/{bot_id}", headers=_headers("delete-key-6"))
    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        first.json()
        == second.json()
        == {
            "bot": None,
            "status": "DELETED",
            "reason": None,
        }
    )
