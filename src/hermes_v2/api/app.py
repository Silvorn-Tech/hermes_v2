from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hermes_v2.api.trading_routes import router as trading_router
from hermes_v2.auth.oauth import (
    get_authenticated_user,
    google_callback,
    google_login,
    logout_user,
)
from hermes_v2.auth.rate_limiting import (
    CALLBACK_RATE_LIMITER,
    LOGIN_RATE_LIMITER,
    LOGOUT_RATE_LIMITER,
    ME_RATE_LIMITER,
    rate_limit,
)
from hermes_v2.auth.session import is_cookie_secure
from hermes_v2.config import configured_allowed_origins as _configured_allowed_origins

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


# allow_origins never falls back to a wildcard: the session cookie requires
# Access-Control-Allow-Credentials, and browsers reject that combined with
# Access-Control-Allow-Origin: *. Each deployment (local, Romeo, production)
# configures its own exact origin(s) via HERMES_ALLOWED_ORIGINS — the same
# allowlist hermes_v2.trading.origin_check's CSRF guard reads, via the
# shared hermes_v2.config module so the two can never silently diverge.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)

# Not app.include_router(trading_router): this FastAPI version wraps
# included routers in a lazy _IncludedRouter that never appears in
# app.routes, which would make trading_router's routes invisible to
# tests/test_authorization.py's real_app.routes walk — the regression
# guard that catches an unprotected mutating endpoint. Extending
# app.router.routes directly with the already-built APIRoute objects
# keeps every route eagerly visible there, exactly like the routes
# defined directly on `app` below.
app.router.routes.extend(trading_router.routes)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/google/login")
async def login(
    return_to: str | None = None,
    _rate_limit: None = Depends(rate_limit(LOGIN_RATE_LIMITER, "auth.google.login")),
) -> object:
    return await google_login(return_to)


@app.get("/auth/google/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    _rate_limit: None = Depends(
        rate_limit(CALLBACK_RATE_LIMITER, "auth.google.callback")
    ),
) -> object:
    return await google_callback(request, code=code, state=state)


@app.get("/auth/me")
async def me(
    request: Request,
    _rate_limit: None = Depends(rate_limit(ME_RATE_LIMITER, "auth.me")),
) -> object:
    return await get_authenticated_user(request)


@app.post("/auth/logout")
async def logout(
    request: Request,
    _rate_limit: None = Depends(rate_limit(LOGOUT_RATE_LIMITER, "auth.logout")),
) -> object:
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
