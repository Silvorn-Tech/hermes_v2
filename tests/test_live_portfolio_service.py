"""Unit tests for the pure LIVE-bot Order calculations — no DB, no
Binance, no side effects. Mirrors test_simulation_portfolio_service.py's
shape, adapted for Order's actual fields (average_fill_price, no fee
column) — see live_portfolio_service.py's module docstring for why the
two aren't shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hermes_v2.trading.live_portfolio_service import (
    compute_live_realized_pnl_today,
    compute_live_trade_stats,
)
from hermes_v2.trading.models import OrderSide, OrderStatus


@dataclass
class _FakeOrder:
    """Duck-types the handful of Order fields these pure functions
    actually read, without needing a real ORM instance or a database."""

    side: OrderSide
    status: OrderStatus
    average_fill_price: Decimal | None
    executed_quantity: Decimal
    terminal_at: datetime | None


def _filled_buy(price: str, qty: str, terminal_at: datetime) -> _FakeOrder:
    return _FakeOrder(
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        average_fill_price=Decimal(price),
        executed_quantity=Decimal(qty),
        terminal_at=terminal_at,
    )


def _filled_sell(price: str, qty: str, terminal_at: datetime) -> _FakeOrder:
    return _FakeOrder(
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        average_fill_price=Decimal(price),
        executed_quantity=Decimal(qty),
        terminal_at=terminal_at,
    )


# --- compute_live_realized_pnl_today ----------------------------------------------


def test_compute_live_realized_pnl_today_pairs_a_round_trip() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("50000", "0.02", now.replace(hour=9)),
        _filled_sell("51000", "0.02", now.replace(hour=10)),
    ]
    # (51000*0.02) - (50000*0.02) = 1020 - 1000 = 20, no fees to subtract.
    assert compute_live_realized_pnl_today(orders, now=now) == Decimal("20")


def test_compute_live_realized_pnl_today_ignores_round_trips_closed_before_today() -> (
    None
):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    yesterday = now.replace(day=16)
    orders = [
        _filled_buy("50000", "0.02", yesterday.replace(hour=9)),
        _filled_sell("51000", "0.02", yesterday.replace(hour=10)),
    ]
    assert compute_live_realized_pnl_today(orders, now=now) == Decimal("0")


def test_compute_live_realized_pnl_today_ignores_an_unpaired_open_buy() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [_filled_buy("50000", "0.02", now.replace(hour=9))]
    assert compute_live_realized_pnl_today(orders, now=now) == Decimal("0")


def test_compute_live_realized_pnl_today_ignores_orders_with_no_fill_price() -> None:
    """A nominally-FILLED order with no average_fill_price (an edge case
    the nullable column allows) must never crash the pairing walk or be
    treated as a free round-trip leg."""
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    broken_buy = _filled_buy("50000", "0.02", now.replace(hour=9))
    broken_buy.average_fill_price = None
    orders = [broken_buy, _filled_sell("51000", "0.02", now.replace(hour=10))]
    assert compute_live_realized_pnl_today(orders, now=now) == Decimal("0")


def test_compute_live_realized_pnl_today_is_zero_for_empty_history() -> None:
    assert compute_live_realized_pnl_today([]) == Decimal("0")


# --- compute_live_trade_stats ------------------------------------------------------


def test_compute_live_trade_stats_no_round_trips_is_none_never_zero_percent() -> None:
    stats = compute_live_trade_stats([])
    assert stats.trade_count == 0
    assert stats.win_rate_pct is None


def test_compute_live_trade_stats_counts_round_trips_not_individual_fills() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("50000", "0.02", now.replace(hour=9)),
        _filled_sell("51000", "0.02", now.replace(hour=10)),
        _filled_buy("51000", "0.02", now.replace(hour=11)),
        _filled_sell("50000", "0.02", now.replace(hour=12)),
    ]
    stats = compute_live_trade_stats(orders)
    assert stats.trade_count == 2
    # One win (50k -> 51k), one loss (51k -> 50k): 50% win rate.
    assert stats.win_rate_pct == Decimal("50")


def test_compute_live_trade_stats_a_losing_round_trip_is_not_a_win() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("51000", "0.02", now.replace(hour=9)),
        _filled_sell("50000", "0.02", now.replace(hour=10)),
    ]
    stats = compute_live_trade_stats(orders)
    assert stats.trade_count == 1
    assert stats.win_rate_pct == Decimal("0")
