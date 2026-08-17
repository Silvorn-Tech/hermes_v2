"""Per-user risk limits and the personal trading switch.

Reuses `risk_engine.RiskLimits` directly rather than a parallel
dataclass — the six fields and their fail-closed "`None` means not
configured, reject on that dimension" meaning are identical to the
global, `HERMES_RISK_*`-env-var-based limits `load_risk_limits()`
already reads; only the source (a per-user database row instead of the
process environment) differs. Applies to **real** orders only —
`SimulationOrderService` stays on the global, env-based limits
deliberately (see `docs/architecture/multi-tenant-trading.md`): a
brand-new user's per-user limits default to all-`None`, and gating
Simulation on that would reject every simulation order for every new
user until they filled in six Settings fields, defeating "Simulation
never requires any setup."

The personal trading switch (`trading_enabled`) is the opposite default
from the global kill switch: it starts `True` (opt-out), because it
gates a self-service convenience pause a user flips on themselves, not
the platform's actual defense against real money moving (that's
permission gating + the global switch + per-user Binance credentials +
RiskEngine, none of which this boolean is). A user with no
`UserTradingSettings` row is treated exactly like one with
`trading_enabled=True` and every limit `None` — the same "no row yet"
semantics `SimulationAccount`/`BotPosition` never need because those are
always created alongside their owning `Bot`; this one legitimately can
be absent for a long time (a user who's never visited Settings).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.trading.models.user_trading_settings import UserTradingSettings
from hermes_v2.trading.risk_engine import RiskLimits


def _get_or_none(session: Session, user_id: uuid.UUID) -> UserTradingSettings | None:
    return session.scalar(
        select(UserTradingSettings).where(UserTradingSettings.user_id == user_id)
    )


def get_user_risk_limits(session: Session, user_id: uuid.UUID) -> RiskLimits:
    row = _get_or_none(session, user_id)
    if row is None:
        return RiskLimits(
            max_order_notional_quote=None,
            max_symbol_exposure_pct=None,
            max_total_exposure_pct=None,
            max_daily_loss_pct=None,
            max_open_positions=None,
            allowed_symbols=None,
        )
    return RiskLimits(
        max_order_notional_quote=(
            Decimal(row.max_order_notional_quote)
            if row.max_order_notional_quote is not None
            else None
        ),
        max_symbol_exposure_pct=(
            Decimal(row.max_symbol_exposure_pct)
            if row.max_symbol_exposure_pct is not None
            else None
        ),
        max_total_exposure_pct=(
            Decimal(row.max_total_exposure_pct)
            if row.max_total_exposure_pct is not None
            else None
        ),
        max_daily_loss_pct=(
            Decimal(row.max_daily_loss_pct)
            if row.max_daily_loss_pct is not None
            else None
        ),
        max_open_positions=row.max_open_positions,
        allowed_symbols=(
            frozenset(row.allowed_symbols) if row.allowed_symbols else None
        ),
    )


def save_user_risk_limits(
    session: Session, user_id: uuid.UUID, limits: RiskLimits
) -> RiskLimits:
    row = _get_or_none(session, user_id)
    if row is None:
        row = UserTradingSettings(user_id=user_id)
        session.add(row)

    row.max_order_notional_quote = limits.max_order_notional_quote
    row.max_symbol_exposure_pct = limits.max_symbol_exposure_pct
    row.max_total_exposure_pct = limits.max_total_exposure_pct
    row.max_daily_loss_pct = limits.max_daily_loss_pct
    row.max_open_positions = limits.max_open_positions
    row.allowed_symbols = (
        sorted(limits.allowed_symbols) if limits.allowed_symbols else None
    )
    session.flush()
    return get_user_risk_limits(session, user_id)


def is_user_trading_enabled(session: Session, user_id: uuid.UUID) -> bool:
    row = _get_or_none(session, user_id)
    return True if row is None else row.trading_enabled


def set_user_trading_enabled(
    session: Session, user_id: uuid.UUID, enabled: bool
) -> bool:
    row = _get_or_none(session, user_id)
    if row is None:
        row = UserTradingSettings(user_id=user_id, trading_enabled=enabled)
        session.add(row)
    else:
        row.trading_enabled = enabled
    session.flush()
    return row.trading_enabled


__all__ = [
    "get_user_risk_limits",
    "is_user_trading_enabled",
    "save_user_risk_limits",
    "set_user_trading_enabled",
]
