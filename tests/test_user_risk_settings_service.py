"""Integration tests for user_risk_settings_service.py against real
Postgres."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.risk_engine import RiskLimits
from hermes_v2.trading.user_risk_settings_service import (
    get_user_risk_limits,
    get_user_simulation_risk_limits,
    is_user_trading_enabled,
    save_user_risk_limits,
    save_user_simulation_risk_limits,
    set_user_trading_enabled,
)

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


def _make_user(session: Session, email: str = "trader@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def test_default_limits_are_all_none_never_a_fabricated_value(session: Session) -> None:
    user = _make_user(session)
    limits = get_user_risk_limits(session, user.id)
    assert limits == RiskLimits(
        max_order_notional_quote=None,
        max_symbol_exposure_pct=None,
        max_total_exposure_pct=None,
        max_daily_loss_pct=None,
        max_open_positions=None,
        allowed_symbols=None,
    )


def test_save_and_read_round_trip_every_field(session: Session) -> None:
    user = _make_user(session)
    limits = RiskLimits(
        max_order_notional_quote=Decimal("5000"),
        max_symbol_exposure_pct=Decimal("25.5"),
        max_total_exposure_pct=Decimal("80"),
        max_daily_loss_pct=Decimal("10"),
        max_open_positions=3,
        allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )
    save_user_risk_limits(session, user.id, limits)
    session.commit()

    result = get_user_risk_limits(session, user.id)
    assert result.max_order_notional_quote == Decimal("5000")
    assert result.max_symbol_exposure_pct == Decimal("25.5")
    assert result.max_total_exposure_pct == Decimal("80")
    assert result.max_daily_loss_pct == Decimal("10")
    assert result.max_open_positions == 3
    assert result.allowed_symbols == frozenset({"BTCUSDT", "ETHUSDT"})


def test_saving_again_overwrites_rather_than_duplicating(session: Session) -> None:
    user = _make_user(session)
    save_user_risk_limits(
        session,
        user.id,
        RiskLimits(Decimal("1000"), None, None, None, None, None),
    )
    session.commit()

    save_user_risk_limits(
        session,
        user.id,
        RiskLimits(Decimal("2000"), None, None, None, None, None),
    )
    session.commit()

    assert get_user_risk_limits(session, user.id).max_order_notional_quote == Decimal(
        "2000"
    )


def test_two_users_limits_are_fully_isolated(session: Session) -> None:
    user_a = _make_user(session, "a@example.com")
    user_b = _make_user(session, "b@example.com")

    save_user_risk_limits(
        session,
        user_a.id,
        RiskLimits(Decimal("100"), None, None, None, None, frozenset({"BTCUSDT"})),
    )
    save_user_risk_limits(
        session,
        user_b.id,
        RiskLimits(Decimal("999999"), None, None, None, None, frozenset({"ETHUSDT"})),
    )
    session.commit()

    limits_a = get_user_risk_limits(session, user_a.id)
    limits_b = get_user_risk_limits(session, user_b.id)
    assert limits_a.max_order_notional_quote == Decimal("100")
    assert limits_a.allowed_symbols == frozenset({"BTCUSDT"})
    assert limits_b.max_order_notional_quote == Decimal("999999")
    assert limits_b.allowed_symbols == frozenset({"ETHUSDT"})


# --- Simulation risk limits ------------------------------------------------------
#
# Opposite posture from the real-order limits above: a brand-new user
# (no row at all) gets working, non-None defaults -- never a value that
# blocks every Simulation order until Settings is filled in.


def test_default_simulation_limits_are_never_none_for_a_new_user(
    session: Session,
) -> None:
    user = _make_user(session)
    limits = get_user_simulation_risk_limits(session, user.id)
    assert limits.max_order_notional_quote == Decimal("1000")
    assert limits.max_symbol_exposure_pct == Decimal("50")
    assert limits.max_total_exposure_pct == Decimal("100")
    assert limits.max_daily_loss_pct == Decimal("20")
    assert limits.max_open_positions == 5
    assert limits.allowed_symbols == frozenset({"BTCUSDT", "ETHUSDT"})


def test_simulation_limits_save_and_read_round_trip_every_field(
    session: Session,
) -> None:
    user = _make_user(session)
    limits = RiskLimits(
        max_order_notional_quote=Decimal("5000"),
        max_symbol_exposure_pct=Decimal("25.5"),
        max_total_exposure_pct=Decimal("80"),
        max_daily_loss_pct=Decimal("10"),
        max_open_positions=3,
        allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )
    save_user_simulation_risk_limits(session, user.id, limits)
    session.commit()

    result = get_user_simulation_risk_limits(session, user.id)
    assert result.max_order_notional_quote == Decimal("5000")
    assert result.max_symbol_exposure_pct == Decimal("25.5")
    assert result.max_total_exposure_pct == Decimal("80")
    assert result.max_daily_loss_pct == Decimal("10")
    assert result.max_open_positions == 3
    assert result.allowed_symbols == frozenset({"BTCUSDT", "ETHUSDT"})


def test_simulation_limits_are_independent_of_real_order_limits(
    session: Session,
) -> None:
    """Saving one never touches the other -- they're deliberately separate
    dimensions of the same per-user row."""
    user = _make_user(session)
    save_user_risk_limits(
        session,
        user.id,
        RiskLimits(Decimal("1"), None, None, None, None, frozenset({"ETHUSDT"})),
    )
    session.commit()

    real = get_user_risk_limits(session, user.id)
    simulation = get_user_simulation_risk_limits(session, user.id)
    assert real.max_order_notional_quote == Decimal("1")
    assert simulation.max_order_notional_quote == Decimal("1000")  # untouched default


def test_trading_switch_defaults_to_enabled_for_a_new_user(session: Session) -> None:
    user = _make_user(session)
    assert is_user_trading_enabled(session, user.id) is True


def test_trading_switch_can_be_turned_off_and_back_on(session: Session) -> None:
    user = _make_user(session)

    set_user_trading_enabled(session, user.id, False)
    session.commit()
    assert is_user_trading_enabled(session, user.id) is False

    set_user_trading_enabled(session, user.id, True)
    session.commit()
    assert is_user_trading_enabled(session, user.id) is True


def test_trading_switch_is_isolated_per_user(session: Session) -> None:
    user_a = _make_user(session, "a@example.com")
    user_b = _make_user(session, "b@example.com")

    set_user_trading_enabled(session, user_a.id, False)
    session.commit()

    assert is_user_trading_enabled(session, user_a.id) is False
    assert is_user_trading_enabled(session, user_b.id) is True  # untouched
