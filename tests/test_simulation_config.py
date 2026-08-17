"""Unit tests for Simulation Mode's env-driven config defaults — mirrors
how risk_engine.py's own HERMES_RISK_* parsing is tested: default value
when unset, override when set, and re-read (never cached) per call.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hermes_v2.trading.simulation_config import (
    default_simulation_initial_capital,
    default_simulation_quote_asset,
    simulation_fee_rate_pct,
    simulation_slippage_rate_pct,
)


def test_default_initial_capital_is_ten_thousand_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", raising=False)
    assert default_simulation_initial_capital() == Decimal("10000")


def test_default_initial_capital_reads_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", "25000")
    assert default_simulation_initial_capital() == Decimal("25000")


def test_default_initial_capital_is_re_read_every_call_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", "5000")
    assert default_simulation_initial_capital() == Decimal("5000")
    monkeypatch.setenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", "7000")
    assert default_simulation_initial_capital() == Decimal("7000")


def test_default_quote_asset_is_usdt() -> None:
    assert default_simulation_quote_asset() == "USDT"


def test_fee_rate_defaults_to_zero_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_SIMULATION_FEE_RATE_PCT", raising=False)
    assert simulation_fee_rate_pct() == Decimal("0")


def test_fee_rate_reads_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_FEE_RATE_PCT", "0.1")
    assert simulation_fee_rate_pct() == Decimal("0.1")


def test_slippage_rate_defaults_to_zero_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SIMULATION_SLIPPAGE_RATE_PCT", raising=False)
    assert simulation_slippage_rate_pct() == Decimal("0")


def test_slippage_rate_reads_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_SLIPPAGE_RATE_PCT", "0.5")
    assert simulation_slippage_rate_pct() == Decimal("0.5")


def test_invalid_decimal_env_value_raises_not_a_silent_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SIMULATION_INITIAL_CAPITAL_USD", "not-a-number")
    with pytest.raises(ValueError, match="must be a decimal number"):
        default_simulation_initial_capital()
