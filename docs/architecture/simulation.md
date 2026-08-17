# Paper trading / Simulation Mode architecture (`feature/simulation-mode-v1`)

Before any Strategy Engine or Model Selection output ever reaches a bot,
Hermes needs a way to watch a bot trade for days or weeks against real
market data with **zero possibility of a real fill**, using the exact
same decision pipeline (`OrderValidator` → `RiskEngine`) a live bot will
eventually use. This phase adds that: `Bot.execution_mode`
(`SIMULATION`/`LIVE`), a persistent virtual ledger, and a parallel
execution path that structurally cannot reach Binance's write endpoints.

```text
Bot.execution_mode == SIMULATION                Bot.execution_mode == LIVE
        |                                                |
        v                                                v
SimulationOrderService.place_bot_order()        OrderService.create_order()
        |  reserve() -> kill switch check                |  (untouched, existing path)
        |  -> OrderValidator.validate()                   |
        |  -> RiskEngine.validate_order()                 |
        |     (snapshot built from the bot's own          |
        |      SimulationAccount, not the real account)   |
        |  -> get_market_data() (READ ONLY)                |
        |  -> apply configured slippage/fee                |
        |  -> write simulation_orders (FILLED/REJECTED)     v
        |  -> update simulation_accounts.cash_balance   BinanceClient.create_order()/
        |  -> update BotPosition.current_quantity        cancel_order() -- real money
        v
simulation_accounts / simulation_orders / simulation_snapshots (PostgreSQL)
        |
        v
GET /bots/{id}/portfolio, GET /bots/{id}/performance
```

## Simulation vs Live

Every `Bot` has an `execution_mode`: `SIMULATION` or `LIVE`
(`BotExecutionMode`, `src/hermes_v2/trading/models/bot.py`). **Every bot
created in this phase is `SIMULATION`** — `POST /bots`'s request schema
doesn't even accept an `execution_mode` field, so there is no way for a
client to request `LIVE`, automatic or otherwise. The migration
(`20260817_0001_simulation_mode.py`) also backfills every **pre-existing**
bot to `SIMULATION`, the safest possible default: the moment this
migration lands, no bot anywhere can place a real order until a future
phase adds an explicit activation path. `execution_mode` is set once at
creation and has no update path in v1 — there is no "Activate LIVE"
endpoint yet (see "What a LIVE-activation phase would need" below).

`OrderService` — the existing, real-money-critical, already-proven path
to `BinanceClient.create_order()`/`cancel_order()` — is **completely
untouched** by this phase. It remains the only path for manual orders
placed via `POST /orders`, and will remain the path for `LIVE` bots once
that activation phase exists. Nothing about it changed; it isn't even
imported by the new Simulation code except for one shared exception
class (`TradingDisabledError`).

## Why `SimulationOrderService`, not a flag on `OrderService`

Two options existed for isolating Simulation:

1. Add an `execution_mode` discriminator to the real `orders`/
   `order_events` tables and branch inside `OrderService`.
2. Give Simulation its own tables and a new, small orchestrator that
   mirrors `OrderService`'s pipeline shape but never touches
   `BinanceClient.create_order`/`cancel_order`.

**Option 2 was chosen.** It makes "simulation data leaking into a real
view" a schema-level impossibility — combining `simulation_orders` and
`orders` would require an explicit `UNION`, not a `WHERE` clause someone
could forget to add. It also means `OrderService` — the module with the
most to lose from a subtle regression — needs zero changes, and the
isolation claim can be **proven**, not just documented (see "Isolation
guarantees" below).

`SimulationOrderService` mirrors `OrderService`'s pipeline *sequence*
exactly, reusing three components **directly, unmodified**:
`OrderValidator.validate()`, `RiskEngine.validate_order()` (same class;
its `RiskLimits` come from each user's own per-user Simulation settings,
`get_user_simulation_risk_limits()` — see
`docs/architecture/multi-tenant-trading.md` #2), and
`hermes_v2.trading.idempotency.reserve()/finalize()`. Nothing about how
an order is validated, risk-checked, or deduplicated is reimplemented.
The only things that genuinely differ — because they're what Simulation
Mode is *for* — are **execution** (a virtual fill against real market
data instead of a Binance write) and **portfolio accounting** (the bot's
own `SimulationAccount` instead of the real account via
`PortfolioService`/`PositionsService`).

## The virtual ledger schema

Three new tables (migration `20260817_0001_simulation_mode.py`), all
foreign-keyed only to `bots.id` — never to `orders.id`, and nothing in
`orders` references them either:

**`simulation_accounts`** — one row per Simulation bot (`bot_id` unique,
same one-row-per-bot convention as `bot_positions`).

| Column | Notes |
|---|---|
| `initial_capital_quote` | Frozen at creation time from `default_simulation_initial_capital()`; never re-read from config afterward, even if the env var changes later |
| `cash_balance_quote` | Current virtual cash, mutated by every fill |
| `quote_asset` | e.g. `USDT` |

**`simulation_orders`** — one row per simulated order attempt. No
`NEW`/`PARTIALLY_FILLED` states exist (`SimulationOrderStatus` is only
`FILLED`/`REJECTED`/`FAILED`) because a simulated MARKET order always
resolves fully and synchronously — there's no real matching engine to
introduce latency or partial fills to model. `side`/`order_type` reuse
`OrderSide`/`OrderType` as Python enum classes (the same values, no
duplicated business meaning) but map to their own Postgres enum types
(`simulation_order_side`/`simulation_order_type`), so this table's schema
never depends on the real `order_side`/`order_type` types' lifecycle.
Carries `requested_quantity`, `fill_price` (null unless `FILLED`),
`executed_quantity`, `fee_quote`, `slippage_quote`, `reason` (for
`REJECTED`/`FAILED`), `created_at`, `terminal_at`.

**`simulation_snapshots`** — periodic per-bot snapshot of total virtual
portfolio value. Unique on `(bot_id, snapshot_at)`. This is the one
metric — drawdown — that genuinely needs a time series rather than a
live computation from current state; everything else (current portfolio
value, return %, realized P&L, trade count, win rate) is computed
directly from `SimulationAccount` + `SimulationOrder` history with no
snapshot needed.

No `simulation_fills`/`simulation_order_events` tables exist: since a v1
simulated order always resolves in one step, one row per order already
captures the whole "order → fill" concept. Event/audit trail for
Simulation activity goes through the existing, generic `AuditLogEntry`
table (action `bot.simulation_order`), not a new table.

`BotPosition` is **reused as-is** — no "simulation position" table.
It already tracks `current_quantity`/`target_quantity` per bot with
zero Binance-specific structure; a simulated fill updates it exactly the
way a real fill does today.

## Execution routing

`BotService._transition()` (the one method Pause/Resume share) branches
on `bot.execution_mode`:

```python
if bot.execution_mode == BotExecutionMode.SIMULATION:
    result = SimulationOrderService(session, client, ...).place_bot_order(...)
else:
    result = OrderService(session, client, ...).create_order(...)
```

Both return the same `{"order"|"bot": ..., "status": "FILLED"|"REJECTED"
|"FAILED"|..., "reason": ...}` shape `_transition()` already knows how to
interpret — `REJECTED` reverts to the prior status, `FILLED` advances the
bot, anything else lands it in `ERROR` for manual review. This decision
table, `target_quantity` semantics, and the "never use `close_position()`
for pause" rule are **unchanged**: `SimulationOrderService` receives an
explicit quantity from `BotPosition` exactly like `OrderService` does —
`current_quantity` for pause (sell the bot's own holding, never the
account-wide Binance balance for that symbol), `target_quantity` for
resume.

One real implementation detail the schema forces:
`BotPosition.last_close_order_id`/`last_open_order_id` are foreign keys
into the real `orders` table. A `SimulationOrder.id` isn't a member of
that table, so these two fields are left untouched on a simulated fill
(guarded by `if not is_simulation:` in `bot_service.py`) rather than
pointed at an id the FK constraint would reject. A simulated bot's full
order history remains queryable via `simulation_orders.bot_id`.

## `AccountRiskSnapshot`: evaluated against the bot's own virtual account

`RiskEngine.validate_order()` needs an `AccountRiskSnapshot` — total
portfolio value, current exposure, open position count, today's realized
loss. `OrderService` builds this from the **real** Binance account via
`PortfolioService`/`PositionsService`. For a Simulation bot, evaluating
risk against the real account would be evaluating the wrong account's
exposure entirely. `SimulationOrderService._evaluate_risk()` instead
builds the snapshot from the bot's own `SimulationAccount` + `BotPosition`
+ one live `get_market_data()` call:

- `total_portfolio_value_quote` = `cash_balance_quote + current_quantity * market_price`
- `current_symbol_exposure_quote` == `current_total_exposure_quote` (a
  `BotPosition` is one instrument per bot, so there is nothing else this
  account could be holding)
- `open_position_count` = 1 if `current_quantity > 0` else 0
- `realized_loss_today_quote` = `max(0, -compute_realized_pnl_today(...))`

`RiskEngine` itself is **not duplicated** — same class, same
`validate_order()` logic, just evaluated against a Simulation account's
numbers with that user's own per-user Simulation limits
(`get_user_simulation_risk_limits()`) rather than their real-order ones.
A Simulation bot reproduces the same *decision behavior* a LIVE bot
would face under equivalent limits.

## Fees and slippage

No fee or slippage modeling exists anywhere else in Hermes today (`Order`
has no fee column; `OrderValidator` doesn't model fees). Two new,
explicit, documented env vars, read at call time and never cached:

| Variable | Default | Effect |
|---|---|---|
| `HERMES_SIMULATION_FEE_RATE_PCT` | `0` | Deducted from cash as `notional * rate / 100` on every fill |
| `HERMES_SIMULATION_SLIPPAGE_RATE_PCT` | `0` | Moves the fill price *against* the account regardless of side — higher for a BUY, lower for a SELL, the same direction real slippage moves |

**The `0` default is a stated v1 limitation, not an invented "reasonable"
number, and not equivalence to real execution costs.** A Simulation
bot's results in this phase do not reflect what real trading fees or
slippage would do to the same trades; operators who want a more realistic
simulation must set these explicitly.

## Initial capital

`default_simulation_initial_capital()` reads
`HERMES_SIMULATION_INITIAL_CAPITAL_USD`, default `10000`. This *is*
appropriate as an env var — an operator-level default, exactly like every
other `HERMES_*` tunable — but it is only a default:
`SimulationAccount.initial_capital_quote` is frozen per-account at
creation time and never re-read afterward, so changing the env var never
retroactively changes an existing bot's starting capital.

## Kill switch: `TRADING_ENABLED` blocks Simulation fills too

This was a deliberate policy decision, not an oversight. Two readings
were possible:

- Simulation could ignore `TRADING_ENABLED` — defensible, since the
  switch's entire purpose ("don't let Hermes touch real money") is
  already unconditionally true for Simulation by construction.
- Simulation could still check `is_trading_enabled()` — an operator who
  flips the switch off expects **nothing** to trade, virtual or not, for
  example during an incident.

**The second reading was chosen.** `TRADING_ENABLED=false` still blocks
Simulation fills: the switch means "Hermes is not placing trades right
now," full stop, not "Hermes is not placing *real* trades right now."
This costs nothing extra — `SimulationOrderService.place_bot_order()`
calls the exact same `is_trading_enabled()` function `OrderService` does,
before any validation or risk evaluation runs, and raises the same
`TradingDisabledError` `BotService` already special-cases (reverting the
bot to its prior status rather than landing it in `ERROR`). Verified
manually against a real Postgres instance with this repo's actual
`.env.dev` value (`TRADING_ENABLED=false`): `resume()` correctly returned
`{"status": "REJECTED", "reason": "Trading is disabled."}` with zero
`SimulationAccount`/`BotPosition` mutation, and is covered by
`tests/test_simulation_order_service.py::test_kill_switch_off_blocks_a_simulation_fill_too`.

`TRADING_ENABLED` itself is untouched by this phase and remains `false`
in every environment this phase was developed against.

## Idempotency

Two-layered, identical in shape to `OrderService`'s: an outer
`reserve()`/`finalize()` scoped to
`f"POST /bots/{bot_id}/simulation-order"` for a direct call, or, when
called from `BotService._transition()`, the same
`f"bot-{op}:{bot_id}:{outer_key}"` inner-key derivation Pause/Resume
already use for `OrderService`. A duplicate request with the same key
returns the identical stored response and never produces a second
`simulation_orders` row — verified in
`tests/test_simulation_order_service.py::test_duplicate_request_never_produces_a_second_fill`.

## Fail-closed on missing market data

If `client.get_market_data()` raises `BinanceError`, the simulated order
is written as `FAILED` with that error as its `reason` and `fill_price`
left `NULL` — **never** a fill at a fabricated price of `0` or a stale
value. The same applies to `SimulationSnapshot` creation: if the bot
holds a position and the price fetch fails, no snapshot row is written
for that tick at all, rather than one with an invented value.

## Isolation guarantees (the central claim of this phase)

Four independent layers, each catching a different failure mode, all
enforced by `tests/test_simulation_isolation.py`:

1. **Static (AST)** — `simulation_order_service.py`'s actual parsed
   source is walked and asserted to never reference `create_order` or
   `cancel_order` as an identifier anywhere, not just in its imports.
   This catches a stray call even if it were dead-code-guarded.
2. **Schema** — `simulation_accounts`/`simulation_orders`/
   `simulation_snapshots` have no foreign key into `orders`, and `orders`
   has no foreign key into them, verified against the live SQLAlchemy
   metadata. Combining real and simulated data requires an explicit
   `UNION`; no ordinary query can do it by accident.
3. **Runtime** — a `BinanceClient` test double that raises
   `AssertionError` the instant `create_order`/`cancel_order` is called,
   exercised across a full simulated BUY-then-SELL cycle through the
   real `SimulationOrderService`. This proves the claim at call-time, not
   just at parse-time.
4. **Real endpoints** — `trading_routes.py` (home of the real
   `GET /portfolio`/`GET /positions`) is confirmed to import no
   Simulation module at all, and
   `tests/test_bot_simulation_api.py::test_a_simulation_fill_never_appears_in_the_real_portfolio`
   drives a real virtual fill through the REST API and asserts
   `GET /portfolio` is byte-for-byte unaffected.

## API

- `GET /bots/{id}` — response gains `execution_mode`; permission
  unchanged (`bots.read`).
- `GET /config/simulation` — **no permission gate.** This is a documented
  operator default (`{"initial_capital_quote": "10000", "quote_asset":
  "USDT"}`), not account data or a secret; the frontend's Bot Creation
  Form budget slider reads it so it never hardcodes the number and stays
  correct if `HERMES_SIMULATION_INITIAL_CAPITAL_USD` is ever reconfigured.
- `GET /bots/{id}/portfolio` — cash/position/exposure/total value/return %
  for this bot's virtual ledger. Reuses `bots.read` + `portfolio.read`
  (no new permissions). Returns `409 {"available": false, "reason": ...}`
  for a `LIVE` bot — there is no per-bot LIVE portfolio view yet, and
  fabricating one would be worse than saying so.
- `GET /bots/{id}/performance` — return %, max drawdown %, today's
  realized P&L, trade count, win rate, exposure. Same permissions, same
  `409` shape for a `LIVE` bot. Sourced from `simulation_orders` (for
  everything computable live) and `simulation_snapshots` (for drawdown,
  the one metric that needs history) via `simulation_portfolio_service`'s
  pure functions plus `portfolio_snapshot_service.compute_max_drawdown_pct`
  reused as-is against the simulation time series.

No new RBAC permissions were added — `bots.read`/`bots.create`/
`bots.pause`/`bots.resume`/`bots.stop`/`portfolio.read` already cover
every route this phase adds.

## Snapshots for drawdown

`PortfolioSnapshotScheduler`'s existing tick gained a second,
independent responsibility in the same thread/interval — not a second
scheduler thread. `run_simulation_snapshot_tick()` queries every
`SIMULATION` bot and snapshots each one; a failure snapshotting one bot
is caught and logged without affecting the others or the real
account-wide snapshot, matching the existing "one bad tick never
permanently stops the scheduler" guarantee. Restart-safety works
identically to the real snapshot table: `snapshot_at` is floored to the
same epoch-aligned interval boundary and inserted via
`INSERT ... ON CONFLICT (bot_id, snapshot_at) DO NOTHING ... RETURNING`
(`RETURNING` rather than the driver's `rowcount`, which was found
unreliable for this exact statement shape on psycopg3 during the
Portfolio Performance phase).

## Current limitations

- **Fees and slippage default to 0** — see "Fees and slippage" above; not
  presented as equivalent to real execution costs.
- **No LIVE activation path exists yet** — `execution_mode` is immutable
  after creation in v1; the frontend shows a `[ Activate LIVE ]` control
  that is visibly disabled with no `onPress` handler that calls anything.
- **No per-bot LIVE portfolio/performance view** — `GET /bots/{id}
  /portfolio` and `/performance` return `409` for a `LIVE` bot rather than
  fabricating a view backed by data this phase doesn't compute.
- **No backtesting, GARCH, Monte Carlo, Strategy Engine, or Model
  Selection V2 connection** — explicitly out of scope for this phase; a
  Simulation bot's Pause/Resume quantities come from the same
  operator-set `target_quantity` every bot has always used.
- **Realized P&L pairing assumes full-position round trips** — valid
  because Pause/Resume always trade a bot's entire position (never a
  partial quantity), so a SELL always closes exactly the most recent BUY.
  This would need to change if partial position sizing is ever added.

## What a future LIVE-activation phase would need to add

1. A real activation endpoint/flow (e.g. `POST /bots/{id}/activate-live`)
   with an explicit confirmation step — not a bare field flip, given the
   real-money consequences.
2. A decision on whether `execution_mode` can ever transition back from
   `LIVE` to `SIMULATION`, and what happens to an in-flight position if so.
3. A per-bot LIVE portfolio/performance view (today's `GET /bots/{id}
   /portfolio`/`/performance` explicitly don't support `LIVE` bots).
4. Whatever additional confirmation/guardrail UX the product decides a
   real-money activation needs — this phase deliberately built no opinion
   on that beyond "must not be a single accidental click."

Everything else — `OrderService`, `RiskEngine`, the kill switch, the
Pause/Resume pipeline, `BotPosition` — already works for `LIVE` today and
needs no further change; `SimulationOrderService`'s existence proves the
decision pipeline (`OrderValidator` → `RiskEngine`) is already fully
shared between the two modes.
