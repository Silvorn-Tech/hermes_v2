"""Tests for the trading-routes Origin-header CSRF guard."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hermes_v2.trading.origin_check import require_trusted_origin


def _build_test_app() -> FastAPI:
    app = FastAPI()

    @app.post("/test/protected")
    async def protected(_: None = Depends(require_trusted_origin)) -> dict:
        return {"ok": True}

    return app


def test_missing_origin_header_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", "https://app.example.com")
    client = TestClient(_build_test_app())

    response = client.post("/test/protected")

    assert response.status_code == 403


def test_untrusted_origin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", "https://app.example.com")
    client = TestClient(_build_test_app())

    response = client.post(
        "/test/protected", headers={"Origin": "https://evil.example.com"}
    )

    assert response.status_code == 403


def test_trusted_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", "https://app.example.com")
    client = TestClient(_build_test_app())

    response = client.post(
        "/test/protected", headers={"Origin": "https://app.example.com"}
    )

    assert response.status_code == 200


def test_one_of_multiple_configured_origins_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HERMES_ALLOWED_ORIGINS", "https://app.example.com,https://romeo.example.com"
    )
    client = TestClient(_build_test_app())

    response = client.post(
        "/test/protected", headers={"Origin": "https://romeo.example.com"}
    )

    assert response.status_code == 200


def test_unset_allowlist_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_ALLOWED_ORIGINS", raising=False)
    client = TestClient(_build_test_app())

    response = client.post(
        "/test/protected", headers={"Origin": "https://app.example.com"}
    )

    assert response.status_code == 403


def test_error_response_reveals_no_configuration_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_ALLOWED_ORIGINS", "https://app.example.com")
    client = TestClient(_build_test_app())

    response = client.post(
        "/test/protected", headers={"Origin": "https://evil.example.com"}
    )

    assert "app.example.com" not in response.text
