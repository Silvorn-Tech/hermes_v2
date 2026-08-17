"""The Simulation (paper trading) virtual ledger — structurally separate
from `orders`/`order_events`/`portfolio_snapshots`. This is a deliberate
architectural choice, not an oversight: a Postgres-level guarantee that
combining real and simulated data requires an explicit `UNION` no query
could write by accident, rather than a `WHERE execution_mode = ...`
someone could forget. See `docs/architecture/simulation.md`.

`side`/`order_type` reuse `OrderSide`/`OrderType` as Python enum classes
(the same values, no duplicated business meaning) but map to their own,
distinctly-named Postgres enum types (`simulation_order_side`/
`simulation_order_type`) rather than the real `order_side`/`order_type`
types, so this table's schema never depends on the real orders table's
type lifecycle.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base
from hermes_v2.trading.models.order import OrderSide, OrderType


class SimulationOrderStatus(str, enum.Enum):
    """Simulated fills are always synchronous — there is no real matching
    engine to introduce latency, so unlike `OrderStatus` there is no
    `NEW`/`PARTIALLY_FILLED`/`PENDING_CANCEL`: a simulated order resolves
    to exactly one of these the moment it's evaluated."""

    FILLED = "FILLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class SimulationAccount(Base):
    """One virtual account per bot — `bot_id` is unique, matching
    `BotPosition`'s own one-row-per-bot convention. `initial_capital_quote`
    is frozen at creation time (never re-read from config afterward);
    `cash_balance_quote` is the current virtual cash, mutated by
    `SimulationOrderService` as fills happen."""

    __tablename__ = "simulation_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    quote_asset: Mapped[str] = mapped_column(String(10), nullable=False)
    initial_capital_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    cash_balance_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
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


class SimulationOrder(Base):
    """One row per simulated order attempt — `FILLED` (with `fill_price`/
    `executed_quantity`/`fee_quote`/`slippage_quote` populated) or
    `REJECTED`/`FAILED` (with `reason`, no fill fields). No partial-fill
    modeling in v1 — a simulated MARKET order always resolves fully or
    not at all."""

    __tablename__ = "simulation_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    simulation_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, name="simulation_order_side"), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, name="simulation_order_type"), nullable=False
    )
    status: Mapped[SimulationOrderStatus] = mapped_column(
        Enum(SimulationOrderStatus, name="simulation_order_status"), nullable=False
    )
    requested_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    fill_price: Mapped[str | None] = mapped_column(Numeric(precision=28, scale=10))
    executed_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, default="0"
    )
    fee_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, default="0"
    )
    slippage_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, default="0"
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SimulationSnapshot(Base):
    """Periodic per-bot snapshot of a simulation account's total value —
    the one metric (drawdown) that genuinely needs a time series rather
    than a live computation from current state. Unique on
    `(bot_id, snapshot_at)`, restart-safe via the same bucketed-timestamp
    + `ON CONFLICT DO NOTHING` pattern as the real `portfolio_snapshots`
    table (see `portfolio_snapshot_service.py`)."""

    __tablename__ = "simulation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "bot_id", "snapshot_at", name="uq_simulation_snapshots_bot_snapshot_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    cash_balance_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    position_value_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    total_value_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    exposure_pct: Mapped[str] = mapped_column(
        Numeric(precision=6, scale=3), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "SimulationAccount",
    "SimulationOrder",
    "SimulationOrderStatus",
    "SimulationSnapshot",
]
