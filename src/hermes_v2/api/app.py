import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from hermes_v2.auth.oauth import (
    get_authenticated_user,
    google_callback,
    google_login,
    logout_user,
)

app = FastAPI(
    title="Hermes v2",
    version="0.1.0",
)


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
    return await logout_user(request)
