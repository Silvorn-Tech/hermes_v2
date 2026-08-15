"""Google identity resolution for Hermes authentication."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.auth.models import Identity, User, UserStatus


class GoogleIdentityError(ValueError):
    """Base exception for expected Google identity resolution failures."""


class GoogleUserNotFoundError(GoogleIdentityError):
    """Raised when a validated Google user is not authorized in Hermes."""


class GoogleUserDisabledError(GoogleIdentityError):
    """Raised when a Hermes user is disabled and cannot authenticate."""


def _google_sub_from_claims(claims: Mapping[str, Any]) -> str:
    """Return a valid Google subject claim or raise an identity error."""
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise GoogleIdentityError("Google claims do not contain a valid 'sub'.")
    return sub.strip()


def _normalized_email_from_claims(claims: Mapping[str, Any]) -> str:
    """Return a normalized Hermes email from trusted Google claims."""
    email = claims.get("email")
    if not isinstance(email, str):
        raise GoogleIdentityError("Google claims do not contain a valid 'email'.")

    normalized_email = email.strip().lower()
    if not normalized_email:
        raise GoogleIdentityError("Google claims do not contain a valid 'email'.")
    return normalized_email


def _require_verified_email_for_linking(claims: Mapping[str, Any]) -> str:
    """Require a verified Google email before linking by email to a Hermes user."""
    email = _normalized_email_from_claims(claims)
    email_verified = claims.get("email_verified")
    if email_verified is not True:
        raise GoogleIdentityError(
            "Google claims must include email_verified=True to link a user by email."
        )
    return email


def resolve_google_user(session: Session, claims: Mapping[str, Any]) -> User:
    """Resolve a trusted Google identity to an internal Hermes user."""
    google_sub = _google_sub_from_claims(claims)

    with session.begin():
        identity = session.scalar(
            select(Identity).where(
                Identity.provider == "google",
                Identity.provider_subject == google_sub,
            )
        )
        if identity is not None:
            user = identity.user
            if user is None:
                raise GoogleIdentityError("Google identity is missing its linked user.")
            if user.status == UserStatus.DISABLED:
                raise GoogleUserDisabledError(
                    "This Hermes user is disabled and cannot authenticate."
                )

            user.last_login_at = datetime.now(timezone.utc)
            session.flush()
            return user

        email = _require_verified_email_for_linking(claims)
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            raise GoogleUserNotFoundError(
                "This Google user is not authorized to access Hermes."
            )

        if user.status == UserStatus.DISABLED:
            raise GoogleUserDisabledError(
                "This Hermes user is disabled and cannot authenticate."
            )

        identity = Identity(
            user_id=user.id,
            provider="google",
            provider_subject=google_sub,
        )
        session.add(identity)
        session.flush()

        user.last_login_at = datetime.now(timezone.utc)
        session.flush()
        return user


__all__ = [
    "GoogleIdentityError",
    "GoogleUserDisabledError",
    "GoogleUserNotFoundError",
    "resolve_google_user",
]
