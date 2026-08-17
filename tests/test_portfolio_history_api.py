"""Integration tests for GET /portfolio/history: auth -> RBAC ->
snapshot query -> downsample -> response, through a real Postgres
session, mirroring test_trading_api.py's fixture shape.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.api.app import app
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.auth.session import create_session
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.models import PortfolioSnapshot

pytestmark = pytest.mark.database


@pytest.fixture()
def db_session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE portfolio_snapshots, role_permissions, user_roles, "
                "identities, sessions, permissions, roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture()
def authorized_client(db_session: Session) -> TestClient:
    seed_authorization_data(db_session)
    user = User(email="trader@example.com")
    db_session.add(user)
    db_session.flush()
    super_admin = db_session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    user.roles.append(super_admin)
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)
    return client


def _insert_snapshot(
    session: Session, snapshot_at: datetime, total_value_quote: str
) -> None:
    session.add(
        PortfolioSnapshot(
            id=uuid.uuid4(),
            snapshot_at=snapshot_at,
            quote_asset="USDT",
            total_value_quote=Decimal(total_value_quote),
            available_balance_quote=Decimal("100"),
            exposure_quote=Decimal(total_value_quote) - Decimal("100"),
            exposure_pct=Decimal("50"),
        )
    )
    session.commit()


# --- authentication / authorization ---------------------------------------------


def test_unauthenticated_request_is_401() -> None:
    client = TestClient(app)
    response = client.get("/portfolio/history", params={"period": "1D"})
    assert response.status_code == 401


def test_authenticated_without_permission_is_403(db_session: Session) -> None:
    seed_authorization_data(db_session)
    user = User(email="no-perms@example.com")
    db_session.add(user)
    db_session.flush()
    _, raw_token = create_session(db_session, user, timedelta(hours=1))
    db_session.commit()

    client = TestClient(app)
    client.cookies.set("hermes_session", raw_token)

    response = client.get("/portfolio/history", params={"period": "1D"})
    assert response.status_code == 403


# --- validation -------------------------------------------------------------------


def test_invalid_period_is_422(authorized_client: TestClient) -> None:
    response = authorized_client.get("/portfolio/history", params={"period": "2W"})
    assert response.status_code == 422


def test_missing_period_is_422(authorized_client: TestClient) -> None:
    response = authorized_client.get("/portfolio/history")
    assert response.status_code == 422


# --- empty history ------------------------------------------------------------


def test_empty_history_returns_200_with_no_points(
    authorized_client: TestClient,
) -> None:
    response = authorized_client.get("/portfolio/history", params={"period": "1D"})
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "1D"
    assert body["points"] == []
    assert body["return_pct"] is None
    assert body["max_drawdown_pct"] is None


# --- each supported period ---------------------------------------------------------


@pytest.mark.parametrize("period", ["1D", "7D", "30D", "90D", "1Y"])
def test_each_supported_period_returns_200(
    authorized_client: TestClient, db_session: Session, period: str
) -> None:
    now = datetime.now(UTC)
    _insert_snapshot(db_session, now - timedelta(minutes=5), "1000")
    _insert_snapshot(db_session, now, "1100")

    response = authorized_client.get("/portfolio/history", params={"period": period})

    assert response.status_code == 200
    body = response.json()
    assert body["period"] == period
    assert len(body["points"]) >= 1


def test_snapshots_outside_the_window_are_excluded(
    authorized_client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    _insert_snapshot(db_session, now - timedelta(days=2), "500")  # outside 1D window
    _insert_snapshot(db_session, now, "1000")

    response = authorized_client.get("/portfolio/history", params={"period": "1D"})

    body = response.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["v"] == "1000.0000000000"


def test_response_points_are_chronologically_ordered(
    authorized_client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    _insert_snapshot(db_session, now - timedelta(minutes=10), "1000")
    _insert_snapshot(db_session, now - timedelta(minutes=20), "900")
    _insert_snapshot(db_session, now, "1100")

    response = authorized_client.get("/portfolio/history", params={"period": "1D"})

    body = response.json()
    timestamps = [point["t"] for point in body["points"]]
    assert timestamps == sorted(timestamps)


def test_return_and_drawdown_are_computed_from_real_points(
    authorized_client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    _insert_snapshot(db_session, now - timedelta(minutes=30), "1000")
    _insert_snapshot(db_session, now - timedelta(minutes=15), "800")
    _insert_snapshot(db_session, now, "1200")

    response = authorized_client.get("/portfolio/history", params={"period": "1D"})

    body = response.json()
    assert body["return_pct"] == "20.0"  # (1200-1000)/1000*100
    assert body["max_drawdown_pct"] == "20.0"  # (1000-800)/1000*100


def test_response_never_includes_binance_credentials_or_secrets(
    authorized_client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    _insert_snapshot(db_session, now, "1000")

    response = authorized_client.get("/portfolio/history", params={"period": "1D"})

    body = response.json()
    assert set(body.keys()) == {
        "period",
        "quote_asset",
        "points",
        "return_pct",
        "max_drawdown_pct",
    }
    for point in body["points"]:
        assert set(point.keys()) == {"t", "v"}
