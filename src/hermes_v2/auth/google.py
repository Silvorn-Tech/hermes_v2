"""Google OAuth/OIDC token validation."""

from __future__ import annotations

import os
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.oauth2 import id_token
from google.auth.transport import requests


class GoogleAuthenticationError(ValueError):
    """Raised when a Google ID token cannot be validated."""


def _google_client_id() -> str:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")

    if not client_id:
        raise GoogleAuthenticationError("GOOGLE_CLIENT_ID must be configured")

    return client_id


def verify_google_id_token(token: str) -> dict[str, Any]:
    """Validate a Google ID token and return its trusted claims."""

    if not token:
        raise GoogleAuthenticationError("Google ID token is required")

    try:
        claims = id_token.verify_oauth2_token(
            token,
            requests.Request(),
            _google_client_id(),
        )
    except (ValueError, GoogleAuthError) as exc:
        raise GoogleAuthenticationError("Invalid Google ID token") from exc

    if claims.get("iss") not in {
        "accounts.google.com",
        "https://accounts.google.com",
    }:
        raise GoogleAuthenticationError("Invalid Google token issuer")

    if not claims.get("sub"):
        raise GoogleAuthenticationError("Google ID token does not contain a subject")

    return claims
