"""Bot — Hermes's own operational record of a trading bot.

A Bot is deliberately not "a Binance bot": `asset_class`/`execution_venue`
are separate fields so a future asset class or venue (e.g. Interactive
Brokers for EQUITY) is a new enum value, not a redesign. `strategy_model`
is a plain string, not a Postgres enum — the strategy/model space is
explicitly open-ended (crypto isn't always signal-based, stocks aren't
always GARCH) and meant to grow without a migration; `strategy_config`
reserves a JSON slot for future model parameters, unused in v1 since no
math models are implemented yet.

`risk_profile` is a stored, descriptive attribute only — it has no effect
on `RiskEngine`, which stays account-wide and unmodified. See
`hermes_v2.trading.bot_service` for the lifecycle this status field
supports (`ACTIVE`/`PAUSED`/`STOPPED`/`ERROR` are the stable states;
`PAUSING`/`RESUMING` are real, persisted, crash-recoverable transient
states — never just an in-memory flag).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_v2.database.connection import Base

if TYPE_CHECKING:
    from hermes_v2.trading.models.bot_position import BotPosition


class RiskProfile(str, enum.Enum):
    """Hermes has exactly these three risk profiles — never extended
    ad hoc. Purely descriptive in v1; see the module docstring."""

    SENTINEL = "SENTINEL"
    EQUILIBRIUM = "EQUILIBRIUM"
    VORTEX = "VORTEX"


class AssetClass(str, enum.Enum):
    CRYPTO = "CRYPTO"
    EQUITY = "EQUITY"


class ExecutionVenue(str, enum.Enum):
    """Binance is the only venue today. Adding Interactive Brokers (or
    another venue) later is a new enum value plus a migration, not a
    redesign of the Bot domain."""

    BINANCE = "BINANCE"


class BotStatus(str, enum.Enum):
    """`ACTIVE`/`PAUSED`/`STOPPED`/`ERROR` are the stable states.
    `PAUSING`/`RESUMING` are transient but persisted — a pause/resume in
    flight must survive a backend restart, so it can't be memory-only.
    `ERROR`'s only exit in v1 is `ERROR -> STOPPED` (see
    `hermes_v2.trading.bot_service` for why an automatic `ERROR ->
    ACTIVE` recovery isn't implemented yet)."""

    ACTIVE = "ACTIVE"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class Bot(Base):
    """Hermes's operational record of one trading bot. `asset_class`,
    `execution_venue`, and `instrument` are immutable after creation — see
    `BotService.update_bot`."""

    __tablename__ = "bots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_profile: Mapped[RiskProfile] = mapped_column(
        Enum(RiskProfile, name="bot_risk_profile"), nullable=False
    )
    asset_class: Mapped[AssetClass] = mapped_column(
        Enum(AssetClass, name="bot_asset_class"), nullable=False
    )
    execution_venue: Mapped[ExecutionVenue] = mapped_column(
        Enum(ExecutionVenue, name="bot_execution_venue"), nullable=False
    )
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_model: Mapped[str | None] = mapped_column(String(50))
    strategy_config: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[BotStatus] = mapped_column(
        Enum(BotStatus, name="bot_status"), nullable=False, default=BotStatus.PAUSED
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

    position: Mapped["BotPosition"] = relationship(
        back_populates="bot", uselist=False, cascade="all, delete-orphan"
    )


__all__ = ["AssetClass", "Bot", "BotStatus", "ExecutionVenue", "RiskProfile"]
