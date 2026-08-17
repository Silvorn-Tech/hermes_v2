"""Tests for portfolio_snapshot_service — snapshot creation, restart-safe
idempotency, and the pure equity-curve calculations.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.trading.models import PortfolioSnapshot
from hermes_v2.trading.portfolio_snapshot_service import (
    bucket_timestamp,
    compute_max_drawdown_pct,
    compute_return_pct,
    downsample,
    take_portfolio_snapshot,
)

pytestmark = pytest.mark.database


class _FakeBinanceClient:
    def __init__(self) -> None:
        self.market_data: dict[str, dict] = {"BTCUSDT": {"last_price": "50000"}}
        self.balances: list[dict] = [
            {"asset": "USDT", "free": "1000", "locked": "500"},
            {"asset": "BTC", "free": "0.02", "locked": "0"},
        ]
        self.get_balances_calls = 0
        self.get_market_data_calls: list[str] = []

    def get_balances(self) -> list[dict]:
        self.get_balances_calls += 1
        return self.balances

    def get_market_data(self, symbol: str) -> dict:
        self.get_market_data_calls.append(symbol)
        return self.market_data[symbol]


class _FailingSession:
    """Wraps a real session but raises on flush, simulating a DB failure
    after the Binance call already succeeded."""

    def __init__(self, real_session: Session) -> None:
        self._real = real_session

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def flush(self, *args, **kwargs):
        raise RuntimeError("simulated DB failure")

    def scalar(self, *args, **kwargs):
        return self._real.scalar(*args, **kwargs)

    def rollback(self):
        self._real.rollback()


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE portfolio_snapshots"))

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


# --- bucket_timestamp ---------------------------------------------------------


def test_bucket_timestamp_floors_to_interval_boundary() -> None:
    now = datetime(2026, 8, 16, 10, 37, 42, tzinfo=UTC)
    assert bucket_timestamp(now, 15) == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


def test_bucket_timestamp_is_epoch_aligned_not_process_start_aligned() -> None:
    # Two different "now" values in the same 15-minute wall-clock window
    # must bucket identically regardless of when the process happened to
    # start ticking.
    a = bucket_timestamp(datetime(2026, 8, 16, 10, 30, 1, tzinfo=UTC), 15)
    b = bucket_timestamp(datetime(2026, 8, 16, 10, 44, 59, tzinfo=UTC), 15)
    assert a == b == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


# --- take_portfolio_snapshot ---------------------------------------------------


def test_creates_a_snapshot_using_only_get_portfolio(session: Session) -> None:
    client = _FakeBinanceClient()
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    snapshot = take_portfolio_snapshot(session, client, interval_minutes=15, now=now)
    session.commit()

    assert snapshot is not None
    assert snapshot.snapshot_at == datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert snapshot.quote_asset == "USDT"
    # 1500 USDT (free+locked) + 0.02 BTC * 50000 = 1500 + 1000 = 2500
    assert snapshot.total_value_quote == Decimal("2500")
    assert snapshot.available_balance_quote == Decimal("1000")  # USDT free only
    assert snapshot.exposure_quote == Decimal("1000")  # BTC value only
    assert snapshot.exposure_pct == Decimal("40.000")  # 1000/2500 * 100

    # Never PositionsService's shape: exactly one get_balances call, one
    # get_market_data call per non-quote asset (BTC), nothing else.
    assert client.get_balances_calls == 1
    assert client.get_market_data_calls == ["BTCUSDT"]


def test_persists_across_a_fresh_session(session: Session) -> None:
    client = _FakeBinanceClient()
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    take_portfolio_snapshot(session, client, interval_minutes=15, now=now)
    session.commit()

    row = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.snapshot_at == datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        )
    )
    assert row is not None
    assert row.total_value_quote == Decimal("2500")


def test_duplicate_within_the_same_bucket_is_skipped_not_duplicated(
    session: Session,
) -> None:
    client = _FakeBinanceClient()
    now = datetime(2026, 8, 16, 12, 3, tzinfo=UTC)  # same 12:00 bucket as below

    first = take_portfolio_snapshot(session, client, interval_minutes=15, now=now)
    session.commit()
    second = take_portfolio_snapshot(
        session,
        client,
        interval_minutes=15,
        now=datetime(2026, 8, 16, 12, 9, tzinfo=UTC),
    )
    session.commit()

    assert first is not None
    assert second is None  # same bucket -> skipped, not a second row
    rows = session.scalars(select(PortfolioSnapshot)).all()
    assert len(rows) == 1


def test_restart_safe_a_fresh_call_after_restart_still_no_ops(session: Session) -> None:
    """Simulates a process restart: a brand-new take_portfolio_snapshot
    call (as if from a freshly started scheduler) against the same
    bucket must not create a duplicate."""
    client = _FakeBinanceClient()
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    take_portfolio_snapshot(session, client, interval_minutes=15, now=now)
    session.commit()

    # "Restart": a fresh client instance, same bucket.
    restarted_client = _FakeBinanceClient()
    result = take_portfolio_snapshot(
        session, restarted_client, interval_minutes=15, now=now
    )
    session.commit()

    assert result is None
    rows = session.scalars(select(PortfolioSnapshot)).all()
    assert len(rows) == 1


def test_chronological_ordering(session: Session) -> None:
    client = _FakeBinanceClient()
    times = [
        datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 16, 12, 30, tzinfo=UTC),
        datetime(2026, 8, 16, 12, 15, tzinfo=UTC),  # inserted out of order
    ]
    for t in times:
        take_portfolio_snapshot(session, client, interval_minutes=15, now=t)
        session.commit()

    rows = session.scalars(
        select(PortfolioSnapshot).order_by(PortfolioSnapshot.snapshot_at.asc())
    ).all()
    assert [r.snapshot_at for r in rows] == sorted(times)


def test_db_failure_propagates_cleanly_no_partial_row(session: Session) -> None:
    client = _FakeBinanceClient()
    failing_session = _FailingSession(session)

    with pytest.raises(RuntimeError, match="simulated DB failure"):
        take_portfolio_snapshot(failing_session, client, interval_minutes=15)

    session.rollback()
    assert session.scalars(select(PortfolioSnapshot)).all() == []


def test_zero_quote_balance_gives_full_exposure(session: Session) -> None:
    client = _FakeBinanceClient()
    client.balances = [
        {"asset": "BTC", "free": "0.02", "locked": "0"}
    ]  # no USDT at all
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    snapshot = take_portfolio_snapshot(session, client, interval_minutes=15, now=now)
    session.commit()

    assert snapshot is not None
    assert snapshot.available_balance_quote == Decimal("0")
    assert snapshot.exposure_pct == Decimal("100.000")


# --- compute_return_pct --------------------------------------------------------


def test_return_pct_monotonic_up() -> None:
    assert compute_return_pct([Decimal("100"), Decimal("150")]) == Decimal("50")


def test_return_pct_monotonic_down() -> None:
    assert compute_return_pct([Decimal("100"), Decimal("80")]) == Decimal("-20")


def test_return_pct_none_for_fewer_than_two_points() -> None:
    assert compute_return_pct([]) is None
    assert compute_return_pct([Decimal("100")]) is None


def test_return_pct_none_when_first_value_is_zero() -> None:
    assert compute_return_pct([Decimal("0"), Decimal("100")]) is None


def test_return_pct_flat_series_is_zero() -> None:
    assert compute_return_pct(
        [Decimal("100"), Decimal("100"), Decimal("100")]
    ) == Decimal("0")


# --- compute_max_drawdown_pct ---------------------------------------------------


def test_drawdown_v_shaped_series() -> None:
    points = [Decimal("100"), Decimal("50"), Decimal("120")]
    assert compute_max_drawdown_pct(points) == Decimal("50")  # (100-50)/100*100


def test_drawdown_monotonic_up_is_zero() -> None:
    points = [Decimal("100"), Decimal("110"), Decimal("120")]
    assert compute_max_drawdown_pct(points) == Decimal("0")


def test_drawdown_flat_series_is_zero() -> None:
    assert compute_max_drawdown_pct([Decimal("100"), Decimal("100")]) == Decimal("0")


def test_drawdown_empty_series_is_none() -> None:
    assert compute_max_drawdown_pct([]) is None


def test_drawdown_single_point_is_zero() -> None:
    assert compute_max_drawdown_pct([Decimal("100")]) == Decimal("0")


def test_drawdown_none_when_peak_never_positive() -> None:
    assert compute_max_drawdown_pct([Decimal("0"), Decimal("0")]) is None


def test_drawdown_takes_the_deepest_decline_across_multiple_dips() -> None:
    points = [Decimal("100"), Decimal("90"), Decimal("130"), Decimal("60")]
    # peak after first dip stays 100 (drawdown 10%), then peak becomes 130
    # (drawdown to 60 is (130-60)/130*100 ~= 53.85%) -- the larger one wins.
    result = compute_max_drawdown_pct(points)
    assert result == (Decimal("130") - Decimal("60")) / Decimal("130") * 100


# --- downsample -----------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, snapshot_at: datetime, total_value_quote: Decimal) -> None:
        self.snapshot_at = snapshot_at
        self.total_value_quote = total_value_quote


def test_downsample_keeps_last_point_per_bucket() -> None:
    snapshots = [
        _FakeSnapshot(datetime(2026, 8, 16, 10, 0, tzinfo=UTC), Decimal("1")),
        _FakeSnapshot(datetime(2026, 8, 16, 10, 15, tzinfo=UTC), Decimal("2")),
        _FakeSnapshot(datetime(2026, 8, 16, 10, 45, tzinfo=UTC), Decimal("3")),
    ]
    result = downsample(snapshots, 60)  # 1-hour buckets
    assert len(result) == 1
    assert result[0].total_value_quote == Decimal("3")  # last one in the hour


def test_downsample_zero_bucket_minutes_is_a_no_op() -> None:
    snapshots = [_FakeSnapshot(datetime(2026, 8, 16, 10, 0, tzinfo=UTC), Decimal("1"))]
    assert downsample(snapshots, 0) == snapshots


def test_downsample_never_fabricates_a_point_for_an_empty_bucket() -> None:
    # A gap of 2 hours between two 1-hour-bucketed points -- the gap
    # bucket must simply be absent, never interpolated.
    snapshots = [
        _FakeSnapshot(datetime(2026, 8, 16, 8, 0, tzinfo=UTC), Decimal("1")),
        _FakeSnapshot(datetime(2026, 8, 16, 11, 0, tzinfo=UTC), Decimal("2")),
    ]
    result = downsample(snapshots, 60)
    assert len(result) == 2  # exactly the 2 real points, no gap-filled 9:00/10:00
    assert [r.total_value_quote for r in result] == [Decimal("1"), Decimal("2")]
