"""Order model — Hermes's own operational record of an order.

Binance remains the final authority on whether an order actually filled;
this row is Hermes's record of what it asked Binance to do and the last
state it observed, refreshed by `hermes_v2.trading.reconciliation`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_v2.database.connection import Base

if TYPE_CHECKING:
    from hermes_v2.trading.models.order_event import OrderEvent


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    """Hermes-side lifecycle. Mirrors Binance's own order states plus two
    Hermes-only states (`PENDING`, `FAILED`) for the window before Binance
    has acknowledged the order at all."""

    PENDING = "PENDING"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)

_CANCELABLE_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})


def is_terminal_status(status: OrderStatus) -> bool:
    """Whether an order in this status will never change again on its own."""
    return status in _TERMINAL_STATUSES


def is_cancelable_status(status: OrderStatus) -> bool:
    """Whether an order in this status can still be cancelled."""
    return status in _CANCELABLE_STATUSES


class Order(Base):
    """Hermes's operational record of one order submitted to Binance."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL for every manual order — only set when a Bot's pause/resume
    # placed this order (see hermes_v2.trading.bot_service). SET NULL on
    # bot deletion (not implemented in v1) so a historical order never
    # disappears just because its bot did.
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, name="order_side"), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, name="order_type"), nullable=False
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )

    requested_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False
    )
    requested_price: Mapped[str | None] = mapped_column(Numeric(precision=28, scale=10))
    executed_quantity: Mapped[str] = mapped_column(
        Numeric(precision=28, scale=10), nullable=False, default="0"
    )
    average_fill_price: Mapped[str | None] = mapped_column(
        Numeric(precision=28, scale=10)
    )

    binance_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    binance_client_order_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )

    error_message: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


__all__ = [
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "is_cancelable_status",
    "is_terminal_status",
]
