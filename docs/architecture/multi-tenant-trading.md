# Multi-tenant trading architecture (`feature/multi-tenant-binance-credentials`)

Hermes v2 was single-tenant for trading execution through
`docs/architecture/trading.md`: one global Binance API key/secret in
`.env`, one global set of `HERMES_RISK_*` risk limits, one global kill
switch. Auth/RBAC, `Bot.user_id`, `Order.user_id`, and idempotency keys
were already scoped per user — this phase closes the remaining gap so
more than one real person can trade with their **own** Binance account
through the same deployment.

```text
Settings UI (frontend)
   |  PUT /settings/binance-credentials {api_key, api_secret}
   v
settings_routes.py
   |  require_permission("secrets.manage") + origin + idempotency + rate limit
   v
binance_credentials_service.connect_credentials()
   |  verify FIRST: client.get_account_info() against real Binance
   |  reject if can_withdraw is not False
   v
credentials_encryption.py (MultiFernet)  ->  user_binance_credentials (Postgres)
   |
   v
Any later real-order/portfolio request for this user:
   binance_credentials_service.get_decrypted_client(session, user_id)
   -> decrypt -> BinanceClient(api_key=..., api_secret=...)
```

## 1. Per-user Binance credentials

- **Verify-before-persist**: `connect_credentials()` never writes
  anything until Binance itself has confirmed the key/secret work,
  mirroring `cli.py`'s `binance_check()` diagnostic philosophy for the
  one operator key. A key with withdrawals enabled is hard-rejected
  (`CredentialsUnsafeError`), not just flagged — closing a gap
  `binance_check()` only ever surfaced as advisory output.
- **Encryption at rest**: `cryptography.fernet.MultiFernet`, keyed by
  `HERMES_CREDENTIALS_ENCRYPTION_KEY` (`.env`-only, generated once,
  never in git). See `docs/security/secrets-management.md` §7 for the
  full secret-handling rationale and rotation procedure
  (`HERMES_CREDENTIALS_ENCRYPTION_KEY_PREVIOUS`, decrypt-only).
- **Never returned after submission**: every read of
  `GET /settings/binance-credentials` returns only `api_key_last4` (a
  credit-card-style masked hint, captured at connect time) plus
  timestamps. Ciphertext columns are never serialized into any response.
- **The pre-existing single operator key is unaffected**:
  `BINANCE_API_KEY`/`BINANCE_API_SECRET` in `.env` and `cli.py`'s
  `binance_check()` remain exactly as documented in
  `docs/security/secrets-management.md` — an ops-only pre-flight
  diagnostic, decoupled from what the trading routes read after this
  phase. There is no automated migration from that key into any user's
  row; the operator reconnects their own key through the same verified
  UI everyone else uses.

## 2. Per-user risk limits

`user_risk_settings_service.py` reuses `risk_engine.RiskLimits` and
`RiskEngine` directly — same six fields, same fail-closed "`None` means
not configured, reject on that dimension" semantics the global,
env-var-based limits already had. The only thing that changes is the
source: a per-user Postgres row (`user_trading_settings`) instead of the
process environment.

**Applies to real orders only.** `OrderService._evaluate_risk` resolves
`RiskEngine(get_user_risk_limits(session, order.user_id))`.
`SimulationOrderService._evaluate_risk` deliberately stays on the
global, env-based `load_risk_limits()` — a brand-new user's per-user
limits default to all-`None` (the only sane, symmetric default for "no
row yet"), and switching Simulation to per-user limits too would reject
every simulation order for every new user until they filled in six
Settings fields. Simulation bots never need Binance credentials or risk
configuration to exist — that must stay true for onboarding to work.

## 3. Two-tier kill switch

Two independent switches, both required (`AND`, never `OR`) — see
`kill_switch.py`:

- **Global** (`trading.config.is_trading_enabled()`, unchanged): the
  platform's one real emergency stop. An env var, set by hand on the
  host, defaults to disabled. See `docs/architecture/trading.md`.
- **Per-user** (`user_risk_settings_service.is_user_trading_enabled()`,
  new): a self-service convenience pause, defaults **enabled** (`true`)
  — the opposite default from the global switch, deliberately, because
  it gates a per-user convenience, not the platform's actual money-safety
  boundary (that's permission gating + the global switch + per-user
  credentials + `RiskEngine`, none of which this boolean is). Gates both
  real orders and Simulation fills, for symmetry with the pre-existing
  global-switch policy already documented in `simulation_order_service.py`
  ("the switch means Hermes is not placing trades right now — full
  stop").

`OrderService`/`SimulationOrderService` call
`kill_switch.is_trading_permitted(session, user_id)` instead of the bare
global `is_trading_enabled()` at every gate.

## 4. `BinanceClient` construction: three tiers

`trading_routes.py` and `bots_routes.py` each keep their own copy of
three small helpers (module-level duplication is deliberate — tests
monkeypatch `BinanceClient` by module attribute name):

- **Required** (`_new_binance_client_for_user`): resolves a real,
  per-user credentialed client via
  `binance_credentials_service.get_decrypted_client()`. 409s with a
  structured `{"available": false, "reason": ...}` body — never a
  500 — if this user hasn't connected an account; 503 if the encryption
  key itself isn't configured on this server. Used by every route that
  actually needs to read or write this user's real Binance account:
  create/cancel order, close position, `GET /portfolio`, `/balances`,
  `/positions`.
- **Optional** (`_new_optional_binance_client_for_user`): best-effort,
  returns `None` instead of raising. Used by `GET /orders` and
  `GET /orders/{id}`'s reconcile-on-read, which already degrades
  gracefully without Binance (a read must never 5xx or 409 just because
  reconciliation couldn't run).
- **Public** (`_new_public_binance_client`): no credentials at all
  (`BinanceClient(api_key="", api_secret="")`), for call sites that
  exclusively use Binance's unsigned endpoints. `GET /market-data` uses
  this. So does every one of `bots_routes.py`'s five call sites, today —
  every bot is `SIMULATION` (see `BotExecutionMode`), and `BotService`
  never actually calls a method on `self._client` for
  list/get/pause/resume/stop/delete. This is the concrete mechanism that
  makes "no bot lifecycle action requires a connected Binance account"
  true. `_run_bot_service_action` carries a `TODO` for the day LIVE bot
  creation ships: it must then branch on `bot.execution_mode` and resolve
  a required, per-user client for the LIVE case.

## 5. Endpoints and permissions

New in `settings_routes.py`, reusing the already-reserved
`secrets.read`/`secrets.manage`/`risk.read`/`risk.manage` catalog
permissions (role-granted, same pattern `bots.create` already uses:
"may act on your own resources," not "admin only" — there is no
`user_id` path parameter on any of these routes, so each can only ever
touch the caller's own row):

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/settings/binance-credentials` | `secrets.read` | `{configured, api_key_last4, verified_at, updated_at}` |
| PUT | `/settings/binance-credentials` | `secrets.manage` | + origin + idempotency + rate limit; verify-before-persist; 409 on verification failure or unsafe key; 503 if encryption unconfigured |
| DELETE | `/settings/binance-credentials` | `secrets.manage` | + origin + idempotency + rate limit; idempotent |
| GET | `/settings/risk-limits` | `risk.read` | Wire `RiskLimits`, six nullable fields |
| PUT | `/settings/risk-limits` | `risk.manage` | + origin + idempotency + rate limit; per-field validated (notional > 0, pct in [0,100], open_positions >= 1, symbols uppercased/deduped) |
| GET | `/settings/trading-switch` | `risk.read` | `{enabled}` |
| PUT | `/settings/trading-switch` | `risk.manage` | + origin + idempotency + rate limit; `{enabled}` |

Every mutating route here is checked by
`tests/test_authorization.py::test_every_mutating_route_is_permission_gated_or_exempt`,
same as `trading_routes.py`/`bots_routes.py`.

## 6. What did not change

- `Bot.user_id`, `Order.user_id`, and every idempotency key were already
  scoped per user before this phase — the data-ownership layer needed no
  retrofit.
- The global kill switch (`TRADING_ENABLED`) and its activation runbook
  in `docs/architecture/trading.md` are unchanged.
- `RiskEngine`'s own logic (the six checks, fail-closed on `None`) is
  unchanged — only where its `RiskLimits` input comes from differs
  between real orders (per-user) and Simulation (global).
