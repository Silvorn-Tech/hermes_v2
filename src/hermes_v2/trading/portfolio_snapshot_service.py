"""Portfolio snapshot creation and the pure equity-curve calculations
built on top of it.

**Why only `total_value_quote`/`available_balance_quote`/`exposure_*`,
never P&L**: a snapshot calls `PortfolioService.get_portfolio()` exactly
once — the same call `GET /portfolio` already makes — and derives
everything from its `balances` list, with zero additional Binance calls.
`PositionsService` (which computes per-position entry price and
unrealized P&L) is deliberately never called here: it re-fetches
balances *and* walks `get_trades()` per held asset, roughly doubling the
Binance cost of every tick forever for data this table doesn't need.
Realized P&L is even less reusable — `PositionsService.
get_realized_loss_today_quote()`'s own docstring already states it's
"today-only, currently-held-symbols-only... best-effort, not an exact
ledger," unfit to persist as a permanent historical fact. Mark-to-market
equity (`total_value_quote`) is what actually drives an equity curve;
entry-price-relative P&L would need real trade-history pagination this
codebase doesn't have yet.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient
from hermes_v2.trading.models import PortfolioSnapshot
from hermes_v2.trading.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_timestamp(now: datetime, interval_minutes: int) -> datetime:
    """Floor `now` to the nearest `interval_minutes` boundary, aligned to
    the Unix epoch (00:00 UTC) — deterministic regardless of when the
    process happens to start, and works for any interval, not just
    divisors of 60."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC).replace(second=0, microsecond=0)
    total_minutes = int((now - _EPOCH).total_seconds() // 60)
    bucket_minutes = (total_minutes // interval_minutes) * interval_minutes
    return _EPOCH + timedelta(minutes=bucket_minutes)


def take_portfolio_snapshot(
    session: Session,
    client: BinanceClient,
    *,
    quote_asset: str = "USDT",
    interval_minutes: int,
    now: datetime | None = None,
) -> PortfolioSnapshot | None:
    """Create one snapshot row, or return `None` if this interval's bucket
    already has one (restart-safe: `ON CONFLICT DO NOTHING` against the
    unique `snapshot_at`, not a Python-side check-then-insert race)."""
    quote_asset = quote_asset.upper()
    portfolio = PortfolioService(client, quote_asset=quote_asset).get_portfolio()
    snapshot_at = bucket_timestamp(now or datetime.now(UTC), interval_minutes)

    total_value_quote: Decimal = portfolio["total_value_quote"]
    quote_balance = next(
        (b for b in portfolio["balances"] if b["asset"] == quote_asset), None
    )
    available_balance_quote = quote_balance["free"] if quote_balance else Decimal("0")
    quote_value = (
        quote_balance["value_quote"]
        if quote_balance and quote_balance["value_quote"]
        else Decimal("0")
    )
    exposure_quote = total_value_quote - quote_value
    exposure_pct = (
        (exposure_quote / total_value_quote * 100)
        if total_value_quote > 0
        else Decimal("0")
    )

    stmt = (
        pg_insert(PortfolioSnapshot.__table__)
        .values(
            id=uuid.uuid4(),
            snapshot_at=snapshot_at,
            quote_asset=quote_asset,
            total_value_quote=total_value_quote,
            available_balance_quote=available_balance_quote,
            exposure_quote=exposure_quote,
            exposure_pct=exposure_pct,
        )
        .on_conflict_do_nothing(index_elements=["snapshot_at"])
    )
    result = session.execute(stmt)
    session.flush()

    if result.rowcount == 0:
        return None
    return session.scalar(
        select(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_at == snapshot_at)
    )


def compute_return_pct(points: list[Decimal]) -> Decimal | None:
    """`(last - first) / first * 100`. `None` if fewer than 2 points or
    the first value is 0 (a return relative to zero is undefined, never
    reported as 0% or any other invented number)."""
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    if first == 0:
        return None
    return (last - first) / first * 100


def compute_max_drawdown_pct(points: list[Decimal]) -> Decimal | None:
    """Running-peak drawdown: at each point, the decline from the highest
    value seen so far; the result is the largest such decline over the
    whole series, as a percentage of that peak. `None` for an empty
    series or one whose peak is never positive (a percentage decline from
    a zero or negative baseline is undefined)."""
    if not points:
        return None
    peak = points[0]
    max_drawdown = Decimal("0")
    peak_was_positive = False
    for value in points:
        if value > peak:
            peak = value
        if peak > 0:
            peak_was_positive = True
            drawdown = (peak - value) / peak * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    return max_drawdown if peak_was_positive else None


def downsample(
    snapshots: list[PortfolioSnapshot], bucket_minutes: int
) -> list[PortfolioSnapshot]:
    """Keep the last snapshot within each `bucket_minutes` window —
    never interpolates or fabricates a point. `snapshots` must already be
    ordered by `snapshot_at` ascending. A gap in the underlying data (the
    process was down) stays a gap; it is never filled in."""
    if bucket_minutes <= 0:
        return snapshots
    buckets: dict[datetime, PortfolioSnapshot] = {}
    for snapshot in snapshots:
        bucket_key = bucket_timestamp(snapshot.snapshot_at, bucket_minutes)
        buckets[bucket_key] = snapshot  # later (more recent) wins per bucket
    return [buckets[key] for key in sorted(buckets)]


__all__ = [
    "bucket_timestamp",
    "compute_max_drawdown_pct",
    "compute_return_pct",
    "downsample",
    "take_portfolio_snapshot",
]
