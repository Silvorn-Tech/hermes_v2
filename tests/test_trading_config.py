"""Tests for the global trading kill switch."""

from __future__ import annotations

import pytest

from hermes_v2.trading.config import is_trading_enabled


def test_defaults_to_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    assert is_trading_enabled() is False


@pytest.mark.parametrize("falsy_value", ["false", "0", "no", "off", "", "FALSE"])
def test_falsy_values_disable_trading(
    monkeypatch: pytest.MonkeyPatch, falsy_value: str
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", falsy_value)
    assert is_trading_enabled() is False


@pytest.mark.parametrize("truthy_value", ["true", "1", "yes", "on", "TRUE"])
def test_truthy_values_enable_trading(
    monkeypatch: pytest.MonkeyPatch, truthy_value: str
) -> None:
    monkeypatch.setenv("TRADING_ENABLED", truthy_value)
    assert is_trading_enabled() is True
