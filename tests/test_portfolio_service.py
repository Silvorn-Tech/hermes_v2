"""Tests for PortfolioService's balance pricing and aggregation."""

from __future__ import annotations

from decimal import Decimal

from hermes_v2.integrations.binance import BinanceRequestError
from hermes_v2.trading.portfolio_service import PortfolioService


class _FakeClient:
    def __init__(self, balances, market_data_by_symbol=None) -> None:
        self._balances = balances
        self._market_data_by_symbol = market_data_by_symbol or {}

    def get_balances(self):
        return self._balances

    def get_market_data(self, symbol: str):
        if symbol not in self._market_data_by_symbol:
            raise BinanceRequestError(f"no market data for {symbol}")
        return self._market_data_by_symbol[symbol]


def test_quote_asset_balance_is_valued_at_face_value() -> None:
    client = _FakeClient(balances=[{"asset": "USDT", "free": "100", "locked": "50"}])
    service = PortfolioService(client, quote_asset="USDT")

    balances = service.get_balances()

    assert len(balances) == 1
    assert balances[0].value_quote == Decimal("150")
    assert balances[0].priced is True


def test_non_quote_asset_is_priced_via_market_data() -> None:
    client = _FakeClient(
        balances=[{"asset": "BTC", "free": "0.01", "locked": "0"}],
        market_data_by_symbol={"BTCUSDT": {"last_price": "50000"}},
    )
    service = PortfolioService(client, quote_asset="USDT")

    balances = service.get_balances()

    assert balances[0].value_quote == Decimal("500.00")
    assert balances[0].priced is True


def test_unpriceable_asset_is_reported_as_unpriced_not_zero() -> None:
    client = _FakeClient(balances=[{"asset": "OBSCURE", "free": "10", "locked": "0"}])
    service = PortfolioService(client, quote_asset="USDT")

    balances = service.get_balances()

    assert balances[0].value_quote is None
    assert balances[0].priced is False


def test_portfolio_total_excludes_unpriced_balances() -> None:
    client = _FakeClient(
        balances=[
            {"asset": "USDT", "free": "1000", "locked": "0"},
            {"asset": "BTC", "free": "0.01", "locked": "0"},
            {"asset": "OBSCURE", "free": "999999", "locked": "0"},
        ],
        market_data_by_symbol={"BTCUSDT": {"last_price": "50000"}},
    )
    service = PortfolioService(client, quote_asset="USDT")

    portfolio = service.get_portfolio()

    assert portfolio["total_value_quote"] == Decimal("1500.00")
    assert portfolio["quote_asset"] == "USDT"
    unpriced = [b for b in portfolio["balances"] if not b["priced"]]
    assert len(unpriced) == 1
    assert unpriced[0]["value_quote"] is None


def test_portfolio_never_fabricates_a_daily_pnl_field() -> None:
    client = _FakeClient(balances=[])
    service = PortfolioService(client, quote_asset="USDT")

    portfolio = service.get_portfolio()

    assert "daily_pnl" not in portfolio
    assert "daily_pnl_pct" not in portfolio


def test_get_total_value_quote_matches_get_portfolio() -> None:
    client = _FakeClient(balances=[{"asset": "USDT", "free": "42", "locked": "0"}])
    service = PortfolioService(client, quote_asset="USDT")

    assert service.get_total_value_quote() == Decimal("42")
