"""PositionsService — Binance Spot has no position endpoint, so a
"position" here is derived: a non-zero asset balance plus a weighted-average
cost basis computed from that asset's trade history.

**Position identity is the symbol itself** (e.g. `BTCUSDT`) — there is no
other stable identifier in Spot. "Closing a position" means submitting a
`MARKET SELL` for the full held quantity through `OrderService`, never a
bypass path (see `hermes_v2.trading.order_service`).

**Known limitation, stated rather than hidden:** cost basis and today's
realized P&L are computed by walking `BinanceClient.get_trades(symbol)`,
which Binance paginates (up to 1000 most recent trades per call, no
pagination cursor is used here). For a symbol with more historical trades
than that, the computed average entry price is approximate — based on the
most recent trade window, not full account history. A position fully closed
earlier the same day (now zero balance) no longer appears in
`get_positions()`, so its realized P&L for that closure isn't included in
`realized_loss_today_quote` either — `HERMES_RISK_MAX_DAILY_LOSS_PCT` is a
best-effort circuit breaker over currently-visible positions, not an exact
daily P&L ledger. A complete one would need either persisted portfolio
snapshots or trade-history pagination, both out of scope for this pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hermes_v2.integrations.binance import BinanceClient, BinanceError

_DEFAULT_QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class Position:
    symbol: str
    asset: str
    quantity: Decimal
    average_entry_price: Decimal | None
    current_price: Decimal | None
    value_quote: Decimal | None
    unrealized_pnl_quote: Decimal | None
    unrealized_pnl_pct: Decimal | None


def _cost_basis_walk(
    trades: list[dict[str, Any]],
) -> list[tuple[int, Decimal, Decimal, Decimal]]:
    """Walk trades chronologically, tracking weighted-average cost basis.

    Returns one entry per trade: `(time, held_qty_after, avg_cost_after,
    realized_pnl_delta)`. `realized_pnl_delta` is zero for every buy.
    """
    sorted_trades = sorted(trades, key=lambda trade: trade.get("time") or 0)
    held_qty = Decimal("0")
    total_cost = Decimal("0")
    steps: list[tuple[int, Decimal, Decimal, Decimal]] = []

    for trade in sorted_trades:
        qty = Decimal(str(trade["qty"]))
        price = Decimal(str(trade["price"]))
        trade_time = trade.get("time") or 0
        realized_delta = Decimal("0")

        if trade.get("is_buyer"):
            total_cost += qty * price
            held_qty += qty
        else:
            if held_qty > 0:
                avg_cost = total_cost / held_qty
                sold_qty = min(qty, held_qty)
                realized_delta = (price - avg_cost) * sold_qty
                total_cost -= avg_cost * sold_qty
                held_qty -= sold_qty
            # A sell beyond currently-tracked held_qty means this trade
            # history window starts mid-position (paginated out an earlier
            # buy) — the excess is simply not tracked rather than guessed at.

        avg_cost_after = (total_cost / held_qty) if held_qty > 0 else Decimal("0")
        steps.append((trade_time, held_qty, avg_cost_after, realized_delta))

    return steps


def _today_start_ms(now: datetime) -> int:
    start_of_day = now.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(start_of_day.timestamp() * 1000)


class PositionsService:
    def __init__(
        self,
        client: BinanceClient,
        quote_asset: str = _DEFAULT_QUOTE_ASSET,
        now: datetime | None = None,
    ) -> None:
        self._client = client
        self._quote_asset = quote_asset.upper()
        self._now = now or datetime.now(UTC)

    def get_positions(self) -> list[Position]:
        balances = self._client.get_balances()
        positions = []
        for balance in balances:
            asset = balance["asset"]
            if asset == self._quote_asset:
                continue
            quantity = Decimal(balance["free"]) + Decimal(balance["locked"])
            if quantity <= 0:
                continue
            position = self._build_position(asset, quantity)
            if position is not None:
                positions.append(position)
        return positions

    def get_position(self, symbol: str) -> Position | None:
        symbol = symbol.upper()
        if not symbol.endswith(self._quote_asset):
            return None
        asset = symbol[: -len(self._quote_asset)]
        balances = {b["asset"]: b for b in self._client.get_balances()}
        balance = balances.get(asset)
        if balance is None:
            return None
        quantity = Decimal(balance["free"]) + Decimal(balance["locked"])
        if quantity <= 0:
            return None
        return self._build_position(asset, quantity)

    def _build_position(self, asset: str, quantity: Decimal) -> Position | None:
        symbol = f"{asset}{self._quote_asset}"
        try:
            trades = self._client.get_trades(symbol)
        except BinanceError:
            trades = []

        steps = _cost_basis_walk(trades)
        average_entry_price = steps[-1][2] if steps and steps[-1][1] > 0 else None

        try:
            current_price = Decimal(self._client.get_market_data(symbol)["last_price"])
        except BinanceError:
            current_price = None

        value_quote = quantity * current_price if current_price is not None else None
        unrealized_pnl_quote = None
        unrealized_pnl_pct = None
        if average_entry_price is not None and current_price is not None:
            unrealized_pnl_quote = (current_price - average_entry_price) * quantity
            if average_entry_price > 0:
                unrealized_pnl_pct = (
                    (current_price - average_entry_price) / average_entry_price * 100
                )

        return Position(
            symbol=symbol,
            asset=asset,
            quantity=quantity,
            average_entry_price=average_entry_price,
            current_price=current_price,
            value_quote=value_quote,
            unrealized_pnl_quote=unrealized_pnl_quote,
            unrealized_pnl_pct=unrealized_pnl_pct,
        )

    def get_realized_loss_today_quote(self) -> Decimal:
        """Sum of today's realized losses across currently-held positions'
        symbols (see the module docstring for the exact scope this covers).
        Always non-negative: a net gain today contributes zero, never a
        negative "loss.\""""
        today_start_ms = _today_start_ms(self._now)
        total_realized = Decimal("0")

        for balance in self._client.get_balances():
            asset = balance["asset"]
            if asset == self._quote_asset:
                continue
            symbol = f"{asset}{self._quote_asset}"
            try:
                trades = self._client.get_trades(symbol)
            except BinanceError:
                continue
            for trade_time, _held_qty, _avg_cost, realized_delta in _cost_basis_walk(
                trades
            ):
                if trade_time >= today_start_ms:
                    total_realized += realized_delta

        return -total_realized if total_realized < 0 else Decimal("0")


__all__ = ["Position", "PositionsService"]
