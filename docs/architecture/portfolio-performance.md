# Portfolio performance architecture (`feature/portfolio-performance-v1`)

`PortfolioService`'s own docstring used to state: *"Deliberately does not
compute a daily P&L figure: that needs a historical baseline... that
nothing in this codebase persists yet."* This phase builds exactly that
baseline — a persisted, periodic portfolio snapshot — and exposes it as a
real equity history behind `GET /portfolio/history`, replacing the
Dashboard's "not available yet" placeholder with a real chart.

```text
Binance
   |  BinanceClient.get_balances() + get_market_data() per asset
   v
PortfolioService.get_portfolio()          <- same call GET /portfolio already makes
   |
   v
portfolio_snapshot_service.take_portfolio_snapshot()
   |  INSERT ... ON CONFLICT (snapshot_at) DO NOTHING   <- idempotent, restart-safe
   v
portfolio_snapshots (PostgreSQL)
   |
   v
GET /portfolio/history?period=            <- require_permission("portfolio.read")
   |  downsample + compute_return_pct + compute_max_drawdown_pct
   v
Frontend: apiClient.getPortfolioHistory -> PerformanceCard (Dashboard)
```

## Snapshot model

Table `portfolio_snapshots` (migration `20260816_0004_portfolio_snapshots`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID (PK) | |
| `snapshot_at` | `timestamptz`, **unique**, indexed | Bucketed to the snapshot interval — the idempotency key and the chronological sort key |
| `quote_asset` | `varchar(10)` | e.g. `USDT` |
| `total_value_quote` | `numeric(28,10)` | Mark-to-market equity — the value the equity curve plots |
| `available_balance_quote` | `numeric(28,10)` | The quote asset's own `free` balance |
| `exposure_quote` | `numeric(28,10)` | `total_value_quote - quote_balance.value_quote` |
| `exposure_pct` | `numeric(6,3)` | `exposure_quote / total_value_quote * 100`, `0` if `total_value_quote` is `0` |
| `created_at` | `timestamptz`, server default `now()` | Literal insert time, distinct from the logical/bucketed `snapshot_at` |

**Realized and unrealized P&L are deliberately not captured.**
`PositionsService.get_realized_loss_today_quote()`'s own docstring already
states it is "today-only, currently-held-symbols-only... best-effort, not
an exact ledger" — unfit to persist as a permanent historical fact.
Unrealized P&L needs the same expensive per-asset trade-history walk
`PositionsService.get_positions()` does, which the data-source decision
below deliberately avoids running on every snapshot tick. Mark-to-market
equity (`total_value_quote`) is what actually drives an equity curve;
entry-price-relative P&L is a different, secondary metric that isn't
reliably computable today without that cost.

The table isn't bot-scoped: no `bot_id` column exists, since a 100%-`NULL`
column with no current meaning would just be unnecessary complexity. It's
named `portfolio_snapshots`, not `snapshots`, specifically so a future
bot-scoped sibling table can be added later without renaming this one.
Per-bot historical performance is explicitly out of scope for this phase.

## Data source: exactly one `PortfolioService.get_portfolio()` call

A snapshot calls `PortfolioService(client, quote_asset).get_portfolio()`
once and derives every column from its `balances` list — the same list
`GET /portfolio` already prices via one `get_balances()` call plus one
`get_market_data()` call per non-quote asset. `PositionsService` (which
separately re-fetches balances *and* walks `get_trades()` per held asset
for cost basis) is never called here: doing so would roughly double the
Binance cost of every snapshot, forever, for data this table doesn't need.
`available_balance_quote` and `exposure_quote`/`exposure_pct` are both
read directly off the balances list the same call already returns — the
quote asset (e.g. USDT) is itself one of the priced entries.

## Frequency: 15 minutes, in-process

**Chosen mechanism: an in-process daemon thread inside the existing
single-process `hermes_v2.runtime` — not Celery, Redis, Kubernetes, or a
new container.** `HermesRuntime.start()` already runs uvicorn in a daemon
thread coordinated by a `threading.Event` for shutdown
(`src/hermes_v2/runtime.py`); `PortfolioSnapshotScheduler` is a second
daemon thread of the exact same shape, sharing the same kind of stop
event. This requires zero deployment changes — no new systemd unit, no
new container — and works identically in `make run` and in ROMEO's
existing Docker Compose stack. There is no existing scheduler or worker
anywhere in this codebase or its deployment to hook into instead: the
only "timer" that exists is a host-level systemd timer that polls GHCR
for image digests and triggers a Compose redeploy — it lives outside this
repo and isn't something snapshot logic can run inside of.

**Interval: 15 minutes**, via `HERMES_PORTFOLIO_SNAPSHOT_INTERVAL_MINUTES`
(default `15`). Gives ~96 points/day — plenty for a smooth 1D chart —
without meaningfully taxing Binance (each snapshot costs the same handful
of calls `GET /portfolio` already costs today) or growing the table fast
(~35k rows/year, trivial for Postgres). The scheduler ticks once
immediately on start, then every interval.

Snapshotting runs regardless of `TRADING_ENABLED`: it's read-only
(`get_portfolio()`, no `OrderService` involvement), identical in kind to
`GET /portfolio`/`GET /positions`, which already work independently of
the kill switch. `TRADING_ENABLED` itself is untouched by this phase and
stays `false`.

## Restart / redeploy safety

`snapshot_at` is floored to the interval boundary
(`portfolio_snapshot_service.bucket_timestamp`, epoch-aligned so it's
deterministic regardless of when the process happens to start) and
enforced `UNIQUE`. Insertion goes through
`postgresql.insert(...).on_conflict_do_nothing(index_elements=["snapshot_at"])
.returning(...)`, and "did this insert actually happen" is read from the
`RETURNING` clause rather than the driver's reported row count —
psycopg3 was found (via `tests/test_portfolio_snapshot_service.py`) to
report `rowcount == -1` for a no-op `ON CONFLICT DO NOTHING`, which would
have silently defeated the duplicate check if trusted. A restart that
re-ticks within the same 15-minute window hits the same bucket and
no-ops on the second insert — no duplicate row, no special-cased restart
logic needed. A container recreation or redeploy loses nothing: the table
lives in Postgres, not in the process.

A Binance or database failure inside one tick is caught and logged, never
raised out of the scheduler loop (`run_snapshot_tick`) — a transient
failure skips that interval's snapshot and the loop simply continues at
the next tick; nothing else would ever restart the thread if it died.

## `GET /portfolio/history`

Requires authentication and the existing `portfolio.read` permission — the
same permission that already gates `GET /portfolio`/`GET /balances`; its
catalog description ("Read portfolio data") is already broad enough, and
nothing in this codebase distinguishes "current" from "historical"
portfolio reads.

```
GET /portfolio/history?period={1D|7D|30D|90D|1Y}
```

```json
{
  "period": "1D",
  "quote_asset": "USDT",
  "points": [{"t": "2026-08-16T12:00:00+00:00", "v": "12345.6700000000"}],
  "return_pct": "2.34",
  "max_drawdown_pct": "1.10"
}
```

An invalid `period` is rejected by FastAPI's own `Literal` validation
(422). An empty or single-point history returns `200` with `points: []`
(or one point) and `return_pct`/`max_drawdown_pct: null` — never an
error, never a fabricated value. The response carries only
`period`/`quote_asset`/`points`/`return_pct`/`max_drawdown_pct`; no
balance, exposure, or other portfolio detail is included.

**Downsampling** (`portfolio_snapshot_service.downsample`) keeps the last
real snapshot within each period's bucket — it never interpolates or
fabricates a point, so a gap in the underlying data (the process was
down) stays a visible gap in the chart rather than being smoothed over:

| Period | Window | Downsample bucket | ~points |
|---|---|---|---|
| `1D` | 24h | none (native 15-min resolution) | ~96 |
| `7D` | 7d | 60 min | ~168 |
| `30D` | 30d | 240 min (4h) | ~180 |
| `90D` | 90d | 720 min (12h) | ~180 |
| `1Y` | 365d | 4320 min (3d) | ~122 |

## Performance formulas

Both are pure functions in `portfolio_snapshot_service.py`, unit-tested in
isolation (`tests/test_portfolio_snapshot_service.py`).

**Return:**
```
return_pct = (last_value - first_value) / first_value * 100
```
`None` if there are fewer than 2 points, or if the first value is `0` (a
return relative to zero is undefined and is never reported as `0%` or any
other invented number).

**Max drawdown** (running-peak method):
```
peak = points[0]
max_drawdown = 0
for value in points:
    peak = max(peak, value)
    if peak > 0:
        drawdown = (peak - value) / peak * 100
        max_drawdown = max(max_drawdown, drawdown)
```
The largest decline from any prior peak, as a percentage of that peak.
`None` for an empty series or one whose peak is never positive.

## Frontend integration

`PerformanceCard` (`components/dashboard/PerformanceCard.tsx`) is
self-fetching: it calls `apiClient.getPortfolioHistory(period)` on mount
and whenever the period tab changes, with real `loading`/`success`/
`empty`/`error` states, using only existing visual primitives
(`SkeletonCard`, `ErrorState` with retry, `EmptyState`, `EquityChart`,
`Tabs`) — no new component, no design change.

The Dashboard's existing 4 period tabs (`7D`/`1M`/`3M`/`1Y`) are
unchanged. The backend implements the spec's full 5-period set
(`1D`/`7D`/`30D`/`90D`/`1Y`); the frontend maps its own tab keys onto the
backend's values at the fetch call site
(`services/api.ts`'s `BACKEND_PERIOD_BY_UI_PERIOD`): `7D→7D`, `1M→30D`,
`3M→90D`, `1Y→1Y`. The backend's `1D` period exists and is tested but has
no UI tab this phase — adding a 5th tab would be a visible design change,
which was explicitly out of scope.

`EquityCurve.winRatePct` no longer exists — there was no reliable
win/loss-classified trade ledger behind it, and showing one would have
been exactly the kind of fabricated number this phase avoids everywhere
else. `Portfolio.equityCurves` is removed from the `Portfolio` type
entirely: it was always `null` (no backend support existed), and
`PerformanceCard` is the only consumer, now fetching its own
period-parameterized resource rather than having history embedded in a
portfolio singleton.

## Retention

No pruning job exists. At ~35k rows/year, storage is trivial and adding a
cleanup mechanism wasn't requested; if retention becomes a concern later,
it can be added as a separate, explicit decision.

## Current limitations

- **No realized or unrealized P&L in the history** — see "Snapshot model"
  above; only mark-to-market total equity is tracked.
- **No per-bot historical performance** — snapshots are portfolio-wide
  only. The schema is deliberately unencumbered by an unused `bot_id`
  column so a bot-scoped sibling table can be added later without
  reworking this one.
- **No resolution finer than the snapshot interval (15 minutes by
  default)** — `1D` history is native resolution; there is no sub-interval
  interpolation, by design.
- **No data before this phase shipped** — history starts accumulating
  from the first snapshot tick after deployment; there is no backfill
  from Binance's own trade history.
