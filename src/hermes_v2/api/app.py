import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hermes_v2.auth.oauth import (
    get_authenticated_user,
    google_callback,
    google_login,
    logout_user,
)
from hermes_v2.auth.session import is_cookie_secure

app = FastAPI(
    title="Hermes v2",
    version="0.1.0",
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security response headers for every Hermes API response.

    None of these depend on request content, so they are safe to apply
    unconditionally to an API that returns only JSON and redirects (no
    HTML), unlike a CSP which would need route-specific tuning.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if is_cookie_secure():
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


def _configured_allowed_origins() -> list[str]:
    """Frontend origins allowed to make credentialed cross-origin requests.

    Never falls back to a wildcard: the session cookie requires
    Access-Control-Allow-Credentials, and browsers reject that combined with
    Access-Control-Allow-Origin: *. Each deployment (local, Romeo,
    production) configures its own exact origin(s) via HERMES_ALLOWED_ORIGINS.
    """
    raw_value = os.environ.get("HERMES_ALLOWED_ORIGINS", "")
    return [entry.strip() for entry in raw_value.split(",") if entry.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/google/login")
async def login(return_to: str | None = None) -> object:
    return await google_login(return_to)


@app.get("/auth/google/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None
) -> object:
    return await google_callback(request, code=code, state=state)


@app.get("/auth/me")
async def me(request: Request) -> object:
    return await get_authenticated_user(request)


@app.post("/auth/logout")
async def logout(request: Request) -> object:
    """Revoke the caller's own session.

    Deliberately not gated by `require_permission()`: this is a
    self-service action on the caller's own session, not a role-scoped
    operation — every authenticated user may end their own session
    regardless of role. Authentication (401 if no valid session) is the
    only check that applies. See
    tests/test_authorization.py::test_every_mutating_route_is_permission_gated_or_exempt
    for the regression guard that keeps this the *only* undocumented
    exemption.
    """
    return await logout_user(request)
