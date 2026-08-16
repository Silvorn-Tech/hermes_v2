"""Tests for OrderValidator and its exchangeInfo-to-SymbolFilters conversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hermes_v2.trading.order_validator import (
    OrderValidationRequest,
    OrderValidator,
    SymbolFilters,
    SymbolFiltersError,
    symbol_filters_from_exchange_info,
)

_FILTERS = SymbolFilters(
    status="TRADING",
    min_qty=Decimal("0.001"),
    max_qty=Decimal("100"),
    step_size=Decimal("0.001"),
    min_price=Decimal("0.01"),
    max_price=Decimal("1000000"),
    tick_size=Decimal("0.01"),
    min_notional=Decimal("10"),
)


def _market(symbol="BTCUSDT", side="BUY", quantity="0.01"):
    return OrderValidationRequest(
        symbol=symbol,
        side=side,
        order_type="MARKET",
        quantity=Decimal(quantity),
        price=None,
    )


def _limit(symbol="BTCUSDT", side="BUY", quantity="0.01", price="50000"):
    return OrderValidationRequest(
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        quantity=Decimal(quantity),
        price=Decimal(price),
    )


# --- symbol_filters_from_exchange_info ---------------------------------------


def test_symbol_filters_from_exchange_info_converts_every_field() -> None:
    info = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "filters": {
            "min_qty": "0.001",
            "max_qty": "100",
            "step_size": "0.001",
            "min_price": "0.01",
            "max_price": "1000000",
            "tick_size": "0.01",
            "min_notional": "10",
        },
    }
    filters = symbol_filters_from_exchange_info(info)
    assert filters == _FILTERS


def test_symbol_filters_from_exchange_info_raises_on_missing_filter() -> None:
    info = {"symbol": "BTCUSDT", "status": "TRADING", "filters": {"min_qty": "0.001"}}
    with pytest.raises(SymbolFiltersError, match="missing filter"):
        symbol_filters_from_exchange_info(info)


def test_symbol_filters_from_exchange_info_raises_on_non_numeric_filter() -> None:
    info = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "filters": {
            "min_qty": "not-a-number",
            "max_qty": "100",
            "step_size": "0.001",
            "min_price": "0.01",
            "max_price": "1000000",
            "tick_size": "0.01",
            "min_notional": "10",
        },
    }
    with pytest.raises(SymbolFiltersError, match="non-numeric"):
        symbol_filters_from_exchange_info(info)


# --- basic shape validation ----------------------------------------------------


def test_invalid_side_is_rejected() -> None:
    validator = OrderValidator()
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="HOLD",
        order_type="MARKET",
        quantity=Decimal("1"),
        price=None,
    )
    result = validator.validate(request, Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "side" in result.reason.lower()


def test_invalid_order_type_is_rejected() -> None:
    validator = OrderValidator()
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="STOP",
        quantity=Decimal("1"),
        price=None,
    )
    result = validator.validate(request, Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "order type" in result.reason.lower()


def test_zero_quantity_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0"), Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "quantity" in result.reason.lower()


def test_negative_quantity_is_rejected() -> None:
    validator = OrderValidator()
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("-1"),
        price=None,
    )
    result = validator.validate(request, Decimal("50000"), _FILTERS)
    assert result.approved is False


def test_market_order_with_a_price_is_rejected() -> None:
    validator = OrderValidator()
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
        price=Decimal("50000"),
    )
    result = validator.validate(request, Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "MARKET" in result.reason


def test_limit_order_without_a_price_is_rejected() -> None:
    validator = OrderValidator()
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.01"),
        price=None,
    )
    result = validator.validate(request, Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "LIMIT" in result.reason


def test_limit_order_with_zero_price_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_limit(price="0"), Decimal("50000"), _FILTERS)
    assert result.approved is False


# --- symbol tradability ---------------------------------------------------------


def test_non_trading_symbol_is_rejected() -> None:
    validator = OrderValidator()
    halted_filters = SymbolFilters(**{**_FILTERS.__dict__, "status": "HALT"})
    result = validator.validate(_market(), Decimal("50000"), halted_filters)
    assert result.approved is False
    assert "not currently tradable" in result.reason


# --- quantity precision -----------------------------------------------------------


def test_quantity_below_min_qty_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0.0001"), Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "outside the allowed range" in result.reason


def test_quantity_above_max_qty_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="200"), Decimal("50000"), _FILTERS)
    assert result.approved is False


def test_quantity_not_matching_step_size_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0.0015"), Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "step size" in result.reason


def test_quantity_matching_step_size_passes() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0.002"), Decimal("50000"), _FILTERS)
    assert result.approved is True


# --- price precision (LIMIT only) --------------------------------------------------


def test_limit_price_below_min_price_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_limit(price="0.001"), Decimal("50000"), _FILTERS)
    assert result.approved is False


def test_limit_price_not_matching_tick_size_is_rejected() -> None:
    validator = OrderValidator()
    result = validator.validate(_limit(price="50000.005"), Decimal("50000"), _FILTERS)
    assert result.approved is False
    assert "tick size" in result.reason


def test_limit_price_matching_tick_size_passes() -> None:
    validator = OrderValidator()
    result = validator.validate(
        _limit(quantity="0.01", price="50000.01"), Decimal("50000"), _FILTERS
    )
    assert result.approved is True


# --- notional -----------------------------------------------------------------------


def test_market_order_below_min_notional_is_rejected() -> None:
    validator = OrderValidator()
    tiny_filters = SymbolFilters(**{**_FILTERS.__dict__, "min_qty": Decimal("0.0001")})
    request = OrderValidationRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.0001"),
        price=None,
    )
    result = validator.validate(request, Decimal("50000"), tiny_filters)
    assert result.approved is False
    assert "below the minimum" in result.reason


def test_market_order_notional_uses_market_price() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0.01"), Decimal("50000"), _FILTERS)
    assert result.approved is True
    assert result.estimated_notional_quote == Decimal("500.00")


def test_limit_order_notional_uses_limit_price_not_market_price() -> None:
    validator = OrderValidator()
    result = validator.validate(
        _limit(quantity="0.01", price="50000.01"), Decimal("1"), _FILTERS
    )
    assert result.approved is True
    assert result.estimated_notional_quote == Decimal("500.0001")


# --- fully valid orders --------------------------------------------------------------


def test_fully_valid_market_order_is_approved() -> None:
    validator = OrderValidator()
    result = validator.validate(_market(quantity="0.01"), Decimal("50000"), _FILTERS)
    assert result.approved is True
    assert result.reason is None


def test_fully_valid_limit_sell_order_is_approved() -> None:
    validator = OrderValidator()
    result = validator.validate(
        _limit(side="SELL", quantity="0.01", price="50000.01"),
        Decimal("50000"),
        _FILTERS,
    )
    assert result.approved is True
