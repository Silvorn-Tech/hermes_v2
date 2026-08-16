"""Origin-header enforcement for mutating trading endpoints.

Closes the gap `SECURITY_AUDIT.md` §8/§22 pre-registered: `SameSite=Lax`
alone is fine for the one low-value mutating endpoint that existed before
this feature (`POST /auth/logout`), but is fragile once a
trading-adjacent mutating endpoint exists — a future `HERMES_COOKIE_SAMESITE=none`
change (e.g. for a mobile webview) would remove that protection entirely.

This adds an explicit, application-level control: every mutating trading
request must carry an `Origin` header matching `HERMES_ALLOWED_ORIGINS`
(the same allowlist `CORSMiddleware` already uses, `api/app.py`, both
reading it via `hermes_v2.config.configured_allowed_origins` so the two
checks can never silently diverge). Combined with FastAPI requiring a JSON
body on these routes — a classic HTML `<form>` POST cannot set
`Content-Type: application/json` without JavaScript, and cross-origin
JavaScript is already blocked by CORS — this closes the gap without a new
dependency or a CSRF token scheme.

By construction this also means every mutating trading request must be a
browser `fetch`/`XHR` call from an allowlisted origin — a plain `curl` or
server-to-server call with no `Origin` header is rejected identically to a
forged one. That is the intended behavior, not a gap: there is no
legitimate non-browser caller of these endpoints today, and CORS already
makes cross-origin JavaScript unable to set `Origin` to anything but the
caller's real origin (`Origin` is a forbidden header name — JavaScript
cannot spoof it).

Scoped to the new trading routes only; `/auth/logout` is unchanged.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from hermes_v2.config import configured_allowed_origins


async def require_trusted_origin(request: Request) -> None:
    """FastAPI dependency: 403 unless `Origin` is present and allowlisted.

    Runs independently of `require_permission()` — both must pass. An
    unset `HERMES_ALLOWED_ORIGINS` means no origin is ever trusted (fails
    closed, matching the CORS allowlist's own behavior when unset) — so
    there is no "dev convenience" default that weakens this check; local
    development must set `HERMES_ALLOWED_ORIGINS` explicitly (already
    required for CORS to work at all, see `.env.dev.example`).
    """
    origin = request.headers.get("origin")
    if not origin or origin not in configured_allowed_origins():
        raise HTTPException(status_code=403, detail="Untrusted request origin.")


__all__ = ["require_trusted_origin"]
