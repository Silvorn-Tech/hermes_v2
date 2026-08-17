"""Integration tests for kill_switch.py -- the full 2x2 truth table of
the global (env var) and per-user (DB) switches, both required."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.kill_switch import is_trading_permitted
from hermes_v2.trading.user_risk_settings_service import set_user_trading_enabled

pytestmark = pytest.mark.database


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE user_trading_settings, role_permissions, "
                "user_roles, identities, sessions, permissions, roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def _make_user(session: Session) -> User:
    user = User(email="trader@example.com")
    session.add(user)
    session.flush()
    return user


def test_global_on_user_on_permits_trading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    set_user_trading_enabled(session, user.id, True)
    session.commit()

    assert is_trading_permitted(session, user.id) is True


def test_global_on_user_off_blocks_trading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)
    set_user_trading_enabled(session, user.id, False)
    session.commit()

    assert is_trading_permitted(session, user.id) is False


def test_global_off_user_on_blocks_trading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    set_user_trading_enabled(session, user.id, True)
    session.commit()

    assert is_trading_permitted(session, user.id) is False


def test_global_off_user_off_blocks_trading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    user = _make_user(session)
    set_user_trading_enabled(session, user.id, False)
    session.commit()

    assert is_trading_permitted(session, user.id) is False


def test_global_on_user_switch_untouched_defaults_to_permitted(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    user = _make_user(session)

    assert is_trading_permitted(session, user.id) is True
