"""Unit tests for the pure Simulation ledger calculations — no DB, no
Binance, no side effects. Mirrors how portfolio_snapshot_service.py's own
compute_return_pct/compute_max_drawdown_pct are unit-tested independently
of the scheduler that calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hermes_v2.trading.models import OrderSide, SimulationOrderStatus
from hermes_v2.trading.simulation_portfolio_service import (
    compute_position_value,
    compute_realized_pnl_today,
    compute_return_pct,
    compute_total_value,
    compute_trade_stats,
)


@dataclass
class _FakeOrder:
    """Duck-types the handful of SimulationOrder fields these pure
    functions actually read, without needing a real ORM instance or a
    database."""

    side: OrderSide
    status: SimulationOrderStatus
    fill_price: Decimal | None
    executed_quantity: Decimal
    fee_quote: Decimal
    terminal_at: datetime | None


def _filled_buy(
    price: str, qty: str, terminal_at: datetime, fee: str = "0"
) -> _FakeOrder:
    return _FakeOrder(
        side=OrderSide.BUY,
        status=SimulationOrderStatus.FILLED,
        fill_price=Decimal(price),
        executed_quantity=Decimal(qty),
        fee_quote=Decimal(fee),
        terminal_at=terminal_at,
    )


def _filled_sell(
    price: str, qty: str, terminal_at: datetime, fee: str = "0"
) -> _FakeOrder:
    return _FakeOrder(
        side=OrderSide.SELL,
        status=SimulationOrderStatus.FILLED,
        fill_price=Decimal(price),
        executed_quantity=Decimal(qty),
        fee_quote=Decimal(fee),
        terminal_at=terminal_at,
    )


# --- compute_position_value / compute_total_value ------------------------------


def test_compute_position_value_multiplies_quantity_by_price() -> None:
    assert compute_position_value(Decimal("0.02"), Decimal("50000")) == Decimal("1000")


def test_compute_position_value_is_zero_with_no_position() -> None:
    assert compute_position_value(Decimal("0"), Decimal("50000")) == Decimal("0")


def test_compute_total_value_sums_cash_and_position_value() -> None:
    total = compute_total_value(Decimal("9000"), Decimal("0.02"), Decimal("50000"))
    assert total == Decimal("10000")


# --- compute_return_pct -----------------------------------------------------------


def test_compute_return_pct_positive() -> None:
    assert compute_return_pct(Decimal("11000"), Decimal("10000")) == Decimal("10")


def test_compute_return_pct_negative() -> None:
    assert compute_return_pct(Decimal("9000"), Decimal("10000")) == Decimal("-10")


def test_compute_return_pct_is_none_never_zero_for_zero_initial_capital() -> None:
    assert compute_return_pct(Decimal("100"), Decimal("0")) is None


# --- compute_realized_pnl_today ---------------------------------------------------


def test_compute_realized_pnl_today_pairs_a_round_trip() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("50000", "0.02", now.replace(hour=9)),
        _filled_sell("51000", "0.02", now.replace(hour=10)),
    ]
    # (51000*0.02) - (50000*0.02) = 1020 - 1000 = 20
    assert compute_realized_pnl_today(orders, now=now) == Decimal("20")


def test_compute_realized_pnl_today_subtracts_fees_from_both_legs() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("50000", "0.02", now.replace(hour=9), fee="1"),
        _filled_sell("51000", "0.02", now.replace(hour=10), fee="1"),
    ]
    # 20 profit minus 1 buy fee minus 1 sell fee = 18
    assert compute_realized_pnl_today(orders, now=now) == Decimal("18")


def test_compute_realized_pnl_today_ignores_round_trips_closed_before_today() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    yesterday = now.replace(day=16)
    orders = [
        _filled_buy("50000", "0.02", yesterday.replace(hour=9)),
        _filled_sell("51000", "0.02", yesterday.replace(hour=10)),
    ]
    assert compute_realized_pnl_today(orders, now=now) == Decimal("0")


def test_compute_realized_pnl_today_ignores_an_unpaired_open_buy() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [_filled_buy("50000", "0.02", now.replace(hour=9))]
    assert compute_realized_pnl_today(orders, now=now) == Decimal("0")


def test_compute_realized_pnl_today_never_a_fabricated_value_from_empty_history() -> (
    None
):
    assert compute_realized_pnl_today([]) == Decimal("0")


# --- compute_trade_stats ----------------------------------------------------------


def test_compute_trade_stats_no_round_trips_is_none_never_zero_percent() -> None:
    stats = compute_trade_stats([])
    assert stats.trade_count == 0
    assert stats.win_rate_pct is None


def test_compute_trade_stats_counts_round_trips_not_individual_fills() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("50000", "0.02", now.replace(hour=9)),
        _filled_sell("51000", "0.02", now.replace(hour=10)),
        _filled_buy("51000", "0.02", now.replace(hour=11)),
        _filled_sell("50000", "0.02", now.replace(hour=12)),
    ]
    stats = compute_trade_stats(orders)
    assert stats.trade_count == 2
    # One win (50k -> 51k), one loss (51k -> 50k): 50% win rate.
    assert stats.win_rate_pct == Decimal("50")


def test_compute_trade_stats_a_losing_round_trip_is_not_a_win() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    orders = [
        _filled_buy("51000", "0.02", now.replace(hour=9)),
        _filled_sell("50000", "0.02", now.replace(hour=10)),
    ]
    stats = compute_trade_stats(orders)
    assert stats.trade_count == 1
    assert stats.win_rate_pct == Decimal("0")
