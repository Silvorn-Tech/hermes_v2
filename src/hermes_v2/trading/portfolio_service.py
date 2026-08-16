"""PortfolioService — balances and an aggregate portfolio value.

Deliberately does **not** compute a daily P&L figure: that needs a
historical baseline (start-of-day portfolio value) that nothing in this
codebase persists yet. Reporting `None` for anything Hermes can't actually
compute is the honest choice — never fabricate a number to fill the field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hermes_v2.integrations.binance import BinanceClient, BinanceError

_DEFAULT_QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class PricedBalance:
    asset: str
    free: Decimal
    locked: Decimal
    value_quote: Decimal | None  # None when this asset couldn't be priced
    priced: bool


class PortfolioService:
    """Wraps `BinanceClient` reads needed for `GET /portfolio` and
    `GET /balances`. Read-only — never calls a write method."""

    def __init__(
        self, client: BinanceClient, quote_asset: str = _DEFAULT_QUOTE_ASSET
    ) -> None:
        self._client = client
        self._quote_asset = quote_asset.upper()

    def get_balances(self) -> list[PricedBalance]:
        raw_balances = self._client.get_balances()
        return [self._price_balance(entry) for entry in raw_balances]

    def _price_balance(self, entry: dict[str, Any]) -> PricedBalance:
        asset = entry["asset"]
        free = Decimal(entry["free"])
        locked = Decimal(entry["locked"])
        total_quantity = free + locked

        if asset == self._quote_asset:
            return PricedBalance(
                asset=asset,
                free=free,
                locked=locked,
                value_quote=total_quantity,
                priced=True,
            )

        try:
            market = self._client.get_market_data(f"{asset}{self._quote_asset}")
            price = Decimal(market["last_price"])
        except BinanceError:
            return PricedBalance(
                asset=asset, free=free, locked=locked, value_quote=None, priced=False
            )

        return PricedBalance(
            asset=asset,
            free=free,
            locked=locked,
            value_quote=total_quantity * price,
            priced=True,
        )

    def get_portfolio(self) -> dict[str, Any]:
        balances = self.get_balances()
        total_value_quote = sum(
            (
                balance.value_quote
                for balance in balances
                if balance.value_quote is not None
            ),
            Decimal("0"),
        )
        return {
            "quote_asset": self._quote_asset,
            "total_value_quote": total_value_quote,
            "as_of": datetime.now(UTC).isoformat(),
            "balances": [
                {
                    "asset": balance.asset,
                    "free": balance.free,
                    "locked": balance.locked,
                    "value_quote": balance.value_quote,
                    "priced": balance.priced,
                }
                for balance in balances
            ],
        }

    def get_total_value_quote(self) -> Decimal:
        """The single number RiskEngine needs — total portfolio value in the
        quote asset. Reuses `get_portfolio()` rather than duplicating the
        pricing loop."""
        return self.get_portfolio()["total_value_quote"]


__all__ = ["PortfolioService", "PricedBalance"]
