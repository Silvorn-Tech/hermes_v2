"""Role model for Hermes authorization."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_v2.auth.models.user import user_roles
from hermes_v2.database.connection import Base

if TYPE_CHECKING:
    from hermes_v2.auth.models.permission import Permission
    from hermes_v2.auth.models.user import User


class Role(Base):
    """A protected system role or an extensible future custom role."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_role: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    users: Mapped[list["User"]] = relationship(
        secondary=user_roles, back_populates="roles"
    )
    permissions: Mapped[list["Permission"]] = relationship(
        secondary="role_permissions", back_populates="roles"
    )
