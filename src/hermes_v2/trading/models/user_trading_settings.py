"""A user's own risk limits and personal trading switch.

One row per user (`user_id` unique). Mirrors `RiskEngine.RiskLimits`'
six fields exactly, with the same fail-closed semantics: `NULL` on any
column means "not configured," which `RiskEngine` treats as reject-on-that-
dimension — the same meaning an unset `HERMES_RISK_*` env var already has
today. A row that doesn't exist yet (a brand-new user) is treated the
same as a row with every limit `NULL` — see
`user_risk_settings_service.get_user_risk_limits`.

`trading_enabled` is this user's own kill switch — defaults to `true`
(opt-out), unlike the global switch's opt-in-by-nature default, because
it gates a self-service convenience pause, not the platform's only line
of defense against real money moving. See
`docs/architecture/multi-tenant-trading.md` for the full two-tier
kill-switch policy (`kill_switch.is_trading_permitted`).

The `simulation_*` columns are a separate, `NOT NULL` twin of the six
real-order limits above, each with a `server_default` (see the
`20260818_0001` migration) — unlike the real-order columns, these are
never "not configured": every row always holds a concrete value, so a
brand-new user's Simulation bots work immediately with sensible
defaults, and Settings just lets them override the numbers, never
un-set them. This is deliberately a *different* fail-open-with-defaults
posture than the real-order columns' fail-closed-on-`NULL` one — see
`user_risk_settings_service.py` for why that's safe (virtual money) and
`docs/architecture/multi-tenant-trading.md` for why Simulation used to
read a global, operator-only env var instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base


class UserTradingSettings(Base):
    __tablename__ = "user_trading_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    trading_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    max_order_notional_quote: Mapped[str | None] = mapped_column(
        Numeric(precision=28, scale=10)
    )
    max_symbol_exposure_pct: Mapped[str | None] = mapped_column(
        Numeric(precision=6, scale=3)
    )
    max_total_exposure_pct: Mapped[str | None] = mapped_column(
        Numeric(precision=6, scale=3)
    )
    max_daily_loss_pct: Mapped[str | None] = mapped_column(
        Numeric(precision=6, scale=3)
    )
    max_open_positions: Mapped[int | None] = mapped_column(Integer)
    allowed_symbols: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(20)).with_variant(JSON(), "sqlite")
    )
    simulation_max_order_notional_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, server_default="1000"
    )
    simulation_max_symbol_exposure_pct: Mapped[str] = mapped_column(
        Numeric(precision=6, scale=3), nullable=False, server_default="50"
    )
    simulation_max_total_exposure_pct: Mapped[str] = mapped_column(
        Numeric(precision=6, scale=3), nullable=False, server_default="100"
    )
    simulation_max_daily_loss_pct: Mapped[str] = mapped_column(
        Numeric(precision=6, scale=3), nullable=False, server_default="20"
    )
    simulation_max_open_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    simulation_allowed_symbols: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)).with_variant(JSON(), "sqlite"),
        nullable=False,
        server_default="{BTCUSDT,ETHUSDT}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["UserTradingSettings"]
