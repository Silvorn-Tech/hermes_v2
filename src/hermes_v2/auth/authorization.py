"""Permission-based authorization enforcement for Hermes v2 endpoints.

`require_permission(name)` is the single mechanism a mutable endpoint uses
to enforce RBAC. It mirrors the session-resolution pattern already used by
`get_authenticated_user` (auth/oauth.py) rather than introducing a second
way to read the session cookie.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.auth.seed import PERMISSION_CATALOG
from hermes_v2.auth.session import (
    get_session_cookie_name,
    get_user_from_session,
    serialize_authenticated_user,
)
from hermes_v2.database.connection import create_engine_from_environment

PermissionDependency = Callable[[Request], Awaitable[dict[str, Any]]]


def _create_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(),
        autoflush=False,
        expire_on_commit=False,
    )


def _user_has_permission(user: User, permission_name: str) -> bool:
    """Check whether any of the user's roles grants `permission_name`."""
    return any(
        permission.name == permission_name
        for role in user.roles
        for permission in role.permissions
    )


def require_permission(permission_name: str) -> PermissionDependency:
    """Build a FastAPI dependency that enforces one Hermes permission.

    Usage: `Depends(require_permission("orders.cancel"))` on any endpoint
    that changes state. Resolves the caller from the `hermes_session` cookie
    exactly like `GET /auth/me` does, then requires that at least one of the
    user's roles grants the named permission. Returns the same safe user
    payload `GET /auth/me` returns, so the endpoint can use it without a
    second lookup.

    Order of checks matters: missing/invalid session is always 401
    (`WWW-Authenticate`-style "who are you") before permission is even
    considered; a real, authenticated identity lacking the permission is
    403 ("I know who you are; you may not do this").

    Fails at *registration* time (when the route module is imported), not
    per-request, if `permission_name` isn't a real entry in
    `hermes_v2.auth.seed.PERMISSION_CATALOG` — a typo'd or invented
    permission must never be able to silently grant or deny access; it
    should never reach a running server at all.
    """
    if permission_name not in PERMISSION_CATALOG:
        raise ValueError(
            f"Unknown permission {permission_name!r}. It must be added to "
            "hermes_v2.auth.seed.PERMISSION_CATALOG before it can be "
            "enforced by require_permission()."
        )

    async def dependency(request: Request) -> dict[str, Any]:
        raw_token = request.cookies.get(get_session_cookie_name())
        if not raw_token:
            raise HTTPException(status_code=401, detail="Authentication required.")

        session_factory = _create_session_factory()
        with session_factory() as database_session:
            user = get_user_from_session(database_session, raw_token)
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required.")

            if not _user_has_permission(user, permission_name):
                raise HTTPException(status_code=403, detail="Insufficient permissions.")

            return serialize_authenticated_user(user)

    dependency.__hermes_permission__ = permission_name  # type: ignore[attr-defined]
    return dependency


__all__ = ["require_permission"]
