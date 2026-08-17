"""PortfolioSnapshot — Hermes's own persisted history of the account's
portfolio value over time. The missing "historical baseline" that
`PortfolioService`'s docstring names as the reason it can't compute a
daily P&L figure.

Deliberately does not capture realized/unrealized P&L: neither has a
reliable source with the current architecture (see
`hermes_v2.trading.portfolio_snapshot_service`'s module docstring for
why) — only `total_value_quote` (mark-to-market equity),
`available_balance_quote`, and `exposure_quote`/`exposure_pct`, all
derived from the exact same `PortfolioService.get_portfolio()` call a
snapshot already has to make.

`snapshot_at` is the logical, interval-bucketed timestamp (e.g. exactly
on a 15-minute boundary) and is the idempotency key — see
`portfolio_snapshot_service.take_portfolio_snapshot`. `created_at` is
the literal insert time, kept separately for debugging; it is never used
for ordering or deduplication.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, unique=True, index=True
    )
    quote_asset: Mapped[str] = mapped_column(String(10), nullable=False)
    total_value_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    available_balance_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    exposure_quote: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    exposure_pct: Mapped[str] = mapped_column(
        Numeric(precision=6, scale=3), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["PortfolioSnapshot"]
