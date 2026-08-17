"""A user's own Binance API credentials — encrypted at rest.

One row per user (`user_id` unique, same one-row-per-owner convention as
`BotPosition`/`SimulationAccount`). `api_key_ciphertext`/
`api_secret_ciphertext` are `MultiFernet` tokens (see
`trading/credentials_encryption.py`) — plaintext is never stored, never
logged, and never serialized back into any API response. `api_key_last4`
is captured separately at write time (not derived from the ciphertext)
purely for display, the same "last 4 digits" convention a credit-card UI
uses — knowing the last 4 characters of an API key reveals nothing
useful to an attacker.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hermes_v2.database.connection import Base


class UserBinanceCredential(Base):
    """A user's connected Binance account. Verified live against Binance
    (`get_account_info()`, withdrawals-disabled check) before this row is
    ever written — see `binance_credentials_service.connect_credentials`."""

    __tablename__ = "user_binance_credentials"

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
    api_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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


__all__ = ["UserBinanceCredential"]
