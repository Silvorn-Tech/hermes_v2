"""Initial protected Super Admin bootstrap operation."""

import os
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.auth.models import Role, User, UserStatus

SUPER_ADMIN_ROLE_NAME = "SUPER_ADMIN"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BootstrapConfigurationError(ValueError):
    """Raised when the administrator bootstrap configuration is invalid."""


class BootstrapStateError(RuntimeError):
    """Raised when the existing authorization state is unsafe to bootstrap."""


def normalized_admin_email_from_environment() -> str:
    """Read, validate, and normalize the configured administrator email."""
    configured_email = os.environ.get("HERMES_ADMIN_EMAIL")
    if configured_email is None:
        message = "HERMES_ADMIN_EMAIL must be configured"
        raise BootstrapConfigurationError(message)

    normalized_email = configured_email.strip().lower()
    if not _EMAIL_PATTERN.fullmatch(normalized_email):
        message = "HERMES_ADMIN_EMAIL must be a valid email address"
        raise BootstrapConfigurationError(message)
    return normalized_email


def bootstrap_super_admin(session: Session) -> uuid.UUID:
    """Create or reuse the configured administrator and grant SUPER_ADMIN.

    The operation deliberately creates no external identity. Google OAuth will
    later associate Google's stable ``sub`` with this internal user.
    """
    admin_email = normalized_admin_email_from_environment()

    if not session.in_transaction():
        with session.begin():
            return _bootstrap_super_admin_transaction(session, admin_email)

    return _bootstrap_super_admin_transaction(session, admin_email)


def _bootstrap_super_admin_transaction(session: Session, admin_email: str) -> uuid.UUID:
    """Execute the protected admin bootstrap within the current transaction."""
    super_admin = session.scalar(select(Role).where(Role.name == SUPER_ADMIN_ROLE_NAME))
    if super_admin is None or not super_admin.system_role:
        message = "Protected SUPER_ADMIN role is missing or invalid"
        raise BootstrapStateError(message)

    current_admins = session.scalars(
        select(User).join(User.roles).where(Role.name == SUPER_ADMIN_ROLE_NAME)
    ).all()
    if any(user.email != admin_email for user in current_admins):
        message = "SUPER_ADMIN is already assigned to a different user"
        raise BootstrapStateError(message)

    user = session.scalar(select(User).where(User.email == admin_email))
    if user is None:
        user = User(email=admin_email, status=UserStatus.ACTIVE)
        session.add(user)
        session.flush()

    if super_admin not in user.roles:
        user.roles.append(super_admin)

    session.flush()
    return user.id
