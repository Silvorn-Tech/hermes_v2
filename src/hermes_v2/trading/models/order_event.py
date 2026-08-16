"""OrderEvent — the append-only execution-attempt history for one order."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_v2.database.connection import Base

if TYPE_CHECKING:
    from hermes_v2.trading.models.order import Order


class OrderEventType(str, enum.Enum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTED = "SUBMITTED"
    BINANCE_ACK = "BINANCE_ACK"
    BINANCE_ERROR = "BINANCE_ERROR"
    RECONCILED = "RECONCILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"


class OrderEvent(Base):
    """One state transition or execution attempt for an `Order`."""

    __tablename__ = "order_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[OrderEventType] = mapped_column(
        Enum(OrderEventType, name="order_event_type"), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    order: Mapped["Order"] = relationship(back_populates="events")


__all__ = ["OrderEvent", "OrderEventType"]
