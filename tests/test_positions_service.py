"""Tests for PositionsService's derived Spot positions and cost basis."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hermes_v2.integrations.binance import BinanceRequestError
from hermes_v2.trading.positions_service import PositionsService

_NOON_TODAY = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _buy_trade(qty: str, price: str, time: int) -> dict:
    return {"qty": qty, "price": price, "time": time, "is_buyer": True}


def _sell_trade(qty: str, price: str, time: int) -> dict:
    return {"qty": qty, "price": price, "time": time, "is_buyer": False}


class _FakeClient:
    def __init__(
        self, balances, trades_by_symbol=None, market_data_by_symbol=None
    ) -> None:
        self._balances = balances
        self._trades_by_symbol = trades_by_symbol or {}
        self._market_data_by_symbol = market_data_by_symbol or {}

    def get_balances(self):
        return self._balances

    def get_trades(self, symbol: str):
        if symbol not in self._trades_by_symbol:
            raise BinanceRequestError(f"no trades for {symbol}")
        return self._trades_by_symbol[symbol]

    def get_market_data(self, symbol: str):
        if symbol not in self._market_data_by_symbol:
            raise BinanceRequestError(f"no market data for {symbol}")
        return self._market_data_by_symbol[symbol]


# --- get_positions() ----------------------------------------------------------


def test_quote_asset_balance_is_never_a_position() -> None:
    client = _FakeClient(balances=[{"asset": "USDT", "free": "1000", "locked": "0"}])
    service = PositionsService(client, quote_asset="USDT")

    assert service.get_positions() == []


def test_zero_balance_asset_is_not_a_position() -> None:
    client = _FakeClient(balances=[{"asset": "BTC", "free": "0", "locked": "0"}])
    service = PositionsService(client, quote_asset="USDT")

    assert service.get_positions() == []


def test_single_buy_produces_a_position_at_that_entry_price() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={"BTCUSDT": [_buy_trade("0.01", "50000", _ms(_NOON_TODAY))]},
        market_data_by_symbol={"BTCUSDT": {"last_price": "55000"}},
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    positions = service.get_positions()

    assert len(positions) == 1
    position = positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.quantity == Decimal("0.01")
    assert position.average_entry_price == Decimal("50000")
    assert position.current_price == Decimal("55000")
    assert position.unrealized_pnl_quote == Decimal("50")
    assert position.unrealized_pnl_pct == Decimal("10")


def test_two_buys_produce_a_weighted_average_entry_price() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.02", "locked": "0"}],
        trades_by_symbol={
            "BTCUSDT": [
                _buy_trade("0.01", "40000", _ms(_NOON_TODAY)),
                _buy_trade("0.01", "60000", _ms(_NOON_TODAY) + 1000),
            ]
        },
        market_data_by_symbol={"BTCUSDT": {"last_price": "50000"}},
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    position = service.get_positions()[0]

    assert position.average_entry_price == Decimal("50000")
    assert position.unrealized_pnl_quote == Decimal("0")


def test_buy_then_partial_sell_reduces_quantity_but_keeps_average_cost() -> None:
    """Weighted-average cost basis: selling part of a position doesn't
    change the average cost of what remains."""
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={
            "BTCUSDT": [
                _buy_trade("0.02", "40000", _ms(_NOON_TODAY)),
                _sell_trade("0.01", "50000", _ms(_NOON_TODAY) + 1000),
            ]
        },
        market_data_by_symbol={"BTCUSDT": {"last_price": "45000"}},
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    position = service.get_positions()[0]

    assert position.quantity == Decimal("0.01")
    assert position.average_entry_price == Decimal("40000")


def test_unpriceable_symbol_yields_none_current_price_and_pnl() -> None:
    client = _FakeClient(
        balances=[{"asset": "OBSCURE", "free": "10", "locked": "0"}],
        trades_by_symbol={"OBSCUREUSDT": []},
    )
    service = PositionsService(client, quote_asset="USDT")

    position = service.get_positions()[0]

    assert position.current_price is None
    assert position.unrealized_pnl_quote is None
    assert position.value_quote is None


def test_missing_trade_history_still_yields_a_position_without_entry_price() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        market_data_by_symbol={"BTCUSDT": {"last_price": "50000"}},
    )  # no trades_by_symbol entry -> get_trades raises
    service = PositionsService(client, quote_asset="USDT")

    position = service.get_positions()[0]

    assert position.average_entry_price is None
    assert position.unrealized_pnl_quote is None
    assert position.value_quote == Decimal(
        "500.00"
    )  # still priced from current market data


# --- get_position(symbol) -------------------------------------------------------


def test_get_position_returns_none_for_zero_balance() -> None:
    client = _FakeClient(balances=[{"asset": "BTC", "free": "0", "locked": "0"}])
    service = PositionsService(client, quote_asset="USDT")

    assert service.get_position("BTCUSDT") is None


def test_get_position_returns_none_for_a_symbol_not_quoted_in_the_quote_asset() -> None:
    client = _FakeClient(balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}])
    service = PositionsService(client, quote_asset="USDT")

    assert service.get_position("BTCETH") is None


def test_get_position_finds_the_held_asset() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={"BTCUSDT": [_buy_trade("0.01", "50000", _ms(_NOON_TODAY))]},
        market_data_by_symbol={"BTCUSDT": {"last_price": "50000"}},
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    position = service.get_position("BTCUSDT")

    assert position is not None
    assert position.symbol == "BTCUSDT"


# --- realized loss today --------------------------------------------------------


def test_realized_loss_today_is_zero_with_no_trades() -> None:
    client = _FakeClient(balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}])
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    assert service.get_realized_loss_today_quote() == Decimal("0")


def test_realized_loss_today_sums_a_losing_sell() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={
            "BTCUSDT": [
                _buy_trade("0.02", "50000", _ms(_NOON_TODAY)),
                _sell_trade("0.01", "45000", _ms(_NOON_TODAY) + 1000),  # sold at a loss
            ]
        },
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    # sold 0.01 at 45000 vs avg cost 50000 -> realized loss of 50
    assert service.get_realized_loss_today_quote() == Decimal("50")


def test_realized_gain_today_does_not_produce_a_negative_loss() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={
            "BTCUSDT": [
                _buy_trade("0.02", "40000", _ms(_NOON_TODAY)),
                _sell_trade("0.01", "50000", _ms(_NOON_TODAY) + 1000),  # sold at a gain
            ]
        },
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    assert service.get_realized_loss_today_quote() == Decimal("0")


def test_realized_loss_excludes_trades_from_before_today() -> None:
    yesterday = _NOON_TODAY.replace(day=_NOON_TODAY.day - 1)
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        trades_by_symbol={
            "BTCUSDT": [
                _buy_trade("0.02", "50000", _ms(yesterday)),
                _sell_trade("0.01", "45000", _ms(yesterday) + 1000),  # yesterday's loss
            ]
        },
    )
    service = PositionsService(client, quote_asset="USDT", now=_NOON_TODAY)

    assert service.get_realized_loss_today_quote() == Decimal("0")
