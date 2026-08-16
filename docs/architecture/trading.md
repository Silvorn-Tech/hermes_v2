# Trading architecture (Phase 2: `feature/binance-trading-integration-v1`)

This is a real-money integration. The whole design rests on one invariant:
**no HTTP request can reach `BinanceClient.create_order()`/`cancel_order()`
except through `OrderService`**, and `OrderService` refuses to call Binance
unless every gate below has already passed. Nothing in this codebase ever
enables live execution automatically — `TRADING_ENABLED` defaults to
`false` and stays `false` until an operator sets it by hand, deliberately,
after reading this document.

```text
Frontend
   |  GET /auth/me (existing) -> hermes_session cookie
   v
Hermes API (src/hermes_v2/api/trading_routes.py)
   |  require_permission(...)            <- auth + RBAC (401 / 403)
   |  require_trusted_origin (mutating)  <- CSRF guard (403)
   |  Idempotency-Key header (mutating)  <- 422 if missing
   v
OrderService (src/hermes_v2/trading/order_service.py)
   |  idempotency.reserve()              <- 409 on conflict/in-progress
   |  is_trading_enabled()               <- kill switch (403, nothing persisted)
   |  OrderValidator                     <- Binance's own trading rules
   |  RiskEngine                         <- Hermes's own limits, fail-closed
   v
BinanceClient (src/hermes_v2/integrations/binance.py)
   |  signed HMAC request
   v
Binance
   |
   v
reconciliation.py -> Order row (Postgres) -> audit_log row -> response
```

A validation or risk rejection still **persists an `Order` row** with
`status=REJECTED` — Hermes did real work (derived a `clientOrderId`, ran
checks) and that's worth keeping for audit/traceability. A kill-switch
rejection persists **nothing but an `audit_log` row** — nothing was
attempted, so there's no order to record.

## Endpoints and permissions

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/portfolio` | `portfolio.read` | Total value + priced balances, no fabricated daily P&L |
| GET | `/balances` | `portfolio.read` | Raw per-asset balances |
| GET | `/market-data?symbol=` | `portfolio.read` | Passthrough of `BinanceClient.get_market_data` |
| GET | `/positions` | `positions.read` | Derived Spot positions, see below |
| GET | `/orders` | `orders.read` | Reconciles non-terminal orders before returning |
| GET | `/orders/{id}` | `orders.read` | Same reconcile-on-read |
| POST | `/orders` | `orders.create` | + `require_trusted_origin` + `Idempotency-Key` |
| POST | `/orders/{id}/cancel` | `orders.cancel` | + `require_trusted_origin` + `Idempotency-Key` |
| POST | `/positions/{symbol}/close` | `positions.close` | + `require_trusted_origin` + `Idempotency-Key` |

Every mutating route is checked by
`tests/test_authorization.py::test_every_mutating_route_is_permission_gated_or_exempt`,
which walks the real app's routes looking for a `require_permission()`
marker. Only `SUPER_ADMIN` is granted the trading permissions today — no
role-assignment endpoint exists yet, so granting them to a non-admin role
is a future admin action, done directly against the `roles`/`role_permissions`
tables until one is built.

## Database

Migrations `20260815_0002_trading_schema` and `20260815_0003_positions_permissions`
add:

- **`orders`** — Hermes's operational record of an order. `status` mirrors
  Binance's own states plus two Hermes-only ones (`PENDING`, `FAILED`) for
  the window before Binance has acknowledged anything.
- **`order_events`** — append-only history per order (`SUBMITTED`,
  `BINANCE_ACK`, `RISK_REJECTED`, `RECONCILED`, ...).
- **`idempotency_keys`** — generic dedupe table shared by all three
  mutating actions, scoped by `(user_id, endpoint, idempotency_key)`.
- **`audit_log`** — one row per mutating action's *outcome*
  (`SUCCESS`/`REJECTED`/`FAILED`), separate from `order_events` because a
  kill-switch rejection has no order to attach an event to but still needs
  an audit trail.
- `positions.read` / `positions.close` added to the permission catalog,
  granted to `SUPER_ADMIN`.

**Binance remains the final authority on order state.** A row in `orders`
is Hermes's last-known view, refreshed by reconciliation — never treated
as ground truth on its own past the moment it was written.

## Kill switch

`hermes_v2.trading.config.is_trading_enabled()` reads `TRADING_ENABLED`
(default `false`). Checked *inside* `OrderService`, not only at the route
layer, so no code path — a route, a future internal caller, a script run
by hand — can place or cancel an order while it's off.

## Idempotency (two layers)

1. **Hermes DB.** Every mutating request carries an `Idempotency-Key`
   header. `hermes_v2.trading.idempotency.reserve()` claims a row via a
   `SAVEPOINT` before any Binance call: a concurrent duplicate hits the
   table's unique constraint and gets back either the already-completed
   response (a true retry) or `IdempotencyInProgressError` (a genuine
   concurrent duplicate, HTTP 409) — never a second execution. Reusing a
   key with a *different* request body raises `IdempotencyConflictError`
   (409).
2. **Binance `newClientOrderId`.** Deterministic from
   `(user_id, endpoint, idempotency_key)` — scoped by endpoint so the same
   key string used for a manual order and a close-position action can't
   collide on Binance's side.

On an ambiguous `create_order` failure (timeout, connection drop after
Binance may have already accepted it), `OrderService` confirms **exactly
once** via `get_order(client_order_id=...)` before concluding anything. If
that confirmation also fails, the order is marked `FAILED` and is **not**
auto-retried on a subsequent request with the same key — an operator must
verify against Binance directly before the caller tries again with a new
key.

## Risk limits (fail-closed by design)

`RiskEngine` (`hermes_v2.trading.risk_engine`) checks six
`HERMES_RISK_*` environment variables. **If any one of them is
unconfigured, every order is rejected** — this is the intended safety
posture, mirroring this codebase's existing precedent (`HERMES_ALLOWED_ORIGINS`
unset means CORS denies every origin; an unrecognized permission means
`require_permission()` denies by construction). No threshold is invented
here; an operator must decide what "too much" means for their own account
before Hermes will place an order:

| Variable | Checked against |
|---|---|
| `HERMES_RISK_MAX_ORDER_NOTIONAL_USD` | Every order |
| `HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT` | BUY orders only |
| `HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT` | BUY orders only |
| `HERMES_RISK_MAX_DAILY_LOSS_PCT` | Every order (both sides — a daily circuit breaker) |
| `HERMES_RISK_MAX_OPEN_POSITIONS` | BUY orders opening a new symbol only |
| `HERMES_RISK_ALLOWED_SYMBOLS` | Every order |

`HERMES_RISK_MAX_DAILY_LOSS_PCT`'s realized-loss figure is computed from
currently-held positions' trade history — a position fully closed earlier
the same day no longer appears in that computation. See
`hermes_v2.trading.positions_service`'s module docstring for the exact
scope this covers.

## Positions (Binance Spot has no position concept)

A "position" is derived: a non-zero asset balance plus a weighted-average
cost basis computed from `BinanceClient.get_trades(symbol)`. **Position
identity is the symbol itself** (`BTCUSDT`) — there's no other stable ID in
Spot. `POST /positions/{symbol}/close` submits a `MARKET SELL` for the full
held quantity through the exact same validate -> risk -> execute pipeline
as any other order.

## Reconciliation

Two triggers, both synchronous — no background worker exists anywhere in
this codebase, and adding one is out of scope for this pass:

1. Immediately after `OrderService` submits an order (the Binance response
   itself).
2. **Reconcile-on-read**: `GET /orders` / `GET /orders/{id}` re-fetch a
   non-terminal order from Binance before returning. Best-effort — if
   Binance can't be reached right now, the last-known (possibly stale)
   state is returned rather than failing the read.

## What's explicitly out of scope this pass

- Frontend wiring (separate repo, `hermes_front_end`) — a follow-up once
  these endpoints are stable to point at.
- Any role-management/role-assignment endpoint — only `SUPER_ADMIN` can
  trade until one exists.
- A risk-limit CRUD endpoint — limits are env-config only.
- Any background/async reconciliation worker — reconcile-on-read only.
- Withdrawals, transfers, deposit addresses — never touched, by
  construction (`BinanceClient`'s whitelist test enforces this).

## LIVE activation runbook

Nothing here happens automatically. To actually enable real order
execution on ROMEO:

1. Create a Binance API key with **withdrawals disabled** and an **IP
   allowlist restricted to ROMEO's egress IP** (Binance account console —
   see `docs/security/secrets-management.md` §7).
2. Set `BINANCE_API_KEY`/`BINANCE_API_SECRET` in `/opt/hermes-v2/.env`,
   following the existing rotation procedure (same file, same
   `docker compose up -d hermes-v2` — not `restart`).
3. Run `hermes binance-check` and confirm every check passes, including
   `Permissions: READ_ONLY` — a key with withdrawals enabled fails this
   diagnostic on principle and must not be used regardless of what else
   passes.
4. Set all six `HERMES_RISK_*` variables in the same `.env` — trading stays
   rejected until every one is set.
5. Grant the operator's Hermes user the trading permissions
   (`orders.create`, `orders.cancel`, `positions.close`, plus the
   corresponding `.read` ones) — today this means the `SUPER_ADMIN` role,
   assigned via `hermes bootstrap-admin`.
6. Only then set `TRADING_ENABLED=true` in the same `.env`.
7. `docker compose -f /opt/hermes-v2/compose.yaml up -d hermes-v2`.
8. Monitor the first order manually: place a small order, verify it in
   both `GET /orders/{id}` and the Binance UI, confirm the `audit_log` row
   looks right, before treating the deployment as routine.

Rolling back live execution is one line: set `TRADING_ENABLED=false` and
re-apply — reads and the rest of the app keep working normally, only order
creation/cancellation/close stop.
