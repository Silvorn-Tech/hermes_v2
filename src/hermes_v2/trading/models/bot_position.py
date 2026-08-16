"""BotPosition — Hermes's own book of record for what a bot currently
holds. Not derived from Binance the way `PositionsService` derives manual
positions: a bot's exposure is tracked explicitly here precisely because
Binance's account-wide balance can't tell two bots' holdings of the same
symbol apart, or a bot's holding from a manual trade (see
`hermes_v2.trading.bot_service`'s module docstring).

One row per bot (`bot_id` is unique) — v1 assumes one instrument per bot.
This row doubles as the required pause/resume snapshot: `current_quantity`
is 0 while paused/stopped, `target_quantity` is what a resume should
re-acquire. It is a **current snapshot, not a ledger** — overwritten each
pause/resume cycle. Full history is recoverable via `orders.bot_id` +
`order_events` + `audit_log`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_v2.database.connection import Base

if TYPE_CHECKING:
    from hermes_v2.trading.models.bot import Bot


class BotPosition(Base):
    __tablename__ = "bot_positions"

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
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    current_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, default="0"
    )
    target_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    last_close_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    last_open_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    bot: Mapped["Bot"] = relationship(back_populates="position")


__all__ = ["BotPosition"]
