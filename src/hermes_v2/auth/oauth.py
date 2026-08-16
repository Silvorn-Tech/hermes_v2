"""Google OAuth authorization flow for Hermes v2."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.google import GoogleAuthenticationError, verify_google_id_token
from hermes_v2.auth.service import GoogleIdentityError, resolve_google_user
from hermes_v2.auth.session import (
    create_session,
    get_cookie_samesite,
    get_session_cookie_name,
    get_session_ttl,
    get_user_from_session,
    is_cookie_secure,
    revoke_session,
    serialize_authenticated_user,
)
from hermes_v2.database.connection import create_engine_from_environment

logger = logging.getLogger(__name__)

_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105
_DEFAULT_STATE_TTL_SECONDS = 600


class OAuthStateStore:
    """In-memory state storage with TTL for this single-process dev implementation.

    Each entry also carries the validated frontend `return_to` URL requested
    at `/auth/google/login`, so the callback knows where to send the user
    back without trusting anything Google echoes back untrusted.

    This is intentionally limited to the current local deployment. Before any
    multi-worker or shared-state production deployment, replace it with a
    persistent, shared state store that is safe across processes and instances.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def create(self, return_to: str) -> str:
        state = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._entries[state] = (expires_at, return_to)
        return state

    def consume(self, state: str | None) -> str | None:
        """Consume a state token once, returning its return_to URL if still valid."""
        if not state or not state.strip():
            return None

        with self._lock:
            entry = self._entries.pop(state, None)

        if entry is None:
            return None

        expires_at, return_to = entry
        if time.monotonic() > expires_at:
            return None

        return return_to


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


def _configured_allowed_return_uris() -> list[str]:
    """Return the exact-match allowlist of frontend URLs the callback may redirect to.

    This is the open-redirect guard for `return_to`: Google itself never
    supplies this value, but a malicious link could try to pass an arbitrary
    `return_to` to `/auth/google/login` to redirect a victim elsewhere after
    a real Hermes login. Only exact matches configured server-side are honored.
    """
    raw_value = os.environ.get("HERMES_ALLOWED_RETURN_URIS", "")
    return [entry.strip() for entry in raw_value.split(",") if entry.strip()]


def _default_return_uri() -> str:
    allowed = _configured_allowed_return_uris()
    if not allowed:
        raise RuntimeError("HERMES_ALLOWED_RETURN_URIS must be configured")

    configured_default = os.environ.get("HERMES_DEFAULT_RETURN_URI")
    if configured_default and configured_default in allowed:
        return configured_default
    return allowed[0]


def _resolve_return_uri(requested: str | None) -> str:
    if not requested:
        return _default_return_uri()

    if requested not in _configured_allowed_return_uris():
        raise HTTPException(status_code=400, detail="Unsupported return_to value.")
    return requested


def _redirect_with_status(return_to: str, status: str) -> RedirectResponse:
    separator = "&" if "?" in return_to else "?"
    return RedirectResponse(url=f"{return_to}{separator}auth={status}", status_code=302)


def _build_google_authorization_url(return_to: str) -> str:
    params = {
        "client_id": _configured_google_client_id(),
        "redirect_uri": _configured_google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": state_store.create(return_to),
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
        error_body = exc.read().decode("utf-8", errors="replace")
        error_code = _extract_google_error_code(error_body)
        logger.warning(
            "Google token exchange failed: http_status=%s error=%s",
            exc.code,
            error_code,
        )
        raise GoogleTokenExchangeError("Google token exchange failed") from exc
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Google token exchange failed: %s", type(exc).__name__)
        raise GoogleTokenExchangeError("Google token exchange failed") from exc


def _extract_google_error_code(error_body: str) -> str:
    """Best-effort extraction of Google's `error` field for log correlation.

    Deliberately drops everything else in the response: request parameters
    are never echoed back into this field, but the rest of the body is not
    worth the risk of logging verbatim.
    """
    try:
        parsed = json.loads(error_body)
    except (ValueError, json.JSONDecodeError):
        return "unknown"
    error_code = parsed.get("error") if isinstance(parsed, dict) else None
    return error_code if isinstance(error_code, str) else "unknown"


class GoogleTokenExchangeError(RuntimeError):
    """Raised when exchanging an authorization code with Google fails."""


async def google_login(return_to: str | None = None) -> RedirectResponse:
    """Redirect the user to Google's consent screen.

    `return_to` must exactly match an entry in HERMES_ALLOWED_RETURN_URIS —
    it is where the browser lands back in the Hermes frontend once the
    callback below finishes, success or not.
    """
    resolved_return_to = _resolve_return_uri(return_to)
    return RedirectResponse(
        url=_build_google_authorization_url(resolved_return_to), status_code=307
    )


async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> JSONResponse | RedirectResponse:
    """Handle the Google callback: set the Hermes session cookie, then hand
    control back to the frontend via a redirect.

    Nothing sensitive (tokens, user data) travels in that redirect — it only
    carries a coarse `auth` status flag. The frontend calls GET /auth/me
    afterwards to fetch the authenticated user through the cookie that was
    just set on this same response.

    A missing/invalid `state` cannot be redirected safely (there is no
    trusted destination yet) and is rejected outright. Every failure mode
    after that point is a normal, expected outcome of a real login attempt
    (the user declined consent, the identity isn't authorized, Google had a
    hiccup) and sends the user back to the app instead of showing a raw
    JSON/error page.
    """
    if not state or not state.strip():
        raise HTTPException(status_code=400, detail="Missing state parameter.")

    return_to = state_store.consume(state)
    if return_to is None:
        logger.warning("OAuth callback rejected: invalid or expired state token")
        raise HTTPException(status_code=400, detail="Invalid or expired state.")

    if request.query_params.get("error"):
        return _redirect_with_status(return_to, "cancelled")

    if not code or not code.strip():
        return _redirect_with_status(return_to, "error")

    try:
        token_response = _exchange_google_code(code)
    except GoogleTokenExchangeError:
        return _redirect_with_status(return_to, "error")

    id_token = token_response.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        return _redirect_with_status(return_to, "error")

    try:
        claims = verify_google_id_token(id_token)
    except GoogleAuthenticationError:
        return _redirect_with_status(return_to, "error")

    session_factory = _create_session_factory()
    with session_factory() as database_session:
        try:
            user = resolve_google_user(database_session, claims)
        except GoogleIdentityError as exc:
            database_session.rollback()
            logger.info(
                "Google identity denied: email=%s reason=%s",
                claims.get("email"),
                type(exc).__name__,
            )
            return _redirect_with_status(return_to, "denied")

        _, raw_token = create_session(database_session, user, get_session_ttl())
        database_session.commit()
        logger.info("Hermes session created: user_id=%s", user.id)

    response = _redirect_with_status(return_to, "success")
    response.set_cookie(
        key=get_session_cookie_name(),
        value=raw_token,
        httponly=True,
        samesite=get_cookie_samesite(),
        path="/",
        secure=is_cookie_secure(),
    )
    return response


async def get_authenticated_user(request: Request) -> JSONResponse:
    """Return the current authenticated user from the Hermes session cookie."""
    raw_token = request.cookies.get(get_session_cookie_name())
    if not raw_token:
        raise HTTPException(status_code=401, detail="Authentication required.")

    session_factory = _create_session_factory()
    with session_factory() as database_session:
        user = get_user_from_session(database_session, raw_token)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")

        safe_user = serialize_authenticated_user(user)

    return JSONResponse(content={"authenticated": True, "user": safe_user})


async def logout_user(request: Request) -> JSONResponse:
    """Revoke the current Hermes session and clear the cookie."""
    raw_token = request.cookies.get(get_session_cookie_name())
    response = JSONResponse({"status": "ok"})

    if raw_token:
        session_factory = _create_session_factory()
        with session_factory() as database_session:
            revoke_session(database_session, raw_token)
            database_session.commit()

    response.delete_cookie(
        key=get_session_cookie_name(),
        path="/",
        secure=is_cookie_secure(),
        samesite=get_cookie_samesite(),
    )
    return response


__all__ = [
    "GoogleTokenExchangeError",
    "OAuthStateStore",
    "get_authenticated_user",
    "google_callback",
    "google_login",
    "logout_user",
    "state_store",
]
