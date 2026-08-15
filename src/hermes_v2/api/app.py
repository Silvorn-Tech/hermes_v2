from fastapi import FastAPI, Request

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/google/login")
async def login() -> object:
    return await google_login()


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
