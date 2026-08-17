"""Tests for `hermes binance-check` (src/hermes_v2/cli.py:binance_check).

Uses a fake BinanceClient so these run without network access or real
credentials — no fixture in this file holds anything resembling a real
Binance API key or secret.
"""

from __future__ import annotations

import pytest

from hermes_v2 import cli
from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceClient,
    BinanceConfigurationError,
)

_FAKE_API_KEY = "unit-test-fake-api-key-do-not-use"
_FAKE_API_SECRET = "unit-test-fake-api-secret-do-not-use"


class _FakeUnauthorizedResponse:
    status_code = 401
    ok = False

    def json(self) -> dict:
        return {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}


class _FakeUnauthorizedSession:
    def get(self, url, params=None, headers=None, timeout=None):
        return _FakeUnauthorizedResponse()


class _FakeClient:
    def __init__(
        self,
        *,
        ping_error: Exception | None = None,
        account: dict | None = None,
        account_error: Exception | None = None,
        permissions: dict | None = None,
        permissions_error: Exception | None = None,
    ) -> None:
        self._ping_error = ping_error
        self._account = account if account is not None else {"can_withdraw": False}
        self._account_error = account_error
        self._permissions = (
            permissions if permissions is not None else {"can_withdraw": False}
        )
        self._permissions_error = permissions_error

    def ping(self) -> bool:
        if self._ping_error:
            raise self._ping_error
        return True

    def get_account_info(self) -> dict:
        if self._account_error:
            raise self._account_error
        return self._account

    def get_api_key_permissions(self) -> dict:
        if self._permissions_error:
            raise self._permissions_error
        return self._permissions

    def get_balances(self) -> list:
        return []

    def get_market_data(self, symbol: str) -> dict:
        return {"symbol": symbol}

    def get_open_orders(self) -> list:
        return []


def test_binance_check_reports_ok_and_exits_zero_when_read_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli, "BinanceClient", lambda: _FakeClient())

    exit_code = cli.binance_check()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Binance connection: OK" in output
    assert "Account: OK" in output
    assert "Balances: OK" in output
    assert "Market data: OK" in output
    assert "Open orders: OK" in output
    assert "Permissions: READ_ONLY" in output


def test_binance_check_fails_when_withdrawals_are_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        cli, "BinanceClient", lambda: _FakeClient(permissions={"can_withdraw": True})
    )

    exit_code = cli.binance_check()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Permissions: UNSAFE" in output


def test_binance_check_reports_failure_and_nonzero_exit_on_connection_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        cli,
        "BinanceClient",
        lambda: _FakeClient(ping_error=BinanceAuthenticationError("bad key")),
    )

    exit_code = cli.binance_check()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Binance connection: FAIL" in output
    assert "bad key" in output


def test_binance_check_skips_all_checks_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def _raise_configuration_error():
        raise BinanceConfigurationError("BINANCE_API_KEY must be configured")

    monkeypatch.setattr(cli, "BinanceClient", _raise_configuration_error)

    exit_code = cli.binance_check()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Binance connection: FAIL" in output
    assert "Account: SKIPPED" in output
    assert "Permissions: SKIPPED" in output


def test_binance_check_output_never_contains_a_credential_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """End-to-end through the real BinanceClient (fake HTTP transport only):
    a 401 from Binance must produce failure output with zero trace of the
    real API key/secret the client was constructed with."""
    real_client = BinanceClient(
        api_key=_FAKE_API_KEY,
        api_secret=_FAKE_API_SECRET,
        session=_FakeUnauthorizedSession(),
    )
    monkeypatch.setattr(cli, "BinanceClient", lambda: real_client)

    exit_code = cli.binance_check()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Invalid API-key" in output
    assert _FAKE_API_KEY not in output
    assert _FAKE_API_SECRET not in output
