# Hermes v2 — Security Audit (Current State)

**Date:** 2026-08-20
**Scope:** `hermes_v2` (this repo) — backend only. Covers everything built
since [`SECURITY_AUDIT_PHASE_1.md`](SECURITY_AUDIT_PHASE_1.md) (2026-08-15),
including bot lifecycle management, SIMULATION and LIVE trading, per-user
encrypted Binance credentials, the risk engine, and the two-level kill
switch — i.e. exactly the surface Phase 1 explicitly scoped out.
**Prepared for:** pre-public-release review (this repository is being
considered for publication as a portfolio project).

This is a code-level audit performed by reading the actual implementation
and running the project's own available tooling (`pytest`, `ruff`,
`bandit`, `pip-audit`) plus a full history scan (`git log --all -p`
across every local and remote branch). Every claim below is anchored to a
real file, and, where useful, a specific mechanism rather than a general
description. This document does not claim Hermes is unconditionally
"secure" — no non-trivial system is. It states what controls exist, so
they can be verified by reading the code rather than taken on faith.

---

## 1. Methodology

- **Secrets audit**: `git log --all -p` (all commits, all local and
  remote branches — 167 commits at time of writing) grepped for
  credential-shaped patterns (AWS-style keys, private-key headers,
  Binance/Google secrets, Postgres connection strings with embedded
  passwords, generic `password=`/`token=` assignments), plus a
  file-existence check for every `.env*`, `*.pem`, `*.key`, `*.sql`,
  `*.db`/`*.sqlite`, and `credentials.json`-style path ever added or
  deleted across the whole history.
- **Static analysis**: `bandit -r src/` (security-focused linter),
  `ruff check .` (general lint, `E`/`F` rules).
- **Dependency scan**: `pip-audit` against the installed environment.
- **Live code reading**: authentication, authorization, credential
  encryption, risk engine, kill switch, idempotency, and rate-limiting
  modules were read in full, not sampled.
- **Not performed**: penetration testing, fuzzing, a live network scan of
  the production host, or any test against a real Binance account. These
  are out of scope for a code-level, pre-publication audit.

## 2. Secrets & Git History Audit

**No secret has ever been committed to this repository**, in the working
tree or anywhere in its full history.

- `.env` and `.env.dev` (both hold real local values today) have never
  been tracked by Git — confirmed via `git log --all --full-history --
  .env` / `.env.dev` returning no results.
- Across all 167 commits and every branch, only `.env.example` and
  `.env.dev.example` (placeholder values only) were ever added.
- Pattern search across the full history for AWS-style keys, PEM private
  key headers, Binance/Google secret shapes, and Postgres URLs with an
  embedded password returned zero matches.
- Every file ever *deleted* from the repository was inspected by name —
  all are source-code moves/renames from refactors, none are credential
  or dump files.
- No file with a risk-shaped extension (`.pem`, `.key`, `.sql`, `.db`,
  `.sqlite`, `credentials.json`) was ever added at any point in history.
- `.gitignore` excludes `.env`, `.env.*` (with explicit exceptions for
  the two `*.example` templates), `*.pem`, `*.key`, `*.crt`, and
  `secrets/`.
- `tests/test_secrets_hygiene.py` runs in the normal test suite and
  fails the build if a real `.env` file is ever tracked — this is an
  enforced regression guard, not only a one-time check.

**No known secret exposure identified within the audit scope.**

## 3. Authentication — `src/hermes_v2/auth/oauth.py`, `google.py`, `service.py`, `session.py`

Unchanged in design since Phase 1, re-verified current: Google OAuth
authorization-code flow, server-side token exchange (the OAuth client
secret never reaches the browser), ID-token issuer/audience verification,
a single-use server-stored `state` token, and no self-registration path —
a Google account with no pre-provisioned matching `User` row is rejected.
Sessions are `secrets.token_urlsafe(32)`-generated, stored only as a
SHA-256 hash, and compared with `hmac.compare_digest` — brute-forcing or
guessing a valid session token is infeasible.

**Known limitation (carried over from Phase 1, still accurate):**
`OAuthStateStore` is in-process memory — state tokens don't survive a
process restart and won't work correctly across multiple workers/replicas.
Not exploitable in the current single-process deployment; would need to
move to a shared store (Postgres row with TTL, or similar) before scaling
beyond one worker.

## 4. Authorization — `src/hermes_v2/auth/authorization.py`, `seed.py`

**This is the single largest change since Phase 1**, which flagged
authorization enforcement as not yet built. It now is: 31 permissions are
seeded (`auth/seed.py`'s `PERMISSION_CATALOG`), and a `require_permission`
FastAPI dependency gates every mutating route — 37 call sites across
`api/app.py`, `bots_routes.py`, `trading_routes.py`, and
`settings_routes.py`. No mutating endpoint relies on the frontend to have
already decided whether an action is allowed; each declares its own
required permission at the route level.

## 5. Binance Credentials & Encryption — `src/hermes_v2/trading/credentials_encryption.py`, `binance_credentials_service.py`

- Credentials are multi-tenant: each user connects their own Binance API
  key/secret, stored per-user, never shared or global.
- **Encryption at rest**: `cryptography`'s `MultiFernet` (AES-128-CBC +
  HMAC-SHA256, authenticated encryption) encrypts the key/secret before
  they ever reach the database. Plaintext is never persisted — only
  ciphertext plus a masked last-4 characters (for display) are stored.
- **Key rotation**: genuinely supported, not aspirational.
  `MultiFernet([Fernet(current), Fernet(previous), ...])` means a new
  encryption key can start encrypting immediately while old keys keep
  decrypting existing rows until they're re-encrypted — no downtime, no
  bulk migration required for the rotation itself.
- **Withdrawal-capable keys are rejected outright.** Before a key is ever
  stored, Hermes calls Binance to check the key's own permissions and
  refuses to save one with withdrawal enabled — the worst-case blast
  radius of a compromised, stored key is bounded to whatever the key
  itself is scoped to (spot trading), never a fund withdrawal.
- Encryption/decryption fail closed if the encryption key environment
  variable is missing — there is no silent fallback to storing plaintext.

## 6. LIVE Trading Gating & the Two-Level Kill Switch — `src/hermes_v2/trading/config.py`, `kill_switch.py`

Verified directly in code, not inferred from documentation:

- **Global switch**: `is_trading_enabled()` reads `TRADING_ENABLED` from
  the environment, defaulting to `false` if unset. Checked *inside*
  `OrderService`, not only at the route layer — no route, no future
  internal caller, and no ad-hoc script can place or cancel a real order
  while it's off. Nothing in this codebase sets it to `true`
  automatically (not `docker compose up`, not a migration, not the
  deploy script — confirmed by reading `deploy/romeo-setup.sh` in full).
- **Per-user switch**: a second, database-backed switch checked after the
  global one (`kill_switch.is_trading_permitted`) — lets one user pause
  their own trading without affecting anyone else, but can never enable
  trading if the global switch is off.
- **LIVE is reached only by explicit promotion**: a bot is always created
  in SIMULATION; LIVE is a separate, one-way action available only once
  that specific bot is PAUSED and the caller has separately connected
  verified Binance credentials. There is no code path from bot creation
  directly to LIVE, and no code path that reverts a LIVE bot back to
  SIMULATION.
- **No accidental-LIVE path from a fresh clone**: `TRADING_ENABLED=false`
  and empty `BINANCE_API_KEY`/`BINANCE_API_SECRET` are the default in
  both `.env.example` and `.env.dev.example`; `compose.yaml` requires an
  operator-provided `.env` to even start the container. `git clone →
  docker compose up` cannot place a real order under any configuration
  found in this repository.

## 7. Risk Engine — `src/hermes_v2/trading/risk_engine.py`

Per-user, configurable limits: max order notional, max symbol exposure %,
max total exposure %, max daily loss %, max open positions, and an
allowed-symbol list. **Fail-closed by design**: any limit left
unconfigured (`NULL`) is treated as "reject on that dimension," never as
"no limit." A brand-new user with no risk settings row is treated
identically to a row with every limit set to reject — there is no
configuration state, including the absence of one, that means
"unrestricted."

## 8. Idempotency — `src/hermes_v2/trading/idempotency.py`

Every mutating endpoint requires an `Idempotency-Key`. A retried or
duplicated request with the same key replays the stored result of the
original attempt rather than re-executing it — verified by dedicated
tests asserting a duplicate request never calls Binance twice, including
under a genuinely ambiguous outcome (Binance confirms an order exists but
the original response was never received).

## 9. Rate Limiting — `src/hermes_v2/trading/rate_limiting.py`, `auth/rate_limiting.py`

15 dedicated sliding-window rate limiters, scoped per sensitive action
(login, credential changes, order placement, each bot lifecycle action
individually) rather than one global limit — a burst on one action type
cannot exhaust the budget for an unrelated one.

## 10. Docker

- Runs as a non-root user (`hermes`) inside the container.
- No secret is ever copied into the image; `.dockerignore` excludes
  `.env*`, `*.key`, `*.pem`, and credential-shaped filenames as defense
  in depth beyond the Dockerfile's own `COPY` allowlist.
- The repository's own `compose.yaml` (what a fresh clone uses) publishes
  no port for either the app or PostgreSQL — safe by default.
- **Known limitation**: the compose configuration actually used on the
  production host (embedded in `deploy/romeo-setup.sh`, not the
  repo-root `compose.yaml`) does publish the app's port. Since a
  repository can't observe its own deployment's firewall/network
  topology, this is now a parametrized, opt-in-restriction binding
  (`HERMES_BIND_ADDRESS`, documented inline in `romeo-setup.sh`) rather
  than a hardcoded assumption in either direction — see that file's
  comments for how an operator applies a stricter bind once they've
  confirmed their own network exposure.

## 11. Dependencies

- `pip-audit` against the installed environment: no known vulnerabilities
  found.
- `bandit -r src/`: no issues identified.
- All runtime dependencies (`pyproject.toml`) are genuinely imported and
  used — none are dead weight.
- `uv.lock` is committed and kept in sync with `pyproject.toml` (verified
  via `uv sync --locked`).

## 12. CI Security Scanning — `.github/workflows/`

`bandit` and `pip-audit` run on every push/PR to `main`. A Trivy scan
runs against the built container image for OS- and library-level
CRITICAL/HIGH CVEs, with the job configured to fail the build on a
finding. Fork PRs run with read-only, secret-less permissions — no
privilege-escalation path into a workflow that holds a real credential
was found.

## 13. Known Limitations

Stated plainly, not buried:

- `OAuthStateStore` is in-process memory (§3) — a scaling limitation, not
  a currently-exploitable one.
- No formal code-coverage measurement is configured for the test suite —
  its breadth is documented qualitatively in `README.md`, not as a
  percentage, because none has been measured.
- The production host's actual network exposure (firewall rules,
  Tailscale/VPN configuration) cannot be verified from this repository —
  §10's port-binding note documents the dependency rather than assuming
  an answer.
- This audit did not include a live penetration test, fuzzing, or a scan
  of the running production host — it is a code-level review.

## 14. Summary

No known vulnerabilities were identified within the audit scope described
above. The controls documented here — the two-level kill switch,
fail-closed risk engine, encrypted-at-rest credentials with supported key
rotation, RBAC, idempotency, and CI-enforced static/dependency/image
scanning — are real, currently in effect, and exercised by the test
suite, not aspirational. Section 13 lists what remains open rather than
omitting it.
