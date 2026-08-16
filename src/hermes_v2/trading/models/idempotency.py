"""IdempotencyKey — generic dedupe record shared by every mutating trading
action (create order, cancel order, close position).

See `hermes_v2.trading.idempotency` for the read/check/store logic that uses
this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base


class IdempotencyKey(Base):
    """One caller-supplied idempotency key, scoped to a user and an endpoint."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "endpoint", "idempotency_key", name="uq_idempotency_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["IdempotencyKey"]
