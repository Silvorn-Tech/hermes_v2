"""Tests for Hermes v2's in-process rate limiting.

Two layers, mirroring test_authorization.py's structure:

1. `SlidingWindowRateLimiter` and `rate_limit()` in isolation, against a
   throwaway test app — never the real database.
2. Integration tests against the real `hermes_v2.api.app.app` for the four
   protected auth endpoints, confirming the exact policy each one gets and
   that normal OAuth/session/logout usage is unaffected under the limit.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)
os.environ.setdefault("HERMES_ALLOWED_RETURN_URIS", "http://localhost:8081/login")
os.environ.setdefault("HERMES_ALLOWED_ORIGINS", "http://localhost:8081")

from hermes_v2.api.app import app as real_app
import hermes_v2.auth.oauth as oauth_module
from hermes_v2.auth.rate_limiting import (
    CALLBACK_RATE_LIMITER,
    LOGIN_RATE_LIMITER,
    LOGOUT_RATE_LIMITER,
    ME_RATE_LIMITER,
    SlidingWindowRateLimiter,
    _client_identifier,
    rate_limit,
)

DEFAULT_RETURN_TO = "http://localhost:8081/login"


class _FakeDatabaseSession:
    """Stands in for a real SQLAlchemy session so these tests never touch
    a database — mirrors test_auth_oauth.py's `google_environment` fixture.
    """

    def __enter__(self) -> "_FakeDatabaseSession":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return False

    def add(self, *_args: object, **_kwargs: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        oauth_module,
        "_create_session_factory",
        lambda: _FakeDatabaseSession,
    )


# --- SlidingWindowRateLimiter, in isolation --------------------------------


def test_requests_within_the_limit_are_allowed() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)

    for _ in range(3):
        allowed, retry_after = limiter.check("key")
        assert allowed is True
        assert retry_after == 0.0


def test_requests_beyond_the_limit_are_rejected() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check("key")

    allowed, retry_after = limiter.check("key")

    assert allowed is False
    assert retry_after > 0.0


def test_retry_after_is_bounded_by_the_window() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=30)
    limiter.check("key")

    _allowed, retry_after = limiter.check("key")

    assert 0.0 < retry_after <= 30.0


def test_rejected_requests_are_not_counted_against_the_window() -> None:
    """A blocked client retrying repeatedly must not push its own unblock
    time further out — only allowed requests are recorded."""
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    limiter.check("key")

    first_reject_retry_after = limiter.check("key")[1]
    for _ in range(5):
        limiter.check("key")
    later_reject_retry_after = limiter.check("key")[1]

    assert later_reject_retry_after <= first_reject_retry_after


def test_different_keys_have_independent_windows() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)

    assert limiter.check("key-a")[0] is True
    assert limiter.check("key-a")[0] is False
    assert limiter.check("key-b")[0] is True


def test_window_expiry_allows_requests_again(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=10)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "hermes_v2.auth.rate_limiting.time.monotonic", lambda: clock["now"]
    )

    assert limiter.check("key")[0] is True
    assert limiter.check("key")[0] is False

    clock["now"] += 10.01

    assert limiter.check("key")[0] is True


def test_reset_clears_all_state() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    limiter.check("key")
    assert limiter.check("key")[0] is False

    limiter.reset()

    assert limiter.check("key")[0] is True


def test_max_requests_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=0, window_seconds=60)


def test_window_seconds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=1, window_seconds=0)


# --- Client identifier / proxy trust ---------------------------------------


def test_client_identifier_uses_direct_peer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_TRUST_PROXY_HEADERS", raising=False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.5"),
        headers={"x-forwarded-for": "198.51.100.9"},
    )

    assert _client_identifier(request) == "203.0.113.5"


def test_client_identifier_ignores_forwarded_header_without_explicit_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cannot forge its way around its own rate limit by sending
    X-Forwarded-For unless the deployment explicitly opted in."""
    monkeypatch.setenv("HERMES_TRUST_PROXY_HEADERS", "false")
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.5"),
        headers={"x-forwarded-for": "198.51.100.9"},
    )

    assert _client_identifier(request) == "203.0.113.5"


def test_client_identifier_trusts_forwarded_header_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_TRUST_PROXY_HEADERS", "true")
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "198.51.100.9, 127.0.0.1"},
    )

    assert _client_identifier(request) == "198.51.100.9"


def test_client_identifier_falls_back_when_client_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_TRUST_PROXY_HEADERS", raising=False)
    request = SimpleNamespace(client=None, headers={})

    assert _client_identifier(request) == "unknown"


# --- rate_limit() dependency, against a throwaway app ----------------------


def _build_test_app(limiter: SlidingWindowRateLimiter) -> FastAPI:
    test_app = FastAPI()

    @test_app.get("/test/limited")
    async def limited_endpoint(
        _rate_limit: None = Depends(rate_limit(limiter, "test.scope")),
    ) -> dict:
        return {"ok": True}

    return test_app


def test_dependency_allows_requests_within_the_limit() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
    client = TestClient(_build_test_app(limiter))

    assert client.get("/test/limited").status_code == 200
    assert client.get("/test/limited").status_code == 200


def test_dependency_returns_429_beyond_the_limit() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
    client = TestClient(_build_test_app(limiter))
    client.get("/test/limited")
    client.get("/test/limited")

    response = client.get("/test/limited")

    assert response.status_code == 429


def test_dependency_429_includes_retry_after_header() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=45)
    client = TestClient(_build_test_app(limiter))
    client.get("/test/limited")

    response = client.get("/test/limited")

    retry_after = response.headers.get("retry-after")
    assert retry_after is not None
    assert 1 <= int(retry_after) <= 45


def test_dependency_429_message_reveals_nothing_sensitive() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    client = TestClient(_build_test_app(limiter))
    client.get("/test/limited")

    response = client.get("/test/limited")
    body = response.text.lower()

    assert response.json()["detail"] == "Too many requests. Please try again later."
    for forbidden in ("test.scope", "testclient", "1 per", "max_requests", "ip"):
        assert forbidden not in body


# --- Integration: the real app's four protected endpoints ------------------


def _extract_state_from_redirect(location: str) -> str:
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    return query["state"][0]


def test_login_within_limit_still_redirects_to_google() -> None:
    client = TestClient(real_app)

    for _ in range(LOGIN_RATE_LIMITER.max_requests):
        response = client.get("/auth/google/login", follow_redirects=False)
        assert response.status_code == 307


def test_login_beyond_limit_returns_429_with_retry_after() -> None:
    client = TestClient(real_app)
    for _ in range(LOGIN_RATE_LIMITER.max_requests):
        client.get("/auth/google/login", follow_redirects=False)

    response = client.get("/auth/google/login", follow_redirects=False)

    assert response.status_code == 429
    assert response.headers.get("retry-after") is not None


def test_callback_and_login_have_independent_limits() -> None:
    """Exhausting the login limiter must not affect the callback limiter,
    and vice versa — each endpoint's policy is fully independent."""
    client = TestClient(real_app)
    for _ in range(LOGIN_RATE_LIMITER.max_requests):
        client.get("/auth/google/login", follow_redirects=False)
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 429

    # The callback limiter is untouched by exhausting the login limiter.
    response = client.get(
        "/auth/google/callback?state=irrelevant", follow_redirects=False
    )
    assert response.status_code != 429


def test_oauth_login_then_callback_succeeds_end_to_end_under_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full, normal OAuth round trip must work unaffected by rate
    limiting when nowhere near either endpoint's limit."""
    client = TestClient(real_app)
    login_response = client.get("/auth/google/login", follow_redirects=False)
    state = _extract_state_from_redirect(login_response.headers["location"])

    monkeypatch.setattr(
        oauth_module,
        "_exchange_google_code",
        lambda code: {"id_token": "google-id-token"},
    )
    monkeypatch.setattr(
        oauth_module,
        "verify_google_id_token",
        lambda token: {
            "sub": "google-sub-rl",
            "email": "rate-limit-ok@example.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(
        oauth_module,
        "resolve_google_user",
        lambda session, claims: SimpleNamespace(
            id="user-rl",
            email="rate-limit-ok@example.com",
            display_name="Rate Limit OK",
            roles=[],
        ),
    )

    response = client.get(
        f"/auth/google/callback?code=example-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{DEFAULT_RETURN_TO}?auth=success"


def test_auth_me_within_limit_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(
        id="user-me", email="me@example.com", display_name="Me", roles=[]
    )
    monkeypatch.setattr(
        oauth_module, "get_user_from_session", lambda session, token: user
    )
    client = TestClient(real_app)

    for _ in range(ME_RATE_LIMITER.max_requests):
        response = client.get("/auth/me", cookies={"hermes_session": "valid"})
        assert response.status_code == 200


def test_auth_me_beyond_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(
        id="user-me", email="me@example.com", display_name="Me", roles=[]
    )
    monkeypatch.setattr(
        oauth_module, "get_user_from_session", lambda session, token: user
    )
    client = TestClient(real_app)
    for _ in range(ME_RATE_LIMITER.max_requests):
        client.get("/auth/me", cookies={"hermes_session": "valid"})

    response = client.get("/auth/me", cookies={"hermes_session": "valid"})

    assert response.status_code == 429
    assert response.headers.get("retry-after") is not None


def test_me_limit_is_more_permissive_than_login_and_callback() -> None:
    assert ME_RATE_LIMITER.max_requests > LOGIN_RATE_LIMITER.max_requests
    assert ME_RATE_LIMITER.max_requests > CALLBACK_RATE_LIMITER.max_requests


def test_logout_within_limit_still_revokes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"revoked": False}

    def fake_revoke(session: object, token: str) -> bool:
        called["revoked"] = True
        return True

    monkeypatch.setattr(oauth_module, "revoke_session", fake_revoke)
    monkeypatch.setenv("HERMES_COOKIE_SECURE", "false")
    client = TestClient(real_app)

    response = client.post(
        "/auth/logout", cookies={"hermes_session": "token-to-revoke"}
    )

    assert response.status_code == 200
    assert called["revoked"] is True


def test_logout_beyond_limit_returns_429() -> None:
    client = TestClient(real_app)
    for _ in range(LOGOUT_RATE_LIMITER.max_requests):
        client.post("/auth/logout")

    response = client.post("/auth/logout")

    assert response.status_code == 429
    assert response.headers.get("retry-after") is not None


def test_health_endpoint_is_not_rate_limited() -> None:
    """/health is a liveness probe (Docker HEALTHCHECK polls it every 30s)
    with no sensitive data and nothing to abuse — it is intentionally
    excluded from the four protected auth endpoints."""
    client = TestClient(real_app)

    for _ in range(200):
        response = client.get("/health")
        assert response.status_code == 200
