"""Tests for Hermes Google OAuth authorization flow."""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/auth/google/callback",
)
os.environ.setdefault(
    "HERMES_ALLOWED_RETURN_URIS",
    "http://localhost:8081/login,hermes:///login",
)
os.environ.setdefault("HERMES_ALLOWED_ORIGINS", "http://localhost:8081")

from hermes_v2.api.app import app
import hermes_v2.auth.oauth as oauth_module
from hermes_v2.auth.models import Role, User
from hermes_v2.auth.service import GoogleUserDisabledError, GoogleUserNotFoundError
from hermes_v2.auth.session import serialize_authenticated_user
from hermes_v2.database.connection import create_engine_from_environment

DEFAULT_RETURN_TO = "http://localhost:8081/login"
ALT_RETURN_TO = "hermes:///login"


@pytest.fixture(autouse=True)
def google_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/auth/google/callback",
    )
    monkeypatch.setenv(
        "HERMES_ALLOWED_RETURN_URIS", f"{DEFAULT_RETURN_TO},{ALT_RETURN_TO}"
    )

    class FakeSession:
        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
            return False

        def add(self, *_args, **_kwargs) -> None:
            return None

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._create_session_factory",
        lambda: FakeSession,
    )


def test_oauth_module_imports_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    oauth_module = importlib.import_module("hermes_v2.auth.oauth")
    reloaded = importlib.reload(oauth_module)

    assert reloaded.state_store is not None


def _extract_state_from_redirect(location: str) -> str:
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    return query["state"][0]


def test_google_login_redirects_to_google() -> None:
    client = TestClient(app)

    response = client.get("/auth/google/login", follow_redirects=False)

    assert response.status_code == 307
    assert (
        "https://accounts.google.com/o/oauth2/v2/auth" in response.headers["location"]
    )
    assert "client_id=test-client-id" in response.headers["location"]
    assert "response_type=code" in response.headers["location"]
    assert "scope=openid+email+profile" in response.headers["location"]


def test_google_login_generates_state() -> None:
    client = TestClient(app)

    response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(response.headers["location"])

    assert state
    assert state in oauth_module.state_store._entries


def test_state_is_not_predictable() -> None:
    client = TestClient(app)

    first = client.get("/auth/google/login", follow_redirects=False)
    second = client.get("/auth/google/login", follow_redirects=False)

    first_state = _extract_state_from_redirect(first.headers["location"])
    second_state = _extract_state_from_redirect(second.headers["location"])

    assert first_state != second_state
    assert first_state in oauth_module.state_store._entries
    assert second_state in oauth_module.state_store._entries


def test_callback_without_state_is_rejected() -> None:
    client = TestClient(app)

    response = client.get("/auth/google/callback?code=abc123")

    assert response.status_code == 400
    assert "state" in response.json()["detail"].lower()


def test_callback_with_invalid_state_is_rejected() -> None:
    client = TestClient(app)

    response = client.get("/auth/google/callback?state=bad-state&code=abc123")

    assert response.status_code == 400
    assert "invalid" in response.json()["detail"].lower()


def test_callback_with_missing_code_is_rejected() -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    response = client.get(
        f"/auth/google/callback?state={state}", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=error"


def test_google_token_exchange_is_mocked(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    called = {"value": False}

    def fake_exchange(code: str) -> dict[str, str]:
        called["value"] = True
        assert code == "example-code"
        return {"id_token": "google-id-token"}

    monkeypatch.setattr("hermes_v2.auth.oauth._exchange_google_code", fake_exchange)

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=error"
    assert called["value"] is True


def test_verify_google_id_token_is_mocked(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    captured = {"token": None}

    def fake_verify(token: str) -> dict[str, str]:
        captured["token"] = token
        return {
            "sub": "google-sub-123",
            "email": "verified@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        }

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr("hermes_v2.auth.oauth.verify_google_id_token", fake_verify)
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-123",
            email="verified@example.com",
            display_name="Verified User",
            roles=[SimpleNamespace(name="ADMIN")],
        ),
    )

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=success"
    assert captured["token"] == "google-id-token"


def test_resolve_google_user_is_mocked(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-123",
            "email": "resolved@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )

    called = {"value": False}

    def fake_resolve(session, claims):
        called["value"] = True
        assert claims["sub"] == "google-sub-123"
        return SimpleNamespace(
            id="resolved-user-1",
            email="resolved@example.com",
            display_name="Resolved User",
            roles=[SimpleNamespace(name="USER")],
        )

    monkeypatch.setattr("hermes_v2.auth.oauth.resolve_google_user", fake_resolve)

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=success"
    assert called["value"] is True


def test_successful_callback_redirects_to_return_to_with_success_status(
    monkeypatch,
) -> None:
    """The callback hands control back to the frontend via redirect, not JSON.

    User details never travel in this response (or its URL) — the frontend
    fetches them afterwards from GET /auth/me using the cookie set here.
    """
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-123",
            "email": "safe@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-abc",
            email="safe@example.com",
            display_name="Safe User",
            roles=[SimpleNamespace(name="ADMIN"), SimpleNamespace(name="USER")],
        ),
    )

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=success"
    assert "safe@example.com" not in response.text
    assert "user-abc" not in response.text


def test_callback_redirects_to_the_return_to_requested_at_login(monkeypatch) -> None:
    """The callback must honor the allowlisted return_to the login step recorded."""
    client = TestClient(app)
    login_response = client.get(
        f"/auth/google/login?return_to={ALT_RETURN_TO}", follow_redirects=False
    )
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-alt",
            "email": "alt@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-alt",
            email="alt@example.com",
            display_name="Alt User",
            roles=[],
        ),
    )

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{ALT_RETURN_TO}?auth=success"


def test_login_rejects_unlisted_return_to() -> None:
    client = TestClient(app)

    response = client.get(
        "/auth/google/login?return_to=https://evil.example.com/steal",
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_google_error_redirects_with_cancelled_status() -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    response = client.get(
        f"/auth/google/callback?state={state}&error=access_denied",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=cancelled"


def test_unauthorized_user_redirects_with_denied_status_not_a_server_error(
    monkeypatch,
) -> None:
    """A validly-authenticated but unauthorized identity must not crash."""
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-unknown",
            "email": "not-invited@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )

    def fake_resolve(session, claims):
        raise GoogleUserNotFoundError("not authorized")

    monkeypatch.setattr("hermes_v2.auth.oauth.resolve_google_user", fake_resolve)

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=denied"
    assert "not-invited@example.com" not in response.text
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_disabled_user_redirects_with_denied_status(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-disabled",
            "email": "disabled@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )

    def fake_resolve(session, claims):
        raise GoogleUserDisabledError("disabled")

    monkeypatch.setattr("hermes_v2.auth.oauth.resolve_google_user", fake_resolve)

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=denied"


def test_serialize_user_with_lazy_roles_while_session_is_active() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE sessions, identities, user_roles, role_permissions, "
                "permissions, roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(bind=engine)
    with session_factory() as database_session:
        user = User(email="lazy-role-user@example.com")
        role = Role(name="ADMIN")
        user.roles.append(role)
        database_session.add(user)
        database_session.commit()

        database_session.expire(user)
        hydrated_user = database_session.scalar(
            select(User).where(User.email == "lazy-role-user@example.com")
        )
        assert hydrated_user is not None
        payload = serialize_authenticated_user(hydrated_user)

        assert payload["email"] == "lazy-role-user@example.com"
        assert payload["roles"] == ["ADMIN"]

    engine.dispose()


def test_callback_response_excludes_google_tokens_and_secrets(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {
            "access_token": "google-access-token",
            "refresh_token": "google-refresh-token",
            "id_token": "google-id-token",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-123",
            "email": "safe@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-xyz",
            email="safe@example.com",
            display_name="Safe User",
            roles=[SimpleNamespace(name="ADMIN")],
        ),
    )

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=success"
    assert "google-access-token" not in response.headers["location"]
    assert "google-refresh-token" not in response.headers["location"]
    assert "google-id-token" not in response.headers["location"]
    assert "test-client-secret" not in response.headers["location"]
    assert "token" not in response.text.lower()
    assert "example.com" not in response.text


def test_successful_callback_sets_hermes_session_cookie(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-123",
            "email": "cookie@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-cookie",
            email="cookie@example.com",
            display_name="Cookie User",
            roles=[SimpleNamespace(name="USER")],
        ),
    )
    monkeypatch.setenv("HERMES_COOKIE_SECURE", "false")

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )
    set_cookie = response.headers.get("set-cookie", "")

    assert response.status_code == 302
    assert "hermes_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()
    assert "token" not in response.text.lower()


def test_successful_callback_sets_secure_cookie_when_enabled(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-456",
            "email": "secure@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-secure",
            email="secure@example.com",
            display_name="Secure User",
            roles=[SimpleNamespace(name="USER")],
        ),
    )
    monkeypatch.setenv("HERMES_COOKIE_SECURE", "true")

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )
    set_cookie = response.headers.get("set-cookie", "")

    assert response.status_code == 302
    assert "secure" in set_cookie.lower()


def test_cookie_samesite_is_configurable(monkeypatch) -> None:
    client = TestClient(app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        "hermes_v2.auth.oauth._exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.verify_google_id_token",
        lambda token: {
            "sub": "google-sub-samesite",
            "email": "samesite@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-samesite",
            email="samesite@example.com",
            display_name="Samesite User",
            roles=[],
        ),
    )
    monkeypatch.setenv("HERMES_COOKIE_SAMESITE", "none")
    monkeypatch.setenv("HERMES_COOKIE_SECURE", "true")

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )
    set_cookie = response.headers.get("set-cookie", "")

    assert "samesite=none" in set_cookie.lower()


def test_auth_me_requires_cookie(monkeypatch) -> None:
    client = TestClient(app)

    response = client.get("/auth/me")

    assert response.status_code == 401


def test_auth_me_accepts_valid_session(monkeypatch) -> None:
    client = TestClient(app)

    user = SimpleNamespace(
        id="user-42",
        email="me@example.com",
        display_name="Current User",
        roles=[SimpleNamespace(name="SUPER_ADMIN")],
    )
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.get_user_from_session",
        lambda session, token: user,
    )

    response = client.get("/auth/me", cookies={"hermes_session": "valid-token"})

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["email"] == "me@example.com"
    assert response.json()["user"]["roles"] == ["SUPER_ADMIN"]


def test_auth_me_rejects_invalid_session(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.get_user_from_session",
        lambda session, token: None,
    )

    response = client.get("/auth/me", cookies={"hermes_session": "bad-token"})

    assert response.status_code == 401


def test_auth_me_rejects_revoked_session(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.get_user_from_session",
        lambda session, token: None,
    )

    response = client.get("/auth/me", cookies={"hermes_session": "revoked-token"})

    assert response.status_code == 401


def test_auth_me_rejects_disabled_user(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        "hermes_v2.auth.oauth.get_user_from_session",
        lambda session, token: None,
    )

    response = client.get("/auth/me", cookies={"hermes_session": "disabled-token"})

    assert response.status_code == 401


def test_logout_revokes_and_clears_cookie(monkeypatch) -> None:
    client = TestClient(app)
    called = {"revoked": False}

    def fake_revoke(session, token):
        called["revoked"] = True
        return True

    monkeypatch.setattr("hermes_v2.auth.oauth.revoke_session", fake_revoke)
    monkeypatch.setenv("HERMES_COOKIE_SECURE", "false")

    response = client.post(
        "/auth/logout",
        cookies={"hermes_session": "token-to-revoke"},
    )

    assert response.status_code == 200
    assert called["revoked"] is True
    assert "hermes_session=" in response.headers.get("set-cookie", "")
    assert "expires=" in response.headers.get("set-cookie", "").lower()


def test_logout_is_idempotent_without_cookie(monkeypatch) -> None:
    client = TestClient(app)

    response = client.post("/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_allows_configured_frontend_origin_with_credentials() -> None:
    """HERMES_ALLOWED_ORIGINS is set to http://localhost:8081 at module import
    for this test module (see top of file) — that's the only origin this
    assertion can exercise, since CORSMiddleware is configured once at app
    startup, not per-request.
    """
    client = TestClient(app)

    response = client.get("/health", headers={"Origin": "http://localhost:8081"})

    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:8081"
    )
    assert response.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_cors_preflight_allows_every_method_the_api_actually_uses(
    method: str,
) -> None:
    """A method missing from CORSMiddleware's `allow_methods` doesn't 403
    the real request -- it fails the browser's own `OPTIONS` preflight
    with a 400 *before* the real request is ever sent, so the route looks
    entirely unreachable cross-origin. Regression guard for exactly this:
    `PATCH /bots/{id}` and `DELETE /bots/{id}` both silently broke this
    way in production (observed as a 400 on the preflight in ROMEO's
    logs) because `allow_methods` only listed `GET`/`POST`. `PUT` broke
    the same way once `/settings/binance-credentials` started using it.
    """
    client = TestClient(app)

    response = client.options(
        "/bots/00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": method,
        },
    )

    assert response.status_code == 200
    assert method in response.headers.get("access-control-allow-methods", "")


def test_cors_rejects_unconfigured_origin() -> None:
    client = TestClient(app)

    response = client.get("/health", headers={"Origin": "https://evil.example.com"})

    assert (
        response.headers.get("access-control-allow-origin")
        != "https://evil.example.com"
    )


def test_cors_never_falls_back_to_wildcard_origin() -> None:
    """Access-Control-Allow-Origin: * is incompatible with credentialed
    cookie-based requests — browsers reject it outright. Guard against a
    misconfiguration regressing this.
    """
    from hermes_v2.api.app import _configured_allowed_origins

    assert "*" not in _configured_allowed_origins()
