"""Pure calculations over the Simulation ledger — no Binance calls, no DB
session, no side effects. Kept separate from `SimulationOrderService`
(the orchestrator) the same way `portfolio_snapshot_service.py` separates
its RSS/AIC-style math from `portfolio_snapshot_scheduler.py`'s
orchestration: these functions are independently testable against
hand-built lists of `SimulationOrder` rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hermes_v2.trading.models import OrderSide, SimulationOrder, SimulationOrderStatus


def compute_position_value(current_quantity: Decimal, market_price: Decimal) -> Decimal:
    return current_quantity * market_price


def compute_total_value(
    cash_balance_quote: Decimal, current_quantity: Decimal, market_price: Decimal
) -> Decimal:
    return cash_balance_quote + compute_position_value(current_quantity, market_price)


def compute_return_pct(
    total_value_quote: Decimal, initial_capital_quote: Decimal
) -> Decimal | None:
    """`None` (never a fabricated 0%) if `initial_capital_quote` is 0 —
    a return relative to zero capital is undefined."""
    if initial_capital_quote == 0:
        return None
    return (total_value_quote - initial_capital_quote) / initial_capital_quote * 100


def compute_realized_pnl_today(
    orders: Sequence[SimulationOrder], now: datetime | None = None
) -> Decimal:
    """Walks FILLED orders chronologically, pairing each SELL with its
    immediately preceding BUY, to compute realized P&L per closed
    round-trip. This pairing is valid *because* Pause/Resume always trade
    a bot's FULL position (never a partial quantity — see
    `BotPosition`'s "one instrument per bot" docstring), so a SELL always
    closes out exactly the most recent BUY, never an ambiguous partial
    lot. Returns the **signed** sum of round-trips closed **today**
    (positive = net profit, negative = net loss) — never a cumulative/
    all-time figure. Orders with no `terminal_at` (never reached FILLED)
    are ignored.
    """
    now = now or datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    filled = sorted(
        (o for o in orders if o.status == SimulationOrderStatus.FILLED and o.terminal_at),
        key=lambda o: o.terminal_at,
    )

    realized_today = Decimal("0")
    open_buy: SimulationOrder | None = None
    for order in filled:
        if order.side == OrderSide.BUY:
            open_buy = order
        elif order.side == OrderSide.SELL and open_buy is not None:
            buy_cost = (
                Decimal(open_buy.fill_price) * Decimal(open_buy.executed_quantity)
                + Decimal(open_buy.fee_quote)
            )
            sell_proceeds = (
                Decimal(order.fill_price) * Decimal(order.executed_quantity)
                - Decimal(order.fee_quote)
            )
            pnl = sell_proceeds - buy_cost
            if order.terminal_at >= today_start:
                realized_today += pnl
            open_buy = None

    return realized_today


@dataclass(frozen=True)
class TradeStats:
    trade_count: int
    win_rate_pct: Decimal | None  # None if there are zero closed round-trips to judge


def compute_trade_stats(orders: Sequence[SimulationOrder]) -> TradeStats:
    """`trade_count` counts closed round-trips (a BUY followed by its
    closing SELL), not individual fills — matching how a human would
    describe "how many trades has this bot made." `win_rate_pct` is
    `None` (never a fabricated 0%) when there are no closed round-trips
    yet to classify."""
    filled = sorted(
        (o for o in orders if o.status == SimulationOrderStatus.FILLED and o.terminal_at),
        key=lambda o: o.terminal_at,
    )

    round_trips = 0
    wins = 0
    open_buy: SimulationOrder | None = None
    for order in filled:
        if order.side == OrderSide.BUY:
            open_buy = order
        elif order.side == OrderSide.SELL and open_buy is not None:
            buy_cost = (
                Decimal(open_buy.fill_price) * Decimal(open_buy.executed_quantity)
                + Decimal(open_buy.fee_quote)
            )
            sell_proceeds = (
                Decimal(order.fill_price) * Decimal(order.executed_quantity)
                - Decimal(order.fee_quote)
            )
            round_trips += 1
            if sell_proceeds > buy_cost:
                wins += 1
            open_buy = None

    if round_trips == 0:
        return TradeStats(trade_count=0, win_rate_pct=None)
    return TradeStats(
        trade_count=round_trips, win_rate_pct=Decimal(wins) / Decimal(round_trips) * 100
    )


__all__ = [
    "TradeStats",
    "compute_position_value",
    "compute_realized_pnl_today",
    "compute_return_pct",
    "compute_total_value",
    "compute_trade_stats",
]
