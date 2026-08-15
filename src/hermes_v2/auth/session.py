"""Cryptographic session helpers for Hermes authentication."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.auth.models import Session as SessionModel, User, UserStatus

_SESSION_COOKIE_NAME = "hermes_session"
_DEFAULT_SESSION_TTL_SECONDS = 86_400


def get_session_cookie_name() -> str:
    """Return the HTTP-only Hermes session cookie name."""
    return _SESSION_COOKIE_NAME


def is_cookie_secure() -> bool:
    """Read the secure-only cookie setting from the environment."""
    value = os.environ.get("HERMES_COOKIE_SECURE", "false")
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def get_session_ttl() -> timedelta:
    """Return the configured Hermes session TTL in seconds."""
    raw_value = os.environ.get(
        "HERMES_SESSION_TTL_SECONDS",
        str(_DEFAULT_SESSION_TTL_SECONDS),
    )
    try:
        seconds = int(raw_value)
    except ValueError as exc:
        raise ValueError("HERMES_SESSION_TTL_SECONDS must be an integer") from exc
    return timedelta(seconds=max(1, seconds))


def serialize_authenticated_user(user: User) -> dict[str, Any]:
    """Return the safe user payload for authenticated API responses."""
    roles = []
    for role in getattr(user, "roles", []) or []:
        if hasattr(role, "name"):
            roles.append(role.name)
        elif isinstance(role, str):
            roles.append(role)

    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name or user.email,
        "roles": roles,
    }


def _hash_token(raw_token: str) -> str:
    """Hash a raw bearer token before persisting it."""
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("raw_token must be a non-empty string")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(
    session: Session,
    user: User,
    ttl: timedelta,
) -> tuple[SessionModel, str]:
    """Create a fresh Hermes session and return the raw token to the caller."""
    raw_token = secrets.token_urlsafe(32)
    created_at = datetime.now(timezone.utc)
    session_record = SessionModel(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        created_at=created_at,
        expires_at=created_at + ttl,
    )
    session.add(session_record)
    session.flush()
    return session_record, raw_token


def get_user_from_session(session: Session, raw_token: str) -> User | None:
    """Resolve an active user from a valid Hermes session token."""
    if not isinstance(raw_token, str) or not raw_token:
        return None

    token_hash = _hash_token(raw_token)
    session_record = session.scalar(
        select(SessionModel).where(
            SessionModel.token_hash == token_hash,
        )
    )
    if session_record is None:
        return None

    if not hmac.compare_digest(session_record.token_hash, token_hash):
        return None

    if session_record.revoked_at is not None:
        return None

    if session_record.expires_at <= datetime.now(timezone.utc):
        return None

    user = session_record.user
    if user is None or user.status != UserStatus.ACTIVE:
        return None

    return user


def revoke_session(session: Session, raw_token: str) -> bool:
    """Revoke an existing session when the token matches an active record."""
    if not isinstance(raw_token, str) or not raw_token:
        return False

    token_hash = _hash_token(raw_token)
    session_record = session.scalar(
        select(SessionModel).where(
            SessionModel.token_hash == token_hash,
        )
    )
    if session_record is None:
        return False

    if not hmac.compare_digest(session_record.token_hash, token_hash):
        return False

    if session_record.revoked_at is not None:
        return False

    session_record.revoked_at = datetime.now(timezone.utc)
    return True
