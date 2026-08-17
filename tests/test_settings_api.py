"""Integration tests for the Settings REST API: Binance credentials, risk
limits, and the personal trading switch. Mirrors test_trading_api.py's
shape (real Postgres session, real session cookie, only BinanceClient
itself faked).
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

import hermes_v2.api.settings_routes as settings_routes
from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceAuthenticationError

pytestmark = pytest.mark.database

_ALLOWED_ORIGIN = "https://app.example.com"


class _FakeBinanceClient:
    def __init__(
        self, *, can_withdraw: bool = False, error: Exception | None = None
    ) -> None:
        self.can_withdraw = can_withdraw
        self.error = error
        self.get_api_key_permissions_calls = 0

    def get_api_key_permissions(self) -> dict:
        self.get_api_key_permissions_calls += 1
        if self.error is not None:
            raise self.error
        return {"can_withdraw": self.can_withdraw}


@pytest.fixture()
def db_session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE user_binance_credentials, user_trading_settings, "
                "role_permissions, user_roles, identities, sessions, permissions, "
                "roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HERMES_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture()
def authorized_client(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> tuple[TestClient, _FakeBinanceClient]:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)

    seed_authorization_data(db_session)
    user = User(email="trader@example.com")
    db_session.add(user)
    db_session.flush()
    super_admin = db_session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    user.roles.append(super_admin)
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    fake_client = _FakeBinanceClient()
    monkeypatch.setattr(settings_routes, "BinanceClient", lambda *a, **kw: fake_client)

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)
    return client, fake_client


@pytest.fixture()
def no_permission_client(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> TestClient:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    seed_authorization_data(db_session)
    user = User(email="no-perms@example.com")
    db_session.add(user)
    db_session.flush()
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)
    return client


def _headers(idempotency_key: str = "test-key-1") -> dict[str, str]:
    return {"Origin": _ALLOWED_ORIGIN, "Idempotency-Key": idempotency_key}


# --- authentication / authorization ---------------------------------------------


def test_unauthenticated_get_credentials_is_401() -> None:
    client = TestClient(app)
    response = client.get("/settings/binance-credentials")
    assert response.status_code == 401


def test_no_permission_cannot_read_credentials(
    no_permission_client: TestClient,
) -> None:
    assert no_permission_client.get("/settings/binance-credentials").status_code == 403


def test_no_permission_cannot_write_credentials(
    no_permission_client: TestClient,
) -> None:
    response = no_permission_client.put(
        "/settings/binance-credentials",
        json={"api_key": "some-key-1234", "api_secret": "some-secret"},
        headers=_headers(),
    )
    assert response.status_code == 403


def test_no_permission_cannot_manage_risk_limits(
    no_permission_client: TestClient,
) -> None:
    assert no_permission_client.get("/settings/risk-limits").status_code == 403
    response = no_permission_client.put(
        "/settings/risk-limits", json={}, headers=_headers()
    )
    assert response.status_code == 403


def test_no_permission_cannot_manage_simulation_risk_limits(
    no_permission_client: TestClient,
) -> None:
    assert (
        no_permission_client.get("/settings/simulation-risk-limits").status_code == 403
    )
    response = no_permission_client.put(
        "/settings/simulation-risk-limits", json={}, headers=_headers()
    )
    assert response.status_code == 403


def test_no_permission_cannot_manage_trading_switch(
    no_permission_client: TestClient,
) -> None:
    assert no_permission_client.get("/settings/trading-switch").status_code == 403
    response = no_permission_client.put(
        "/settings/trading-switch", json={"enabled": False}, headers=_headers()
    )
    assert response.status_code == 403


# --- Binance credentials: full CRUD ----------------------------------------------


def test_credentials_not_configured_initially(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/settings/binance-credentials")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["api_key_last4"] is None


def test_connect_credentials_verifies_and_persists(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "my-real-secret"},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["api_key_last4"] == "1234"
    assert body["verified_at"] is not None
    assert fake.get_api_key_permissions_calls == 1

    status_response = client.get("/settings/binance-credentials")
    assert status_response.json()["configured"] is True
    assert status_response.json()["api_key_last4"] == "1234"


def test_connect_credentials_never_returns_the_plaintext_secret(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "super-secret-value"},
        headers=_headers(),
    )
    assert "super-secret-value" not in response.text


def test_connect_credentials_rejects_a_key_with_withdrawals_enabled(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    fake.can_withdraw = True

    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "unsafe-key-9999", "api_secret": "unsafe-secret"},
        headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["connected"] is False

    assert client.get("/settings/binance-credentials").json()["configured"] is False


def test_connect_credentials_rejects_a_key_that_fails_verification(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, fake = authorized_client
    fake.error = BinanceAuthenticationError("bad signature")

    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "bad-key-0000", "api_secret": "bad-secret"},
        headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["connected"] is False


def test_connect_credentials_without_encryption_key_is_503(
    monkeypatch: pytest.MonkeyPatch,
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    monkeypatch.delenv("HERMES_CREDENTIALS_ENCRYPTION_KEY", raising=False)

    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "my-real-secret"},
        headers=_headers(),
    )
    assert response.status_code == 503


def test_disconnect_credentials(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "my-real-secret"},
        headers=_headers(),
    )

    response = client.delete(
        "/settings/binance-credentials", headers=_headers("disconnect-key")
    )
    assert response.status_code == 200
    assert response.json() == {"connected": False}
    assert client.get("/settings/binance-credentials").json()["configured"] is False


def test_disconnect_credentials_is_idempotent(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.delete(
        "/settings/binance-credentials", headers=_headers("disconnect-key")
    )
    assert response.status_code == 200


def test_put_credentials_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "my-real-secret"},
        headers={"Idempotency-Key": "test-key-1"},
    )
    assert response.status_code == 403


def test_put_credentials_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/binance-credentials",
        json={"api_key": "my-real-key-1234", "api_secret": "my-real-secret"},
        headers={"Origin": _ALLOWED_ORIGIN},
    )
    assert response.status_code == 422


# --- risk limits -------------------------------------------------------------


def test_risk_limits_default_to_unconfigured(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/settings/risk-limits")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "max_order_notional_quote": None,
        "max_symbol_exposure_pct": None,
        "max_total_exposure_pct": None,
        "max_daily_loss_pct": None,
        "max_open_positions": None,
        "allowed_symbols": None,
    }


def test_put_risk_limits_round_trips_and_normalizes_symbols(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/risk-limits",
        json={
            "max_order_notional_quote": "5000",
            "max_symbol_exposure_pct": "25",
            "max_total_exposure_pct": "80",
            "max_daily_loss_pct": "10",
            "max_open_positions": 3,
            "allowed_symbols": ["btcusdt", "ETHUSDT", "btcusdt"],
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["max_order_notional_quote"]) == Decimal("5000")
    assert body["max_open_positions"] == 3
    assert body["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]

    # The DB round-trip may reformat decimal precision (Numeric column
    # scale) but must never change the actual value or the symbol list.
    persisted = client.get("/settings/risk-limits").json()
    assert Decimal(persisted["max_order_notional_quote"]) == Decimal("5000")
    assert persisted["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_put_risk_limits_rejects_a_non_positive_notional(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/risk-limits",
        json={"max_order_notional_quote": "0"},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_put_risk_limits_rejects_a_percentage_over_100(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/risk-limits",
        json={"max_symbol_exposure_pct": "150"},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_put_risk_limits_rejects_zero_open_positions(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/risk-limits", json={"max_open_positions": 0}, headers=_headers()
    )
    assert response.status_code == 422


def test_put_risk_limits_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/risk-limits",
        json={},
        headers={"Idempotency-Key": "test-key-1"},
    )
    assert response.status_code == 403


# --- Simulation risk limits ---------------------------------------------------------


def test_simulation_risk_limits_default_to_sensible_non_null_values(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    """Unlike /settings/risk-limits, a brand-new user gets working
    defaults here, never null -- Simulation must never require setup."""
    client, _fake = authorized_client
    response = client.get("/settings/simulation-risk-limits")
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["max_order_notional_quote"]) == Decimal("1000")
    assert Decimal(body["max_symbol_exposure_pct"]) == Decimal("50")
    assert Decimal(body["max_total_exposure_pct"]) == Decimal("100")
    assert Decimal(body["max_daily_loss_pct"]) == Decimal("20")
    assert body["max_open_positions"] == 5
    assert body["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_put_simulation_risk_limits_round_trips_and_normalizes_symbols(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/simulation-risk-limits",
        json={
            "max_order_notional_quote": "5000",
            "max_symbol_exposure_pct": "25",
            "max_total_exposure_pct": "80",
            "max_daily_loss_pct": "10",
            "max_open_positions": 3,
            "allowed_symbols": ["btcusdt", "ETHUSDT", "btcusdt"],
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["max_order_notional_quote"]) == Decimal("5000")
    assert body["max_open_positions"] == 3
    assert body["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]

    persisted = client.get("/settings/simulation-risk-limits").json()
    assert Decimal(persisted["max_order_notional_quote"]) == Decimal("5000")
    assert persisted["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_put_simulation_risk_limits_rejects_a_non_positive_notional(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/simulation-risk-limits",
        json={
            "max_order_notional_quote": "0",
            "max_symbol_exposure_pct": "50",
            "max_total_exposure_pct": "100",
            "max_daily_loss_pct": "20",
            "max_open_positions": 5,
            "allowed_symbols": ["BTCUSDT"],
        },
        headers=_headers(),
    )
    assert response.status_code == 422


def test_put_simulation_risk_limits_rejects_an_empty_symbol_list(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    """Unlike the real-order limits, an empty list is never valid here --
    a Simulation limit can be changed, never un-set."""
    client, _fake = authorized_client
    response = client.put(
        "/settings/simulation-risk-limits",
        json={
            "max_order_notional_quote": "1000",
            "max_symbol_exposure_pct": "50",
            "max_total_exposure_pct": "100",
            "max_daily_loss_pct": "20",
            "max_open_positions": 5,
            "allowed_symbols": [],
        },
        headers=_headers(),
    )
    assert response.status_code == 422


def test_put_simulation_risk_limits_without_origin_header_is_403(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/simulation-risk-limits",
        json={
            "max_order_notional_quote": "1000",
            "max_symbol_exposure_pct": "50",
            "max_total_exposure_pct": "100",
            "max_daily_loss_pct": "20",
            "max_open_positions": 5,
            "allowed_symbols": ["BTCUSDT"],
        },
        headers={"Idempotency-Key": "test-key-1"},
    )
    assert response.status_code == 403


# --- personal trading switch ------------------------------------------------------


def test_trading_switch_defaults_to_enabled(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.get("/settings/trading-switch")
    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_put_trading_switch_round_trips(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    off_response = client.put(
        "/settings/trading-switch", json={"enabled": False}, headers=_headers()
    )
    assert off_response.status_code == 200
    assert off_response.json() == {"enabled": False}
    assert client.get("/settings/trading-switch").json() == {"enabled": False}

    on_response = client.put(
        "/settings/trading-switch",
        json={"enabled": True},
        headers=_headers("switch-key-2"),
    )
    assert on_response.json() == {"enabled": True}


def test_put_trading_switch_without_idempotency_key_is_422(
    authorized_client: tuple[TestClient, _FakeBinanceClient],
) -> None:
    client, _fake = authorized_client
    response = client.put(
        "/settings/trading-switch",
        json={"enabled": False},
        headers={"Origin": _ALLOWED_ORIGIN},
    )
    assert response.status_code == 422
