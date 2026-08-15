"""Google OAuth authorization flow for Hermes v2."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.google import GoogleAuthenticationError, verify_google_id_token
from hermes_v2.auth.service import resolve_google_user
from hermes_v2.database.connection import create_engine_from_environment

_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105
_DEFAULT_STATE_TTL_SECONDS = 600


class OAuthStateStore:
    """In-memory state storage with TTL for this single-process dev implementation.

    This is intentionally limited to the current local deployment. Before any
    multi-worker or shared-state production deployment, replace it with a
    persistent, shared state store that is safe across processes and instances.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        state = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._entries[state] = expires_at
        return state

    def consume(self, state: str | None) -> bool:
        if not state or not state.strip():
            return False

        with self._lock:
            expires_at = self._entries.pop(state, None)

        if expires_at is None:
            return False

        return time.monotonic() <= expires_at


state_store = OAuthStateStore()


def _require_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _configured_google_client_id() -> str:
    return _require_environment_value("GOOGLE_CLIENT_ID")


def _configured_google_client_secret() -> str:
    return _require_environment_value("GOOGLE_CLIENT_SECRET")


def _configured_google_redirect_uri() -> str:
    return _require_environment_value("GOOGLE_REDIRECT_URI")


def _build_google_authorization_url() -> str:
    params = {
        "client_id": _configured_google_client_id(),
        "redirect_uri": _configured_google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": state_store.create(),
    }
    return f"{_GOOGLE_AUTHORIZATION_URL}?{urlencode(params)}"


def _create_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(),
        autoflush=False,
        expire_on_commit=False,
    )


def _exchange_google_code(code: str) -> dict[str, Any]:
    payload = urlencode(
        {
            "code": code,
            "client_id": _configured_google_client_id(),
            "client_secret": _configured_google_client_secret(),
            "redirect_uri": _configured_google_redirect_uri(),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=400, detail="Google token exchange failed"
        ) from exc
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail="Google token exchange failed"
        ) from exc


async def google_login() -> RedirectResponse:
    """Redirect the user to Google's consent screen."""
    return RedirectResponse(url=_build_google_authorization_url(), status_code=307)


async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Handle the Google callback and return a temporary authenticated response."""
    if not state or not state.strip():
        raise HTTPException(status_code=400, detail="Missing state parameter.")
    if not state_store.consume(state):
        raise HTTPException(status_code=400, detail="Invalid or expired state.")

    google_error = request.query_params.get("error")
    if google_error:
        raise HTTPException(
            status_code=400,
            detail=f"Google OAuth error: {google_error}",
        )

    if not code or not code.strip():
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    token_response = _exchange_google_code(code)
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        raise HTTPException(status_code=400, detail="Google token exchange failed.")

    try:
        claims = verify_google_id_token(id_token)
    except GoogleAuthenticationError as exc:
        raise HTTPException(
            status_code=400,
            detail="Google authentication failed.",
        ) from exc

    session_factory = _create_session_factory()
    with session_factory() as session:
        user = resolve_google_user(session, claims)

    roles = [role.name for role in getattr(user, "roles", [])]
    safe_user: dict[str, Any] = {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name or user.email,
        "roles": roles,
    }
    return {"authenticated": True, "user": safe_user}


__all__ = ["OAuthStateStore", "google_callback", "google_login", "state_store"]
