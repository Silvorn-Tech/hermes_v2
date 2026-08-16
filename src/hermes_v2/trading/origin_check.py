"""Origin-header enforcement for mutating trading endpoints.

Closes the gap `SECURITY_AUDIT.md` §8/§22 pre-registered: `SameSite=Lax`
alone is fine for the one low-value mutating endpoint that existed before
this feature (`POST /auth/logout`), but is fragile once a
trading-adjacent mutating endpoint exists — a future `HERMES_COOKIE_SAMESITE=none`
change (e.g. for a mobile webview) would remove that protection entirely.

This adds an explicit, application-level control: every mutating trading
request must carry an `Origin` header matching `HERMES_ALLOWED_ORIGINS`
(the same allowlist `CORSMiddleware` already uses, `api/app.py`). Combined
with FastAPI requiring a JSON body on these routes — a classic HTML
`<form>` POST cannot set `Content-Type: application/json` without
JavaScript, and cross-origin JavaScript is already blocked by CORS — this
closes the gap without a new dependency or a CSRF token scheme.

Scoped to the new trading routes only; `/auth/logout` is unchanged.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request


def _configured_allowed_origins() -> list[str]:
    """Same source of truth as `api/app.py`'s CORS allowlist."""
    raw_value = os.environ.get("HERMES_ALLOWED_ORIGINS", "")
    return [entry.strip() for entry in raw_value.split(",") if entry.strip()]


async def require_trusted_origin(request: Request) -> None:
    """FastAPI dependency: 403 unless `Origin` is present and allowlisted.

    Runs independently of `require_permission()` — both must pass. An
    unset `HERMES_ALLOWED_ORIGINS` means no origin is ever trusted (fails
    closed, matching the CORS allowlist's own behavior when unset).
    """
    origin = request.headers.get("origin")
    if not origin or origin not in _configured_allowed_origins():
        raise HTTPException(status_code=403, detail="Untrusted request origin.")


__all__ = ["require_trusted_origin"]
