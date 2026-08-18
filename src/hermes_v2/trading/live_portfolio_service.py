"""Pure calculations over a LIVE bot's real Order history — the LIVE
counterpart to `simulation_portfolio_service.py`'s math, operating on
real `Order` rows (filtered by `bot_id`) instead of `SimulationOrder`
rows. Deliberately not shared with `simulation_portfolio_service.py`'s
functions: `Order` has no `fee_quote` column (unlike `SimulationOrder`)
and uses `average_fill_price` where `SimulationOrder` uses `fill_price`
— a shared implementation would need to paper over that with duck-typing
or a translation layer, which is worse than the small,
independently-testable duplication this module accepts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hermes_v2.trading.models import Order, OrderSide, OrderStatus


def compute_live_realized_pnl_today(
    orders: Sequence[Order], now: datetime | None = None
) -> Decimal:
    """Same round-trip pairing algorithm as
    `simulation_portfolio_service.compute_realized_pnl_today`, valid for
    the same reason: Pause/Resume always trade a bot's FULL position (see
    `BotPosition`'s "one instrument per bot" docstring), so a SELL always
    closes exactly the most recent BUY. No fee subtraction — `Order` has
    no fee column in this phase. Orders with no `terminal_at` or no
    `average_fill_price` (never reached a real fill) are ignored."""
    now = now or datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    filled = sorted(
        (
            o
            for o in orders
            if o.status == OrderStatus.FILLED
            and o.terminal_at
            and o.average_fill_price is not None
        ),
        key=lambda o: o.terminal_at,
    )

    realized_today = Decimal("0")
    open_buy: Order | None = None
    for order in filled:
        if order.side == OrderSide.BUY:
            open_buy = order
        elif order.side == OrderSide.SELL and open_buy is not None:
            buy_cost = Decimal(open_buy.average_fill_price) * Decimal(
                open_buy.executed_quantity
            )
            sell_proceeds = Decimal(order.average_fill_price) * Decimal(
                order.executed_quantity
            )
            pnl = sell_proceeds - buy_cost
            if order.terminal_at >= today_start:
                realized_today += pnl
            open_buy = None

    return realized_today


@dataclass(frozen=True)
class LiveTradeStats:
    trade_count: int
    win_rate_pct: Decimal | None  # None if there are zero closed round-trips to judge


def compute_live_trade_stats(orders: Sequence[Order]) -> LiveTradeStats:
    """`trade_count` counts closed round-trips (a BUY followed by its
    closing SELL), not individual fills. `win_rate_pct` is `None` (never
    a fabricated 0%) when there are no closed round-trips yet to
    classify."""
    filled = sorted(
        (
            o
            for o in orders
            if o.status == OrderStatus.FILLED
            and o.terminal_at
            and o.average_fill_price is not None
        ),
        key=lambda o: o.terminal_at,
    )

    round_trips = 0
    wins = 0
    open_buy: Order | None = None
    for order in filled:
        if order.side == OrderSide.BUY:
            open_buy = order
        elif order.side == OrderSide.SELL and open_buy is not None:
            buy_cost = Decimal(open_buy.average_fill_price) * Decimal(
                open_buy.executed_quantity
            )
            sell_proceeds = Decimal(order.average_fill_price) * Decimal(
                order.executed_quantity
            )
            round_trips += 1
            if sell_proceeds > buy_cost:
                wins += 1
            open_buy = None

    if round_trips == 0:
        return LiveTradeStats(trade_count=0, win_rate_pct=None)
    return LiveTradeStats(
        trade_count=round_trips, win_rate_pct=Decimal(wins) / Decimal(round_trips) * 100
    )


__all__ = [
    "LiveTradeStats",
    "compute_live_realized_pnl_today",
    "compute_live_trade_stats",
]
