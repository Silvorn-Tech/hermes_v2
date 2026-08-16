"""Tests for Google ID token cryptographic validation."""

from __future__ import annotations

import pytest
from google.auth.exceptions import GoogleAuthError

from hermes_v2.auth.google import GoogleAuthenticationError, verify_google_id_token


@pytest.fixture(autouse=True)
def google_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")


def test_verify_rejects_empty_token() -> None:
    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("")


def test_verify_requires_google_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("some-token")


def test_verify_wraps_expired_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        raise ValueError("Token expired")

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("expired-token")


def test_verify_wraps_wrong_audience_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        raise ValueError(f"Token has wrong audience {audience!r}")

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("wrong-audience-token")


def test_verify_wraps_malformed_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        raise ValueError("Wrong number of segments in token")

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("not-a-jwt")


def test_verify_wraps_google_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        raise GoogleAuthError("transport failure")

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("token")


def test_verify_rejects_unexpected_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        return {"iss": "https://not-google.example.com", "sub": "12345"}

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("token-with-wrong-issuer")


def test_verify_rejects_missing_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(token, request, audience):
        return {"iss": "https://accounts.google.com"}

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )

    with pytest.raises(GoogleAuthenticationError):
        verify_google_id_token("token-without-sub")


def test_verify_accepts_both_documented_issuer_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for issuer in ("accounts.google.com", "https://accounts.google.com"):

        def fake_verify(token, request, audience, _issuer=issuer):
            return {"iss": _issuer, "sub": "12345", "email": "user@example.com"}

        monkeypatch.setattr(
            "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
        )

        claims = verify_google_id_token("valid-token")
        assert claims["sub"] == "12345"


def test_verify_passes_configured_client_id_as_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_verify(token, request, audience):
        captured["audience"] = audience
        return {"iss": "https://accounts.google.com", "sub": "12345"}

    monkeypatch.setattr(
        "hermes_v2.auth.google.id_token.verify_oauth2_token", fake_verify
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "the-configured-client-id")

    verify_google_id_token("token")

    assert captured["audience"] == "the-configured-client-id"
