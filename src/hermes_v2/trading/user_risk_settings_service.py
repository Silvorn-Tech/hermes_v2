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

`get_user_simulation_risk_limits`/`save_user_simulation_risk_limits`
are the Simulation-only counterpart, reading/writing the `simulation_*`
columns. Unlike the real-order limits above, a value is never `None`:
a user with no row gets `_DEFAULT_SIMULATION_RISK_LIMITS` (this
module's own constant, kept in sync with the `20260818_0001` migration's
column `server_default`s — the same numbers either way, just the source
differs depending on whether a row exists yet), and Settings can only
ever change the numbers, never un-set them back to "not configured."
See that migration's docstring for why Simulation gets working defaults
where real orders deliberately don't.
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


# Mirrors the `20260818_0001` migration's column `server_default`s exactly —
# this is only the "no row yet" fallback; once a row exists (created by
# *any* Settings write, not just this one) the database's own defaults
# already apply. Deliberately more generous than one operator's real-money
# env-var choices: this is the shared starting point for every user's
# virtual money, not a single account's real risk tolerance.
_DEFAULT_SIMULATION_RISK_LIMITS = RiskLimits(
    max_order_notional_quote=Decimal("1000"),
    max_symbol_exposure_pct=Decimal("50"),
    max_total_exposure_pct=Decimal("100"),
    max_daily_loss_pct=Decimal("20"),
    max_open_positions=5,
    allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
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


def get_user_simulation_risk_limits(session: Session, user_id: uuid.UUID) -> RiskLimits:
    row = _get_or_none(session, user_id)
    if row is None:
        return _DEFAULT_SIMULATION_RISK_LIMITS
    return RiskLimits(
        max_order_notional_quote=Decimal(row.simulation_max_order_notional_quote),
        max_symbol_exposure_pct=Decimal(row.simulation_max_symbol_exposure_pct),
        max_total_exposure_pct=Decimal(row.simulation_max_total_exposure_pct),
        max_daily_loss_pct=Decimal(row.simulation_max_daily_loss_pct),
        max_open_positions=row.simulation_max_open_positions,
        allowed_symbols=frozenset(row.simulation_allowed_symbols),
    )


def save_user_simulation_risk_limits(
    session: Session, user_id: uuid.UUID, limits: RiskLimits
) -> RiskLimits:
    """Every field is required (`None` isn't a valid override for a
    Simulation limit — see this module's own docstring) — the route
    layer validates that before this is ever called."""
    row = _get_or_none(session, user_id)
    if row is None:
        row = UserTradingSettings(user_id=user_id)
        session.add(row)

    row.simulation_max_order_notional_quote = limits.max_order_notional_quote
    row.simulation_max_symbol_exposure_pct = limits.max_symbol_exposure_pct
    row.simulation_max_total_exposure_pct = limits.max_total_exposure_pct
    row.simulation_max_daily_loss_pct = limits.max_daily_loss_pct
    row.simulation_max_open_positions = limits.max_open_positions
    row.simulation_allowed_symbols = sorted(limits.allowed_symbols or [])
    session.flush()
    return get_user_simulation_risk_limits(session, user_id)


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
    "get_user_simulation_risk_limits",
    "is_user_trading_enabled",
    "save_user_risk_limits",
    "save_user_simulation_risk_limits",
    "set_user_trading_enabled",
]
