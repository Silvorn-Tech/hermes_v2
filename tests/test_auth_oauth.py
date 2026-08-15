"""Tests for Hermes Google OAuth authorization flow."""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/auth/google/callback",
)

from hermes_v2.api.app import app
import hermes_v2.auth.oauth as oauth_module


@pytest.fixture(autouse=True)
def google_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/auth/google/callback",
    )

    class FakeSession:
        def __enter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
            return False

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

    response = client.get(f"/auth/google/callback?state={state}")

    assert response.status_code == 400
    assert "code" in response.json()["detail"].lower()


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

    response = client.get(f"/auth/google/callback?code=example-code&state={state}")

    assert response.status_code == 400
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

    response = client.get(f"/auth/google/callback?code=example-code&state={state}")

    assert response.status_code == 200
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

    response = client.get(f"/auth/google/callback?code=example-code&state={state}")

    assert response.status_code == 200
    assert called["value"] is True


def test_successful_callback_returns_safe_user_response(monkeypatch) -> None:
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

    response = client.get(f"/auth/google/callback?code=example-code&state={state}")

    expected = {
        "authenticated": True,
        "user": {
            "id": "user-abc",
            "email": "safe@example.com",
            "display_name": "Safe User",
            "roles": ["ADMIN", "USER"],
        },
    }

    assert response.status_code == 200
    assert response.json() == expected


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

    response = client.get(f"/auth/google/callback?code=example-code&state={state}")
    body = response.json()

    assert response.status_code == 200
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "id_token" not in body
    assert "client_secret" not in body
    assert "authorization_code" not in body
    assert response.text.lower().count("token") == 0
