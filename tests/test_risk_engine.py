"""Tests for RiskEngine and its env-driven RiskLimits loader.

No threshold is invented anywhere in this file's assertions about
`load_risk_limits()` — every expected value traces back to an env var this
test itself set. The RiskEngine tests build `RiskLimits` directly, never via
the environment, so they stay fast and don't leak state between tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hermes_v2.trading.risk_engine import (
    AccountRiskSnapshot,
    OrderRiskRequest,
    RiskEngine,
    RiskLimits,
    load_risk_limits,
)

_FULLY_CONFIGURED_LIMITS = RiskLimits(
    max_order_notional_quote=Decimal("1000"),
    max_symbol_exposure_pct=Decimal("25"),
    max_total_exposure_pct=Decimal("50"),
    max_daily_loss_pct=Decimal("5"),
    max_open_positions=3,
    allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
)

_UNCONFIGURED_LIMITS = RiskLimits(
    max_order_notional_quote=None,
    max_symbol_exposure_pct=None,
    max_total_exposure_pct=None,
    max_daily_loss_pct=None,
    max_open_positions=None,
    allowed_symbols=None,
)


def _snapshot(
    total_portfolio_value_quote: str = "10000",
    current_symbol_exposure_quote: str = "0",
    current_total_exposure_quote: str = "0",
    open_position_count: int = 0,
    realized_loss_today_quote: str = "0",
) -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        total_portfolio_value_quote=Decimal(total_portfolio_value_quote),
        current_symbol_exposure_quote=Decimal(current_symbol_exposure_quote),
        current_total_exposure_quote=Decimal(current_total_exposure_quote),
        open_position_count=open_position_count,
        realized_loss_today_quote=Decimal(realized_loss_today_quote),
    )


def _buy(
    symbol: str = "BTCUSDT",
    notional: str = "100",
    is_new_symbol: bool = True,
) -> OrderRiskRequest:
    return OrderRiskRequest(
        symbol=symbol,
        side="BUY",
        estimated_notional_quote=Decimal(notional),
        is_new_symbol_for_account=is_new_symbol,
    )


def _sell(symbol: str = "BTCUSDT", notional: str = "100") -> OrderRiskRequest:
    return OrderRiskRequest(
        symbol=symbol,
        side="SELL",
        estimated_notional_quote=Decimal(notional),
        is_new_symbol_for_account=False,
    )


# --- load_risk_limits() ------------------------------------------------------


def test_load_risk_limits_defaults_to_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HERMES_RISK_MAX_ORDER_NOTIONAL_USD",
        "HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT",
        "HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT",
        "HERMES_RISK_MAX_DAILY_LOSS_PCT",
        "HERMES_RISK_MAX_OPEN_POSITIONS",
        "HERMES_RISK_ALLOWED_SYMBOLS",
    ):
        monkeypatch.delenv(name, raising=False)

    limits = load_risk_limits()

    assert limits == _UNCONFIGURED_LIMITS


def test_load_risk_limits_reads_every_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "1000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "25")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "50")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "5")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "btcusdt, ethusdt")

    limits = load_risk_limits()

    assert limits == _FULLY_CONFIGURED_LIMITS


def test_load_risk_limits_rejects_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "not-a-number")

    with pytest.raises(ValueError, match="HERMES_RISK_MAX_ORDER_NOTIONAL_USD"):
        load_risk_limits()


# --- non-finite values (NaN/Infinity) -----------------------------------------
#
# Decimal NaN comparisons raise decimal.InvalidOperation instead of
# returning False the way float NaN does. OrderValidator already blocks a
# non-finite order quantity/price before RiskEngine runs, but the account
# snapshot (from PortfolioService/PositionsService, ultimately Binance's own
# data) isn't re-validated upstream — RiskEngine must reject these itself,
# cleanly, rather than crash.


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_portfolio_value_is_rejected_without_raising(
    bad_value: Decimal,
) -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = AccountRiskSnapshot(
        total_portfolio_value_quote=bad_value,
        current_symbol_exposure_quote=Decimal("0"),
        current_total_exposure_quote=Decimal("0"),
        open_position_count=0,
        realized_loss_today_quote=Decimal("0"),
    )

    decision = engine.validate_order(_buy(notional="10"), snapshot)

    assert decision.approved is False
    assert "finite" in decision.reason.lower()


def test_non_finite_estimated_notional_is_rejected_without_raising() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    request = OrderRiskRequest(
        symbol="BTCUSDT",
        side="BUY",
        estimated_notional_quote=Decimal("NaN"),
        is_new_symbol_for_account=True,
    )

    decision = engine.validate_order(request, _snapshot())

    assert decision.approved is False
    assert "finite" in decision.reason.lower()


def test_non_finite_realized_loss_is_rejected_without_raising() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot()
    snapshot = AccountRiskSnapshot(
        **{**snapshot.__dict__, "realized_loss_today_quote": Decimal("Infinity")}
    )

    decision = engine.validate_order(_buy(notional="10"), snapshot)

    assert decision.approved is False
    assert "finite" in decision.reason.lower()


# --- fail-closed on missing config -------------------------------------------


def test_order_rejected_when_no_limits_configured_at_all() -> None:
    engine = RiskEngine(_UNCONFIGURED_LIMITS)

    decision = engine.validate_order(_buy(), _snapshot())

    assert decision.approved is False
    assert "ALLOWED_SYMBOLS" in decision.reason


@pytest.mark.parametrize(
    "missing_field",
    [
        "allowed_symbols",
        "max_daily_loss_pct",
        "max_order_notional_quote",
        "max_symbol_exposure_pct",
        "max_total_exposure_pct",
        "max_open_positions",
    ],
)
def test_order_rejected_when_a_single_limit_is_missing(missing_field: str) -> None:
    limits_dict = _FULLY_CONFIGURED_LIMITS.__dict__.copy()
    limits_dict[missing_field] = None
    engine = RiskEngine(RiskLimits(**limits_dict))

    decision = engine.validate_order(_buy(notional="50"), _snapshot())

    assert decision.approved is False


# --- zero and negative limits: must fail closed, never open --------------------
#
# A limit of 0 or a negative number is almost certainly a misconfiguration
# (nobody means "allow negative exposure"), but the important property is
# that it can never accidentally *bypass* a check — every one of these
# should end up blocking every order, the same direction as "not
# configured," never the opposite.


def test_zero_order_notional_limit_blocks_every_order() -> None:
    limits = RiskLimits(
        **{
            **_FULLY_CONFIGURED_LIMITS.__dict__,
            "max_order_notional_quote": Decimal("0"),
        }
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(_buy(notional="0.01"), _snapshot())

    assert decision.approved is False


def test_negative_order_notional_limit_blocks_every_order() -> None:
    limits = RiskLimits(
        **{
            **_FULLY_CONFIGURED_LIMITS.__dict__,
            "max_order_notional_quote": Decimal("-100"),
        }
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(_buy(notional="0.01"), _snapshot())

    assert decision.approved is False


def test_zero_symbol_exposure_limit_blocks_every_buy() -> None:
    limits = RiskLimits(
        **{**_FULLY_CONFIGURED_LIMITS.__dict__, "max_symbol_exposure_pct": Decimal("0")}
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(
        _buy(notional="10", is_new_symbol=False), _snapshot()
    )

    assert decision.approved is False


def test_negative_total_exposure_limit_blocks_every_buy() -> None:
    limits = RiskLimits(
        **{**_FULLY_CONFIGURED_LIMITS.__dict__, "max_total_exposure_pct": Decimal("-1")}
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(_buy(notional="10"), _snapshot())

    assert decision.approved is False


def test_zero_daily_loss_limit_blocks_every_order_even_with_no_loss_today() -> None:
    limits = RiskLimits(
        **{**_FULLY_CONFIGURED_LIMITS.__dict__, "max_daily_loss_pct": Decimal("0")}
    )
    engine = RiskEngine(limits)
    snapshot = _snapshot(realized_loss_today_quote="0")

    decision = engine.validate_order(_buy(notional="10"), snapshot)

    assert decision.approved is False


def test_zero_open_positions_limit_blocks_every_new_symbol() -> None:
    limits = RiskLimits(
        **{**_FULLY_CONFIGURED_LIMITS.__dict__, "max_open_positions": 0}
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(
        _buy(symbol="ETHUSDT", notional="10", is_new_symbol=True),
        _snapshot(open_position_count=0),
    )

    assert decision.approved is False


def test_negative_open_positions_limit_blocks_every_new_symbol() -> None:
    limits = RiskLimits(
        **{**_FULLY_CONFIGURED_LIMITS.__dict__, "max_open_positions": -1}
    )
    engine = RiskEngine(limits)

    decision = engine.validate_order(
        _buy(symbol="ETHUSDT", notional="10", is_new_symbol=True),
        _snapshot(open_position_count=0),
    )

    assert decision.approved is False


# --- allowed symbols ----------------------------------------------------------


def test_disallowed_symbol_is_rejected() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)

    decision = engine.validate_order(_buy(symbol="DOGEUSDT"), _snapshot())

    assert decision.approved is False
    assert "DOGEUSDT" in decision.reason


# --- daily loss ---------------------------------------------------------------


def test_daily_loss_at_limit_rejects() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        total_portfolio_value_quote="10000", realized_loss_today_quote="500"
    )  # 5% loss, limit is 5%

    decision = engine.validate_order(_buy(notional="10"), snapshot)

    assert decision.approved is False
    assert "Daily loss" in decision.reason


def test_daily_loss_blocks_sell_orders_too() -> None:
    """A daily circuit breaker halts everything, not just new buys."""
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        total_portfolio_value_quote="10000", realized_loss_today_quote="600"
    )

    decision = engine.validate_order(_sell(notional="10"), snapshot)

    assert decision.approved is False


def test_daily_loss_below_limit_passes_that_check() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        total_portfolio_value_quote="10000", realized_loss_today_quote="100"
    )  # 1% loss

    decision = engine.validate_order(_buy(notional="10", is_new_symbol=False), snapshot)

    assert decision.approved is True


# --- order notional -------------------------------------------------------------


def test_order_notional_over_limit_rejects() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)

    decision = engine.validate_order(_buy(notional="1500"), _snapshot())

    assert decision.approved is False
    assert "notional" in decision.reason.lower()


# --- symbol exposure (BUY only) -------------------------------------------------


def test_symbol_exposure_over_limit_rejects_a_buy() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    # current exposure 2000/10000=20%, +900 -> 29% > 25% limit
    snapshot = _snapshot(
        total_portfolio_value_quote="10000",
        current_symbol_exposure_quote="2000",
        current_total_exposure_quote="2000",
    )

    decision = engine.validate_order(
        _buy(notional="900", is_new_symbol=False), snapshot
    )

    assert decision.approved is False
    assert "exposure" in decision.reason.lower()


def test_sell_orders_are_never_blocked_by_symbol_exposure() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        total_portfolio_value_quote="10000",
        current_symbol_exposure_quote="9000",
        current_total_exposure_quote="9000",
    )

    decision = engine.validate_order(_sell(notional="500"), snapshot)

    assert decision.approved is True


# --- total exposure (BUY only) --------------------------------------------------


def test_total_exposure_over_limit_rejects_a_buy() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    # 4800/10000=48%, +300 -> 51% > 50% limit
    snapshot = _snapshot(
        total_portfolio_value_quote="10000",
        current_symbol_exposure_quote="0",
        current_total_exposure_quote="4800",
    )

    decision = engine.validate_order(_buy(notional="300"), snapshot)

    assert decision.approved is False
    assert "total exposure" in decision.reason.lower()


# --- open positions (BUY opening a new symbol only) -----------------------------


def test_open_positions_at_limit_rejects_a_new_symbol() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(open_position_count=3)

    decision = engine.validate_order(
        _buy(symbol="ETHUSDT", notional="10", is_new_symbol=True), snapshot
    )

    assert decision.approved is False
    assert "open-position" in decision.reason.lower()


def test_open_positions_limit_does_not_block_adding_to_an_existing_symbol() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        open_position_count=3,
        current_symbol_exposure_quote="10",
        current_total_exposure_quote="10",
    )

    decision = engine.validate_order(
        _buy(symbol="BTCUSDT", notional="10", is_new_symbol=False), snapshot
    )

    assert decision.approved is True


# --- everything passing -----------------------------------------------------------


def test_fully_valid_buy_within_every_limit_is_approved() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(
        total_portfolio_value_quote="10000",
        current_symbol_exposure_quote="0",
        current_total_exposure_quote="0",
        open_position_count=1,
        realized_loss_today_quote="0",
    )

    decision = engine.validate_order(_buy(notional="100"), snapshot)

    assert decision.approved is True
    assert decision.reason is None


def test_fully_valid_sell_within_every_limit_is_approved() -> None:
    engine = RiskEngine(_FULLY_CONFIGURED_LIMITS)
    snapshot = _snapshot(total_portfolio_value_quote="10000")

    decision = engine.validate_order(_sell(notional="100"), snapshot)

    assert decision.approved is True
