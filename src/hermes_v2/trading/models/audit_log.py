"""AuditLogEntry — the durable, queryable record of every mutable trading
action, per `SECURITY_AUDIT.md` §22's pre-registered requirement. Written
by a service layer (`OrderService`, `BotService`), never by a route
handler directly, so every mutation is recorded exactly once regardless of
which endpoint triggered it. Never stores a secret, header, or raw Binance
payload — same discipline `BinanceClient` already enforces for its own
error messages.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base


class AuditResult(str, enum.Enum):
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20))
    quantity: Mapped[str | None] = mapped_column(Numeric(precision=28, scale=10))
    result: Mapped[AuditResult] = mapped_column(
        Enum(AuditResult, name="audit_result"), nullable=False
    )
    hermes_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    binance_order_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["AuditLogEntry", "AuditResult"]
