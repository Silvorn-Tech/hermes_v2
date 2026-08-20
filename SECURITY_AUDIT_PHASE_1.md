# Hermes v2 — Security Audit (Phase 1: Security Hardening)

> **Historical document.** This audit covers Hermes as it existed on
> 2026-08-15 — before bots, LIVE trading, multi-tenant Binance
> credentials, and the risk engine were built. It's kept for the record,
> not as a description of the current system. See
> [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md) for the current-state audit,
> which covers exactly the trading surface this document explicitly
> scoped out.

**Date:** 2026-08-15
**Scope:** `hermes_v2` (backend, this repo) and `hermes_front_end` (frontend, sibling repo)
**Branch:** `feature/security-hardening-v1` (both repos)
**Out of scope:** Binance/trading integration — not implemented yet, and this audit does not implement it.

This is a code-level audit. Every finding below is anchored to a real file and, where the file
wasn't subsequently edited by this audit's fixes, a line number. Nothing here is a generic OWASP
checklist item — items that don't apply to Hermes's actual code (e.g. SQL injection via string
concatenation, which the codebase never does) are marked OK with the evidence that ruled them out,
not omitted.

---

## 1. Executive Summary

Hermes v2's authentication core (Google OAuth → server-side session cookie) is well-built for its
stage: authorization-code flow with server-side token exchange, ID token issuer/audience
validation, an exact-match open-redirect allowlist, hashed+constant-time-compared session tokens,
httpOnly cookies, and an allowlist-only CORS policy that never combines a wildcard with
credentials. No secrets have ever been committed to git in either repo (verified against full
history, not just the working tree). Dependency scans are clean on the backend and show only
build-tool vulnerabilities (not runtime) on the frontend. CI/CD is already a pull-based GitOps
design — GitHub Actions never holds a deploy credential to the production host, and fork PRs run
with read-only, secret-less permissions.

The gaps are concentrated in three places: **authorization enforcement doesn't exist yet** (the
RBAC data model is built and seeded, but no endpoint checks it — there's nothing to check yet,
but the checking mechanism itself still needs to be written before it's needed), **there is no
abuse protection** (no rate limiting anywhere, no security response headers until this audit added
a baseline), and **production configuration is currently incomplete** — the live `/opt/hermes-v2/.env`
on ROMEO is missing `HERMES_ALLOWED_RETURN_URIS`, which independently breaks Google login in
production right now (confirmed by `hermes_front_end/docs/deployment.md`, written by a prior
session against the live host). None of this blocks Phase 1; all of it should close before Phase 2
(Binance).

**Verdict: not trading-ready, but the foundation is sound.** Nothing found rises to "an attacker
can authenticate as someone else" or "a secret is exposed." The work here is closing operational
gaps (rate limiting, headers, logging, prod config) before there's money on the line.

---

## 2. Architecture

```
Browser (hermes_front_end, static SPA via nginx, Tailscale-only)
   │  GET /auth/google/login?return_to=<allowlisted frontend URL>
   ▼
hermes_v2 backend (FastAPI, single process)
   │  307 redirect, state=<random, server-stored, 10 min TTL>
   ▼
Google consent screen
   │  redirects back with code + state
   ▼
GET /auth/google/callback
   │  1. consume+validate state (single use)
   │  2. exchange code for tokens server-side (client_secret never leaves backend)
   │  3. verify Google ID token (issuer, audience=own client_id, sub present)
   │  4. resolve Google sub → existing Hermes user (pre-provisioned, no self-signup)
   │  5. create session row (sha256(token) stored, raw token returned once)
   │  6. set httpOnly `hermes_session` cookie
   │  7. redirect to return_to?auth=success
   ▼
Frontend calls GET /auth/me (cookie sent automatically) → renders authorized app
```

Auth data model: `users` ⟷ `identities` (provider + provider_subject, unique) ⟷ `sessions`
(token_hash, expires_at, revoked_at), plus `roles`/`permissions` (RBAC, seeded, currently unused
by any endpoint). Only four backend endpoints exist today — `/health`, `/auth/google/login`,
`/auth/google/callback`, `/auth/me`, `/auth/logout` — confirmed by reading the only route file,
`src/hermes_v2/api/app.py`. There is no trading, bot-control, admin, or settings endpoint anywhere
in the codebase.

Deployment is pull-based GitOps on both repos: GitHub Actions builds and publishes an image to
GHCR on merge to `main`; a systemd timer on ROMEO polls GHCR and redeploys on digest change.
**GitHub Actions never has network access to ROMEO or holds a ROMEO credential** — this is a good
architectural choice that eliminates an entire class of CI/CD → prod compromise.

---

## 3. Authentication — `src/hermes_v2/auth/oauth.py`, `google.py`, `service.py`, `session.py`

**¿Puede alguien autenticarse con una cuenta Google no autorizada?** No. `resolve_google_user`
(`auth/service.py:58-104`) only succeeds if the Google `sub` is already linked to a Hermes
`Identity`, or — on first login — if the verified email matches a **pre-existing** `User` row.
There is no self-registration path; a Google account with no matching row raises
`GoogleUserNotFoundError` and the callback redirects with `?auth=denied`
(`oauth.py`, `google_callback`). Users are provisioned only via `hermes bootstrap-admin`
(`cli.py`) or (not yet built) an admin endpoint.

**¿Puede alguien reutilizar una sesión?** Sessions are checked on every request against
`expires_at`, `revoked_at`, and the linked user's `status == ACTIVE`
(`session.py:110-137`, `get_user_from_session`). A revoked or expired token is rejected. There is
no session cleanup job (expired/revoked rows stay in the table indefinitely — 🔵 LOW, hygiene
only, not a security hole since they're already unusable).

**¿Puede alguien falsificar una sesión?** No. The cookie value is `secrets.token_urlsafe(32)` (256
bits of entropy) and only its SHA-256 hash is stored (`session.py:84-107`); lookups compare with
`hmac.compare_digest` (`session.py:124`, `154`). Brute-forcing or guessing a token is infeasible.

**¿Puede alguien saltarse la autorización llamando directamente a la API?** N/A today — there is no
endpoint that performs an authorization-sensitive operation yet. `/auth/me` requires a valid
session (401 otherwise); `/auth/logout` requires nothing but only acts on the caller's own cookie.
See §4 for why this changes the moment a mutating endpoint is added.

**Replay attacks:** the authorization `code` is single-use by Google's own protocol; the local
`state` token is consumed exactly once (`OAuthStateStore.consume`, `oauth.py:62-77`, pops the
entry) and expires after 10 minutes even if unused. An intercepted callback URL is a single-use
credential system-wide, not a replayable one.

**Session fixation:** not applicable — the raw session token is generated by the backend after
identity is verified; nothing client-supplied is ever accepted as a session identifier before
authentication.

🟡 **MEDIUM — `OAuthStateStore` is in-process memory** (`oauth.py:38-48`, the author's own
docstring already flags this). State tokens don't survive a process restart and won't work across
multiple workers/replicas. Not exploitable today (single-process deployment), but it's a
correctness/availability landmine the moment the backend scales beyond one worker — logins would
intermittently fail with "invalid or expired state" for requests routed to a different worker than
the one that issued it. Fix before adding `uvicorn --workers > 1` or a second replica: move to a
shared store (Postgres row with TTL, or Redis if one gets introduced for other reasons).

🟡 **MEDIUM — no logging of auth events** (fixed in this audit, see §14).

🔵 **LOW — Google token-exchange errors were silently discarded**, not logged (fixed in this
audit — see §14 and §20).

---

## 4. Authorization

**No endpoint currently depends on the frontend for an authorization decision** — verified by
reading every route in `api/app.py`: `/health` (no auth), `/auth/google/login` and `/callback`
(pre-auth by definition), `/auth/me` (requires valid session, returns the session's own user —
nothing to authorize beyond "is this a real session"), `/auth/logout` (acts only on the caller's
own session). There is no admin, settings, user-management, or bot-control endpoint in the
codebase to audit for a missing check, because none exist yet. This matches the user's own
instruction not to build trading yet.

🟠 **HIGH — the authorization *mechanism* doesn't exist yet.** `Role` and `Permission` are modeled
and seeded (`auth/models/role.py`, `permission.py`, `auth/seed.py` — a 20-entry permission catalog
including `orders.create`, `orders.cancel`, `risk.manage`, `deployments.execute`) but **nothing in
the codebase reads `user.roles` or checks a permission** — there is no `require_permission(...)`
FastAPI dependency anywhere. This is correctly scored HIGH rather than CRITICAL because there is
currently nothing for it to protect; it becomes a CRITICAL blocker the moment the first mutating
endpoint (bot pause/resume, order actions, settings) is opened, because that endpoint will have
*nothing to enforce authorization with* unless this is built first. Recommended fix (§20): build
the `require_permission()` dependency now, against the existing seeded permissions, before any
endpoint needs it — so the very first mutating endpoint is required to declare its permission by
construction, not as an afterthought.

The rule the user stated —

```
request → authenticated user → authorization check → operation
```

— has no counterexample in the current code, because "operation" doesn't exist yet for anything
sensitive. Flag this section for re-audit the moment a PR adds a `POST`/`PUT`/`PATCH`/`DELETE`
endpoint beyond `/auth/logout`.

---

## 5. Session Security — `src/hermes_v2/auth/session.py`

| Property | Value | Evidence |
|---|---|---|
| Cookie name | `hermes_session` | `session.py:17` |
| httpOnly | always `True` | `oauth.py` `response.set_cookie(..., httponly=True, ...)` |
| Secure | `HERMES_COOKIE_SECURE` env, **default `false`** | `session.py:26-29` |
| SameSite | `HERMES_COOKIE_SAMESITE` env, default `"lax"`, validated against `{lax,strict,none}` | `session.py:32-51` |
| Path | `/` | `oauth.py` `set_cookie(path="/")` |
| Domain | not set (defaults to exact host, no subdomain sharing) | `oauth.py` |
| TTL | `HERMES_SESSION_TTL_SECONDS` env, default 86400s (24h) | `session.py:54-64` |
| Token entropy | `secrets.token_urlsafe(32)` = 256 bits | `session.py:97` |
| Storage | SHA-256 hash only, raw token never persisted | `session.py:84-107` |
| Lookup comparison | `hmac.compare_digest` (constant-time) | `session.py:124`, `154` |
| Revocation | `revoked_at` timestamp, checked on every lookup | `session.py:127-129`, `140-161` |
| Rotation | new token every login; no mid-session rotation | n/a — not needed, no fixation vector |
| Expired/revoked cleanup | **none** | 🔵 LOW — no scheduled purge |

**Is the current session design secure enough for production?** Yes, mechanically. The open
question is entirely configuration: `HERMES_COOKIE_SECURE` **defaults to `false`**, and the
checked-in local `.env` doesn't set it either (only `.env.dev` does). If production's real
`/opt/hermes-v2/.env` on ROMEO doesn't explicitly set `HERMES_COOKIE_SECURE=true`, the session
cookie is issued without the `Secure` attribute in an environment that is served over HTTPS —
functionally it still only travels over the HTTPS connection Tailscale Serve terminates, but the
cookie itself doesn't *enforce* that, so any future accidental plaintext-HTTP path (a
misconfigured proxy, a debug port) would leak it. **This must be verified directly on ROMEO** — see
§16 and §22.

No `HERMES_SESSION_SECRET` or equivalent exists in the code, and none is needed: sessions are
opaque random tokens looked up by hash, not signed/HMAC'd tokens (like a JWT) that would need a
signing key. The security-critical variable that does the equivalent job here is the entropy
source (`secrets.token_urlsafe`, cryptographically secure, no configuration needed) plus
`HERMES_COOKIE_SECURE`/`HERMES_COOKIE_SAMESITE`, which **do** need explicit production values.

---

## 6. OAuth — Google

| Control | Status | Evidence |
|---|---|---|
| `state` param | random 32-byte token, server-stored, single-use, 10 min TTL | `oauth.py:38-77` |
| Redirect URI validation | fixed, server-configured `GOOGLE_REDIRECT_URI`, never client-influenced | `oauth.py:98-99`, `139-149` |
| `return_to` allowlist | exact-match against `HERMES_ALLOWED_RETURN_URIS`, rejects anything else with 400 | `oauth.py:102-131` |
| Authorization code exchange | server-side, `client_secret` never leaves the backend | `oauth.py:160-190` |
| ID token verification | Google's own `google-auth` library, audience pinned to own `GOOGLE_CLIENT_ID` | `google.py:26-50` |
| Issuer check | `accounts.google.com` or `https://accounts.google.com` only | `google.py:41-45` |
| Subject (`sub`) present | required, rejected otherwise | `google.py:47-48`, `service.py:27-32` |
| Email verified | required **only** for first-time linking by email; existing identities skip it (correct — the identity is already proven) | `service.py:47-56` |
| Nonce | not used | not applicable — this is the server-side auth-code flow, not the implicit flow; code-for-token exchange already provides freshness |
| Authorized users only | yes — see §3 | `service.py:82-87` |
| Callback errors | mapped to a coarse `?auth=` status, no internal detail leaked to the browser | `oauth.py:258-284` |

**`return_to=https://evil.com`** is rejected: `_resolve_return_uri` (`oauth.py:125-131`) 400s
anything not in `_configured_allowed_return_uris()`, which is a `HERMES_ALLOWED_RETURN_URIS`
allowlist with no wildcard support (`oauth.py:102-111`, plain string split on comma, exact match
only — no prefix/suffix matching that could be abused).

🟡 **MEDIUM (re-flagged from §3):** the state store's in-memory, single-process nature is this
section's main structural risk. It's a correctness constraint on scaling, not an exploitable
OAuth flaw today.

---

## 7. CORS — `src/hermes_v2/api/app.py`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured_allowed_origins(),  # from HERMES_ALLOWED_ORIGINS, comma-split
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
```

🟢 **OK.** `allow_origins` is never a wildcard — it reads `HERMES_ALLOWED_ORIGINS` and returns an
empty list if unset (fail closed, not fail open). The code comment at the top of the function
(`_configured_allowed_origins`, originally lines 19-28) explicitly documents *why*: browsers reject
`Access-Control-Allow-Origin: *` combined with `Access-Control-Allow-Credentials: true`, so a
wildcard wouldn't even work with cookie auth — the author already built this correctly.
`allow_methods` is restricted to `GET, POST` (no `PUT`/`PATCH`/`DELETE` exist yet, so none are
allowed), and `allow_headers` to just `Content-Type`.

**Controlling variable:** `HERMES_ALLOWED_ORIGINS` (comma-separated exact origins). Dev vs. prod
differ only in this env var's value — same code path, no dev/prod branching logic to audit
separately.

🔴 **CRITICAL (production config, not code) — currently unset in production.** Per
`hermes_front_end/docs/deployment.md` (written against the live ROMEO host), `/opt/hermes-v2/.env`
is missing `HERMES_ALLOWED_RETURN_URIS`; `HERMES_ALLOWED_ORIGINS` needs the same verification (its
absence fails *closed*, so the practical symptom is the frontend's `fetch` calls being rejected by
CORS, not a security hole — but it must still be set correctly for the app to function at all in
production). See §16 and §22.

---

## 8. CSRF

Hermes uses cookie-based auth, so CSRF is a real question. Current mutating surface: exactly one
endpoint, `POST /auth/logout`. There is no CSRF token, double-submit cookie, or explicit
`Origin`/`Referer` check anywhere in the code.

**Why this is lower risk than it looks, today:** `HERMES_COOKIE_SAMESITE` defaults to `"lax"`
(`session.py:45`), and per the deployment docs the frontend
(`https://romeo-dev-zone.tailed9c54.ts.net:8443`) and backend
(`https://romeo-dev-zone.tailed9c54.ts.net`) share a registrable domain — different ports, but the
`SameSite` attribute is scoped to *site* (scheme + registrable domain), not origin, so it's
same-site across those two ports. A `SameSite=Lax` cookie is **not** sent on a cross-site
POST triggered by a third-party page (only on top-level, safe-method navigations), so a classic
`<form action="https://api.../auth/logout" method="POST">` hosted on `evil.com` would not carry the
cookie. The worst case today is a forced logout from a truly same-site page, which is low-value.

🟡 **MEDIUM — no CSRF defense-in-depth exists, and none should be assumed permanent.**
`SameSite=Lax` is one configuration change away from being weakened (e.g. a future requirement to
set `HERMES_COOKIE_SAMESITE=none` for a genuinely cross-site client — a mobile app webview, a
second frontend on a different domain). The moment any endpoint does something consequential
(pause bot, close a position, cancel an order), relying solely on `SameSite` is fragile: it's a
browser-behavior guarantee, not an application-level control, and CORS (§7) doesn't help here
either — a plain HTML `<form>` POST never triggers a CORS preflight and isn't subject to the CORS
allowlist at all (only `fetch`/`XHR` are). **Before any trading-adjacent mutating endpoint ships**,
add an explicit control: the cheapest is validating the `Origin` header against
`HERMES_ALLOWED_ORIGINS` on state-changing requests (a few lines, no new dependency); the more
thorough option is a double-submit CSRF token. This is documented as a recommendation (§20) rather
than implemented in this pass, since it's exactly the kind of "prepare the architecture, don't
build trading yet" item the user asked to scope for later — but it should land *before* the first
mutating trading endpoint, not after.

---

## 9. Secrets Inventory

No real values below — names only, per the request.

| Secret / Variable | Exists | Required | Environment | Lives in | Sensitive | Status |
|---|---|---|---|---|---|---|
| `GOOGLE_CLIENT_ID` | Yes | Yes | dev + prod | `.env` (gitignored, host) | Low (public-ish, but treat as config) | ✅ set locally; verify prod |
| `GOOGLE_CLIENT_SECRET` | Yes | Yes | dev + prod | `.env` (gitignored, host) | **High** | ✅ set locally; verify prod, confirm not reused across environments |
| `GOOGLE_REDIRECT_URI` | Yes | Yes | dev + prod | `.env` | Low (config, not secret) | ✅ set |
| `DATABASE_URL` | Yes | Yes | dev + prod | `.env` | **High** (embeds DB password) | ✅ set; prod password strength unverified from this environment |
| `POSTGRES_PASSWORD` | Yes | Yes | dev + prod | `.env` | **High** | ⚠️ local `.env`/`.env.dev` use a dev-labeled placeholder-strength value — confirm prod uses a distinct, strong, generated one |
| `POSTGRES_USER` / `POSTGRES_DB` | Yes | Yes | dev + prod | `.env` | Low | ✅ set |
| `HERMES_ADMIN_EMAIL` | Yes | Yes | dev + prod | `.env` | Medium (identifies the super-admin account) | ✅ set |
| `HERMES_ALLOWED_ORIGINS` | Yes (code) | Yes (functionally) | dev + prod | `.env` | Low | ⚠️ **confirmed unset or unverified in prod** |
| `HERMES_ALLOWED_RETURN_URIS` | Yes (code) | Yes (functionally) | dev + prod | `.env` | Low | 🔴 **confirmed unset in prod** — login is broken right now (per frontend deploy docs) |
| `HERMES_DEFAULT_RETURN_URI` | Yes (code) | No (optional) | dev + prod | `.env` | Low | Optional |
| `HERMES_COOKIE_SECURE` | Yes (code) | Yes in prod | prod | `.env` | Low | ⚠️ unverified in prod; defaults to `false` if unset |
| `HERMES_COOKIE_SAMESITE` | Yes (code) | Recommended | dev + prod | `.env` | Low | ⚠️ unverified in prod; defaults to `lax` if unset (likely fine, verify) |
| `HERMES_SESSION_TTL_SECONDS` | Yes (code) | No (optional) | dev + prod | `.env` | Low | Optional, defaults to 24h |
| `HERMES_HOST` | Yes (code) | No | container | Dockerfile/runtime default | Low | Not needed — defaults correctly to `0.0.0.0` for container networking |
| `EXPO_PUBLIC_API_URL` | Yes | Yes | dev + prod | frontend `.env` / GHA `vars.EXPO_PUBLIC_API_URL_PRODUCTION` | **Not sensitive** — baked into the public JS bundle by design | ✅ set, correctly public |
| `GITHUB_TOKEN` | Yes (GitHub-provided) | Yes | CI only | GitHub Actions, ephemeral | Medium (scoped, short-lived) | ✅ used correctly (packages:write only on `main`-branch publish jobs) |
| SSH keys / deploy credentials to ROMEO | **Does not exist** | N/A by design | — | — | — | ✅ correctly absent — deploy is pull-based, GitHub never touches ROMEO |
| Session signing/secret key | **Does not exist** | Not needed | — | — | — | ✅ correctly absent — sessions are opaque random tokens looked up by hash, not signed tokens; no `HERMES_SESSION_SECRET` needed under this design |
| Encryption-at-rest key | **Does not exist** | Not needed yet | — | — | — | Nothing currently stored needs it; revisit before Binance secrets are stored (§17) |
| `EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID` / `_IOS_CLIENT_ID` / `_ANDROID_CLIENT_ID` | Referenced in dead code only | No | — | Never set anywhere | Low | See §18 finding — unused client-side OAuth path, safe to remove |
| Binance API key / secret | **Does not exist** | Not yet — trading not implemented | — | — | **Highest** | See §17 |

---

## 10. Secrets Leak Audit

Checked: full git history (not just working tree) of both repos for committed `.env` files,
Google client secrets, database passwords, and private-key headers.

```
git log --all -p -- .env    → only .env.dev.example was ever committed (no real values)
git log --all -p | grep -inE "GOCSPX-|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY-----|postgresql\+psycopg://[^:]+:[^@]+@"
                              → zero real credential matches in either repo's full history
```

🟢 **OK — no secret has ever been committed to either repo.** `.gitignore` in `hermes_v2` correctly
excludes `.env`, `.env.*` (with explicit exceptions for the two `*.example` files), `*.pem`,
`*.key`, `*.crt`, `secrets/`. The two `.example` files contain no real values, only placeholders
(and this audit strengthened them — see §20).

No `EXPO_PUBLIC_` variable in the frontend carries anything sensitive by design — `EXPO_PUBLIC_API_URL`
is a plain hostname, and no Google client ID/secret or backend credential is ever read by frontend
code (confirmed: `grep` for `GOOGLE_CLIENT` in frontend source only appears in the unused
`useGoogleAuthRequest.ts`, whose env vars are never set — see §18).

No README, doc, compose file, Dockerfile, GitHub Actions workflow, test fixture, or mock data file
in either repo contains a real secret (checked by reading every workflow file and every `.env*`
file, and grepping test fixtures for credential-shaped strings).

---

## 11. GitHub Actions — both repos

**`hermes_v2/.github/workflows/`:**

| Workflow | Trigger | Secrets used | Fork-PR risk |
|---|---|---|---|
| `ci.yml` | push/PR to `main` | none | None — no secrets in scope |
| `docker-ci.yml` | PR to `main` | none (`push: false`) | None — builds only, never pushes |
| `docker.yml` | push to `main`, `workflow_dispatch` | `GITHUB_TOKEN` (packages:write) | None — **never runs on `pull_request`**, so a fork PR cannot trigger it |
| `security.yml` | push/PR to `main` | none | None — `pip-audit`, `bandit`, Trivy, all read-only |

**`hermes_front_end/.github/workflows/`:** `ci.yml` (PR, no secrets), `docker.yml` (`main`-only +
`workflow_dispatch`, uses `GITHUB_TOKEN` and the non-secret `vars.EXPO_PUBLIC_API_URL_PRODUCTION`).

🟢 **OK — a PR cannot execute code with access to production secrets.** No workflow in either repo
uses `pull_request_target` (the trigger that would hand a fork PR a privileged token). The only
job with `packages: write` (`docker.yml` build-and-publish) only runs on `push` to `main` — which a
fork PR cannot cause — or manual `workflow_dispatch`. No workflow holds ROMEO SSH keys or any
deploy credential; the pull-based deploy model (§2) means there is nothing for a compromised
Action to steal that would reach the production host.

🔵 **LOW — no GitHub Environment protection configured.** Neither `docker.yml` uses an
`environment:` key, so there's no required-reviewer gate before an image reaches `:latest`. Given
the trigger is already restricted to `main`-branch pushes (which presumably already requires a
reviewed PR to merge), this is a defense-in-depth nice-to-have, not a gap that enables anything on
its own — flagged as optional in §22.

---

## 12. Docker

**Backend `Dockerfile`:**
```dockerfile
FROM python:3.12-slim
...
RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin hermes \
    && chmod +x ./entrypoint.sh \
    && chown -R hermes:hermes /app
USER hermes
HEALTHCHECK ...
```
🟢 **OK — already runs as a non-root user** (`hermes`, no shell). Minimal `slim` base, healthcheck
present, `ENTRYPOINT` runs `alembic upgrade head` then `bootstrap-admin` then the app — no shell
injection risk (fixed argv, no interpolated user input).

**Frontend `Dockerfile`:** multi-stage (`node:22-alpine` build → `nginx:1.27-alpine` runtime),
healthcheck present, `EXPOSE 8080`. The `nginx:1.27-alpine` image's master process runs as root by
default (standard for that image; worker processes drop privilege internally) — 🔵 LOW, could move
to `nginxinc/nginx-unprivileged` for full non-root, optional hardening, not blocking.

**`compose.yaml` (production, backend):** no `ports:` published for either `hermes-v2` or
`postgres` — both are reachable only over the internal Docker network. 🟢 **OK — Postgres is not
published to the host in the committed compose file.** Log rotation configured (`max-size: 10m,
max-file: 3`). No CPU/memory limits, no `read_only`/`cap_drop` hardening — 🔵 LOW, standard
defense-in-depth to add later, not urgent for a single-tenant deployment.

**`compose.dev.yaml`:** Postgres published to `127.0.0.1:5432` only (loopback, not `0.0.0.0`) —
correct for local dev.

⚠️ **UNVERIFIED — the live `/opt/hermes-v2/compose.yaml` on ROMEO may differ from this repo's
`compose.yaml`.** The frontend's own deployment doc (`hermes_front_end/docs/deployment.md`)
explicitly contrasts the frontend's loopback-only exposure against *"the backend's current
`0.0.0.0:8000`"* — implying the backend's container port is currently bound more broadly than this
repo's checked-in `compose.yaml` shows. Two explanations are equally plausible from here: either
the live ROMEO compose file was hand-configured before this repo's `compose.yaml` existed and
never reconciled, or Tailscale Serve is pointed at a `0.0.0.0`-bound port for a reason not visible
in either repo. **This must be checked directly on ROMEO** (not from this sandboxed environment,
which has no access to it) — see §16 and §22. If port 8000 is in fact bound to `0.0.0.0` rather
than `127.0.0.1`, it is reachable from ROMEO's LAN (and potentially further, depending on the
host's network exposure) without going through the Tailscale-authenticated path the frontend
uses — this would be the single highest-priority item to verify and fix before Phase 2.

---

## 13. Database — PostgreSQL

- **SQL injection:** not applicable — every query in the codebase goes through SQLAlchemy's ORM
  (`select(...)`, `.where(Model.col == value)`) with bound parameters; there is no raw SQL string
  built from request input anywhere in `src/`. 🟢 OK.
- **Connection:** `create_engine(database_url_from_environment(), pool_pre_ping=True)`
  (`database/connection.py:23-25`) — pooled, with pre-ping to avoid stale-connection errors. No
  connection limit configured explicitly (uses SQLAlchemy's pool defaults) — fine at current scale.
- **Migrations:** Alembic, two migrations to date, both additive schema (no destructive migration
  patterns found).
- **Network exposure:** see §12 — not published to the host in the committed compose file; must be
  verified live on ROMEO.
- **Credentials:** `DATABASE_URL` is a single connection string with embedded password, read once
  from the environment — never logged, never returned in an API response.
- **Backups:** no backup tooling or documented backup strategy found anywhere in either repo. 🟡
  **MEDIUM** — not a vulnerability per se, but worth flagging: there is currently no evidence of a
  Postgres backup/restore plan for ROMEO. Recommend documenting one (even a simple `pg_dump` cron)
  before Phase 2, since trading state (positions, orders) will make data loss materially worse
  than it is today (auth data only, easily re-bootstrapped).

---

## 14. Logging

**Before this audit:** the auth module (`oauth.py`, `session.py`, `service.py`) had **zero**
logging calls. This cuts both ways — nothing sensitive was ever logged (no risk of a leaked
token/cookie/password in logs), but there was also no audit trail: a failed login, a denied user,
an invalid/expired OAuth state, or a Google token-exchange failure produced no log line at all.
One concrete bug: `_exchange_google_code`'s `HTTPError` handler read Google's error response body
and then discarded it (`error_body = exc.read().decode(...)` with the result never used) — a real
operational blind spot, since misconfiguration (wrong redirect URI, expired client secret) would
fail silently with no diagnostic trail.

**Fixed in this audit** (§20): `oauth.py` now logs, at appropriate levels, without ever including a
token, cookie, or secret value:
- OAuth callback rejected for invalid/expired `state` (`logger.warning`)
- Google token-exchange failure, with Google's `error` code only, never the full response body or
  any request parameter (`logger.warning`)
- A Google identity resolved but denied (user not authorized), logging the email and the reason
  class (`logger.info`)
- A Hermes session successfully created, logging only the internal user ID (`logger.info`)

`runtime.py` already used a generic, safe log format (`asctime levelname name: message`) with no
per-request or credential data.

🟡 **MEDIUM — still no structured audit trail for security-relevant events beyond auth.** Once
authorization checks and trading actions exist, each should log who did what and the outcome
(§17). This is scoped as future work, not implemented now (no such actions exist yet to log).

---

## 15. Dependencies

**Backend (`hermes_v2`)** — `pip-audit` against the resolved `uv.lock` dependency set:
```
No known vulnerabilities found
```
`bandit -r src/` — clean (only a `# nosec`-suppressed, deliberately-justified `0.0.0.0` bind for
container networking, `runtime.py:26`).

**Frontend (`hermes_front_end`)** — `npm audit --omit=dev`:
```
21 vulnerabilities (7 moderate, 14 high) — 0 critical
```
All 21 trace to `image-size`, `metro`/`metro-config`, and `uuid`/`xcode`, pulled in transitively by
`expo`'s **build tooling** (the Metro bundler, `@expo/config-plugins`, `@expo/cli`). These are
**development/build-time-only dependencies** — none of them ship in the exported static web bundle
that actually runs in a user's browser (verified: `image-size`, `metro`, `xcode` are Node-only
packages used during `expo export`, not runtime React Native/web modules). Classified:

| Package | Severity | Runtime or dev-only |
|---|---|---|
| `image-size` (via `metro`) | HIGH | Dev-only (build tool) |
| `metro`, `metro-config`, `metro-transform-worker` | HIGH | Dev-only (bundler) |
| `uuid` <11.1.1 (via `xcode`) | MODERATE | Dev-only (iOS project file generation, unused for web export) |
| `xcode` (via `@expo/config-plugins`) | MODERATE | Dev-only |

A fix requires `expo@53.0.27`, a **major, breaking downgrade** from the currently pinned
`~57.0.13` — explicitly not applied automatically per the user's instruction not to run major
upgrades without review. Recommend tracking this and revisiting when Expo SDK 57's own patch line
addresses it upstream, rather than downgrading.

---

## 16. Production Configuration

Compared what the code actually reads (§9) against what's checked into each `.env*` file:

| File | Purpose | Gaps found |
|---|---|---|
| `hermes_v2/.env` (local checkout, gitignored) | Mirrors what's presumably deployed via `compose.yaml` | Sets `DATABASE_URL`/Google creds/`HERMES_ADMIN_EMAIL` but **omits `HERMES_ALLOWED_ORIGINS`, `HERMES_ALLOWED_RETURN_URIS`, `HERMES_COOKIE_SECURE`, `HERMES_COOKIE_SAMESITE`** entirely |
| `hermes_v2/.env.dev` | Local dev, outside Docker | Complete — has all the vars the checked-in `.env` is missing |
| `hermes_v2/.env.dev.example` (was committed) | Template for new devs | **Was missing 8 of the 12 variables the code actually reads** — fixed in this audit (§20) |
| `hermes_v2/.env.example` | Production template | **Did not exist at all** — created in this audit (§20) |

**The live `/opt/hermes-v2/.env` on ROMEO cannot be inspected from this environment** — this
sandbox has no SSH access to ROMEO, and inspecting/modifying a production host is outside what
should happen without the user present regardless. What *is* independently confirmed, in writing,
by `hermes_front_end/docs/deployment.md` (committed 2026-08-15, describing the live host): **ROMEO's
`/opt/hermes-v2/.env` is currently missing `HERMES_ALLOWED_RETURN_URIS`, which means Google login
is broken in production right now**, independent of anything in this audit. That doc also gives the
exact fix (set the variable, then `docker compose -f /opt/hermes-v2/compose.yaml up -d hermes-v2`
— not `restart`, which doesn't reload `env_file` values). This is carried into the checklist below
verbatim since it's a known, already-diagnosed gap, not a new finding.

**Must be verified directly on ROMEO** (this audit cannot do so):
1. `HERMES_ALLOWED_RETURN_URIS` — confirmed missing, needs setting (see above).
2. `HERMES_ALLOWED_ORIGINS` — verify it's set to the frontend's exact production origin.
3. `HERMES_COOKIE_SECURE` — verify it's `true` (code defaults to `false` if unset).
4. `HERMES_COOKIE_SAMESITE` — verify it's an intentional value, not an accidental default.
5. `POSTGRES_PASSWORD` — verify it isn't the same placeholder-strength value used in this repo's
   local `.env`/`.env.dev` (`hermes_dev_password`-style).
6. The backend container's actual port binding (`0.0.0.0` vs `127.0.0.1` vs unpublished) — see §12.

---

## 17. Frontend Security — `hermes_front_end`

Reviewed `hooks/AuthContext.tsx`, `services/auth.ts`, `services/api.ts`, `hooks/useGoogleAuthRequest.ts`,
`types/auth.ts`, `app.json`, both `.env*` files, `Dockerfile`, `deploy/nginx.conf`.

| Check | Result |
|---|---|
| Google client secret in frontend | 🟢 Absent — confirmed by reading every file in `services/`, `hooks/`; only `GOOGLE_CLIENT_ID`-*shaped env var names* appear, in unused code (see below), and no value is ever set for them |
| Backend secrets (DB, session) in frontend | 🟢 Absent |
| Bearer tokens | 🟢 None used — `services/auth.ts` relies entirely on the httpOnly cookie via `credentials: 'include'`; `services/api.ts` (the future data-layer client) is an unimplemented placeholder, not wired to anything |
| Session cookie storage in JS | 🟢 Absent — `hermes_session` is httpOnly, never readable by JS, and the frontend never attempts to read or store it (confirmed: no `document.cookie`, no `expo-secure-store` usage for auth, despite the plugin being installed) |
| Hardcoded users/credentials | 🟢 None found |
| Authorization logic in frontend | 🟢 The frontend only gates *UI rendering* (`isAuthorized` in `AuthContext.tsx`) — every actual authorization decision is a backend call (`GET /auth/me`); there is no code path where the frontend decides *access*, only what it *displays* |

🔵 **LOW — dead code: `hooks/useGoogleAuthRequest.ts` implements a second, unused, client-side
Google OAuth flow** (`expo-auth-session`'s `useIdTokenAuthRequest`), reading
`EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID`/`_IOS_CLIENT_ID`/`_ANDROID_CLIENT_ID`. **Confirmed unused** —
`grep` for `useGoogleAuthRequest` across the repo shows only its own definition, no import
anywhere; none of its env vars are set in `.env`, `.env.example`, or CI. It predates the current
server-side flow (`services/auth.ts`) and appears to be left over from an earlier branch
(`feature/google-auth-login`). Not a live vulnerability — it does nothing today — but it's exactly
the kind of orphaned alternate auth path that risks being wired back in by a future contributor
without realizing it bypasses the backend's authorization check entirely (a client-side ID token
flow like this never calls the backend at all). **Recommend removing** `hooks/useGoogleAuthRequest.ts`
and the `expo-auth-session` dependency in a follow-up cleanup PR — not done in this pass since it
touches `package.json`/`package-lock.json` and is a cleanup, not a CRITICAL/HIGH security fix.

🟢 **OK — `EXPO_PUBLIC_API_URL` is the only environment variable the frontend needs**, and it is
non-sensitive by design (it's baked into the public JS bundle regardless; there's nothing to
protect). `.env.example` already documents this correctly with a clear comment explaining why no
Google credentials belong in the frontend.

---

## Security Score

Each score is justified by the evidence cited in its section above — not a generic rubric.

| Category | Score | Why |
|---|---|---|
| Authentication | 8/10 | Correct server-side OAuth-code flow, ID token issuer/audience validation, authorized-users-only, hashed constant-time-compared sessions. Deducted for: no auth-event audit trail before this pass, single-process OAuth state store. |
| Authorization | 5/10 | RBAC model and permission catalog exist and are well-designed, but zero enforcement code exists — appropriate given nothing sensitive to protect yet, but the mechanism itself must be built before the next mutating endpoint (finding #3). |
| Session | 8/10 | Strong entropy, hashed storage, constant-time comparison, revocation, configurable Secure/SameSite/TTL. Deducted for: production values for those config flags unverified, no expired-session cleanup. |
| OAuth | 8/10 | Server-side code exchange, strict state+return_to allowlisting, no open redirect possible. Deducted for: in-memory state store, no failure logging before this pass (fixed). |
| CORS | 9/10 | Never combines wildcard with credentials; allowlist-only, fails closed. Deducted only for unverified production value. |
| Secrets | 6/10 | Zero secrets ever committed to git (verified against full history), correct `.gitignore`. Deducted for: no secrets-manager, unverified production Postgres password strength, incomplete `.env.example` before this pass (fixed). |
| API | 7/10 | Pydantic/FastAPI typed params, no stack-trace leakage, generic error responses. Deducted for: no rate limiting, no request size limits (both still open). |
| Database | 8/10 | ORM-only queries (no SQL injection surface), not published to host network in the committed compose file, pooled connections. Deducted for: no documented backup strategy, live ROMEO config unverified. |
| Docker | 7/10 | Backend runs non-root with a healthcheck and minimal base image. Deducted for: no resource limits, frontend nginx image not fully non-root, live ROMEO port binding unverified. |
| CI/CD | 9/10 | Pull-based GitOps — GitHub Actions never holds a ROMEO credential; fork PRs run with read-only, secret-less permissions; dedicated security workflow (pip-audit, bandit, Trivy) already wired in. Deducted only for missing optional Environment protection. |
| Logging | 7/10 (was 4/10 before this pass) | Was zero auth-event logging plus one silent-failure bug; now logs OAuth failures, denials, and session creation without ever logging a secret. Still no broader audit trail (nothing to audit yet beyond auth). |

**Global score: 7.4/10 — solid foundation, not yet trading-ready.** Nothing found allows
authenticating as someone else, forging a session, or leaking a secret. What's missing is
production-config verification, abuse protection, and the authorization-enforcement mechanism that
the next phase of work will need on day one.

---

## 18. Findings Summary (severity-ranked)

| # | Severity | Area | Finding | Status |
|---|---|---|---|
| 1 | 🔴 CRITICAL | Production config | ROMEO's `/opt/hermes-v2/.env` missing `HERMES_ALLOWED_RETURN_URIS` — Google login is broken in prod right now | Documented; requires host access to fix (§22) |
| 2 | 🔴 CRITICAL (unverified) | Docker/network | Backend container possibly bound to `0.0.0.0:8000` in the *live* ROMEO compose config (differs from this repo's `compose.yaml`), per frontend deployment docs | Needs verification on ROMEO (§12, §22) |
| 3 | 🟠 HIGH | Authorization | No `require_permission()`-style enforcement mechanism exists yet, despite a seeded RBAC model — must be built before the first mutating endpoint | Documented, recommended for next PR (§20) |
| 4 | 🟠 HIGH | Abuse protection | No rate limiting anywhere (login, callback, `/auth/me`, `/auth/logout`) | Documented, not implemented this pass — needs a dependency decision (§20) |
| 5 | 🟠 HIGH | Config verification | `HERMES_COOKIE_SECURE`/`HERMES_COOKIE_SAMESITE`/`HERMES_ALLOWED_ORIGINS`/`POSTGRES_PASSWORD` strength unverifiable on live ROMEO from this environment | Checklist item (§22) |
| 6 | 🟡 MEDIUM | OAuth/Auth | `OAuthStateStore` is in-memory, single-process — breaks under multi-worker/multi-replica scaling | Documented (§3, §6) |
| 7 | 🟡 MEDIUM | CSRF | No CSRF defense beyond `SameSite=Lax`; fine today (one low-value mutating endpoint), fragile before trading endpoints exist | Documented, recommend `Origin` header check before Phase 2 (§8) |
| 8 | 🟡 MEDIUM | Logging | No auth-event audit trail; one silent error-swallowing bug in token exchange | **Fixed in this audit** (§14, §20) |
| 9 | 🟡 MEDIUM | Database | No documented backup/restore strategy | Documented, recommend before Phase 2 (§13) |
| 10 | 🟡 MEDIUM | Headers | No security response headers on API or static frontend | **Fixed in this audit** (§20) |
| 11 | 🔵 LOW | Frontend | Dead client-side OAuth code path (`useGoogleAuthRequest.ts`) | Documented, recommend removal (§17) |
| 12 | 🔵 LOW | Docker | No CPU/memory limits, no `nginx-unprivileged`, no `cap_drop`/`read_only` hardening | Documented, optional (§12) |
| 13 | 🔵 LOW | Session hygiene | No cleanup job for expired/revoked session rows | Documented, optional (§5) |
| 14 | 🔵 LOW | CI/CD | No GitHub Environment protection on the publish job | Documented, optional (§11) |
| — | 🟢 OK | Multiple | CORS, SQL injection surface, secrets-in-git, CI/CD fork-PR isolation, Docker non-root backend, session token entropy/hashing/comparison, OAuth state/PKCE-equivalent/redirect validation, frontend secret hygiene | Verified, no action needed |

---

## 19. Severity Legend Applied

🔴 CRITICAL — exploitable now or blocks core function in production
🟠 HIGH — not exploitable today but becomes CRITICAL with the very next reasonable change (a new
endpoint, a scaled deployment)
🟡 MEDIUM — real gap, currently low practical impact, must close before Phase 2 (Binance)
🔵 LOW — hygiene/hardening, optional
🟢 OK — verified correct, evidence cited

---

## 20. Recommended Fixes — what was implemented in this pass vs. deferred

**Implemented (this audit, on `feature/security-hardening-v1`, both repos):**

1. **Backend: baseline security response headers** — new `SecurityHeadersMiddleware` in
   `src/hermes_v2/api/app.py` adds `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
   `Referrer-Policy: no-referrer` to every response, plus `Strict-Transport-Security` when
   `HERMES_COOKIE_SECURE` is true (i.e., whenever the deployment is HTTPS). No CSP — this is a pure
   JSON/redirect API with no HTML rendering, so a CSP has nothing to constrain yet.
2. **Backend: fixed the silent error-swallowing bug and added auth-event logging** in
   `src/hermes_v2/auth/oauth.py` — Google token-exchange failures now log the HTTP status and
   Google's `error` code (never the full response body, never a secret); invalid/expired OAuth
   `state` logs a warning; denied logins log the email and reason; successful session creation
   logs the internal user ID. No token, cookie, or secret value is ever logged.
3. **Backend: created `.env.example`** (didn't exist before) — a complete production template
   documenting every environment variable the code reads, with no real values, including inline
   comments on the security-relevant ones (`HERMES_COOKIE_SECURE`, `HERMES_COOKIE_SAMESITE`,
   `HERMES_ALLOWED_ORIGINS`/`_RETURN_URIS`) explaining what to set and why.
4. **Backend: completed `.env.dev.example`** — it was missing 8 of the 12 variables the code
   actually reads (all the OAuth/CORS/cookie ones); now documents all of them with dev-appropriate
   placeholder guidance.
5. **Frontend: baseline security headers** in `deploy/nginx.conf` — `X-Content-Type-Options`,
   `X-Frame-Options`, `Referrer-Policy` on every response.

All five changes were verified non-breaking: `uv run pytest` (52 passed, 27 skipped — the skips are
pre-existing DB-marked tests requiring a live `DATABASE_URL`, unrelated to this change),
`ruff check`/`ruff format --check` (clean), and `bandit -r src/` (clean, no new findings) all pass
after the edits.

**Deferred — documented but not implemented, with the reason:**

- **Rate limiting** (finding #4) — needs a dependency decision (e.g. `slowapi`) and a policy
  decision (per-IP? per-session? what limits?) that's a real design choice, not a "clearly correct"
  drop-in fix. Recommend a follow-up PR once the user picks an approach.
- **Authorization enforcement mechanism** (finding #3) — building `require_permission()` against
  the seeded RBAC model is worthwhile now, but it's still a new architectural piece (a FastAPI
  dependency pattern that every future endpoint will follow) that deserves its own review rather
  than being bundled into an audit-fix commit.
- **CSRF `Origin` header check** (finding #7) — low urgency with the current single low-value
  mutating endpoint; recommend adding when the first trading-adjacent mutating endpoint is built,
  not before, since it's easiest to write against a real target endpoint.
- **ROMEO production config fixes** (findings #1, #2, #5) — require access to the live host,
  which this session doesn't have and shouldn't attempt to obtain implicitly. See §22 for the
  exact commands.
- **`useGoogleAuthRequest.ts` removal** — cleanup, not a security fix per se (the code is inert);
  bundling a `package.json` dependency removal into a security-audit commit muddies the diff.
  Recommend as a separate, clearly-labeled cleanup PR.
- **Postgres backup strategy, Docker resource limits, session cleanup job, GitHub Environment
  protection** — all LOW/MEDIUM hygiene items appropriate for a dedicated hardening pass once
  Phase 1's higher-severity items close, not urgent enough to bundle here.

---

## 21. Production Checklist

- [ ] Set `HERMES_ALLOWED_RETURN_URIS` in `/opt/hermes-v2/.env` on ROMEO (confirmed missing —
      login is currently broken in production)
- [ ] Verify `HERMES_ALLOWED_ORIGINS` is set to the frontend's exact production origin
- [ ] Verify `HERMES_COOKIE_SECURE=true` on ROMEO
- [ ] Verify `HERMES_COOKIE_SAMESITE` is an intentional value (`lax` is correct given the current
      same-site-different-port topology)
- [ ] After any `.env` change: `docker compose -f /opt/hermes-v2/compose.yaml up -d hermes-v2`
      (not `restart` — it does not reload `env_file`)
- [ ] Verify the backend container's port binding on ROMEO is `127.0.0.1`-only or unpublished, not
      `0.0.0.0` (see finding #2)
- [ ] Verify `POSTGRES_PASSWORD` on ROMEO is a strong, generated value distinct from this repo's
      local dev placeholder
- [ ] Confirm Postgres is not reachable from outside ROMEO's Docker network
- [ ] Pull this branch's `.env.example` and diff against the live `/opt/hermes-v2/.env` to catch
      any other drift
- [ ] Re-run `curl -s -o /dev/null -w "%{http_code}\n" https://romeo-dev-zone.tailed9c54.ts.net:8443/login`
      and confirm end-to-end Google login works after the above (per
      `hermes_front_end/docs/deployment.md`'s existing verification steps)

---

## 22. Future Trading Security (documentation only — nothing implemented)

Before connecting Binance or any live trading:

- **Binance API key storage:** never in `.env` in plaintext on ROMEO if avoidable at that point —
  Hermes has no secrets-manager/vault integration today (Google/DB creds are plain env vars,
  acceptable for the current stakes). Revisit this specifically for Binance given the blast radius
  of a leaked trading key is direct financial loss, unlike a leaked OAuth client secret.
- **Withdrawal permission MUST be disabled** on every Binance API key Hermes ever uses — this is a
  Binance account-console setting, not a code control, but it's the single most important item on
  this list.
- **IP restrictions:** Binance supports IP-allowlisted API keys — restrict to ROMEO's egress IP.
- **Separate keys per environment:** never share a Binance key between dev/staging and production;
  Hermes doesn't have a staging trading environment yet, but plan the key-naming/storage convention
  before the first key is created.
- **Encryption at rest:** if Binance secrets end up in Postgres (e.g. per-user API keys, if Hermes
  ever supports more than one trader), they need column-level encryption, not plaintext — the
  current schema has no precedent for storing any encrypted secret, so this is new work.
- **Rotation:** define a rotation cadence and a documented rotation procedure before go-live, not
  after an incident.
- **Audit logging:** every trading action (order placed, cancelled, bot paused/resumed, position
  closed) needs a durable, queryable audit log — who, what, when, from where. Nothing like this
  exists yet; §20's authorization-enforcement work should be designed to naturally produce this
  (a `require_permission()` dependency is a natural place to also emit an audit event).
- **Idempotency:** trading endpoints (place order, cancel order) need idempotency keys to survive
  retries safely — a duplicated "place order" call must not double-execute. No idempotency
  mechanism exists anywhere in the codebase today (nothing has needed one yet).
- **Replay protection:** covered by idempotency above for actions; for reads, not applicable.
- **Trading action authorization:** this is exactly what finding #3 (§4, §18) is preparing the
  ground for — every trading endpoint must declare and enforce a specific permission
  (`orders.create`, `orders.cancel`, etc. — already present in the seeded catalog,
  `auth/seed.py:8-30`) via the not-yet-built `require_permission()` dependency.

---

# Secrets I Need To Create

No values below — names, purpose, and handling only.

## Required NOW

**NAME:** `HERMES_ALLOWED_RETURN_URIS` *(not secret, but required config — listed here because
it's currently missing and blocking)*
**PURPOSE:** Open-redirect allowlist for post-login destinations
**WHERE IT SHOULD LIVE:** `/opt/hermes-v2/.env` on ROMEO
**WHO SHOULD HAVE ACCESS:** Whoever operates ROMEO (this is config, not a secret — no access
restriction needed beyond normal host access)
**ROTATION:** N/A — update when frontend URLs change
**STATUS:** MISSING (confirmed)

**NAME:** `HERMES_COOKIE_SECURE`, `HERMES_ALLOWED_ORIGINS` *(config, not secrets)*
**PURPOSE:** Correct session-cookie and CORS behavior in production
**WHERE IT SHOULD LIVE:** `/opt/hermes-v2/.env` on ROMEO
**WHO SHOULD HAVE ACCESS:** ROMEO operator
**ROTATION:** N/A
**STATUS:** UNKNOWN (cannot verify from this environment — likely missing/default, needs a direct
check)

**NAME:** `POSTGRES_PASSWORD` (production value)
**PURPOSE:** Database authentication
**WHERE IT SHOULD LIVE:** `/opt/hermes-v2/.env` on ROMEO only, never in git
**WHO SHOULD HAVE ACCESS:** ROMEO operator(s) only
**ROTATION:** Rotate now if it matches the dev-placeholder-strength value found in this repo's
local `.env`/`.env.dev`; otherwise no immediate action, but establish a rotation cadence
**STATUS:** UNKNOWN — needs direct verification on ROMEO

## Required Before Trading

**NAME:** `BINANCE_API_KEY` / `BINANCE_API_SECRET`
**PURPOSE:** Authenticate Hermes's trading requests to Binance
**WHERE IT SHOULD LIVE:** Production host env (or a secrets manager, if one gets introduced before
this point) — never in git, never in the frontend
**WHO SHOULD HAVE ACCESS:** Backend process only; no human should need routine access after
initial setup
**ROTATION:** Define a cadence (e.g. quarterly, or on any suspected exposure) before creation
**STATUS:** MISSING — do not create until trading is actually being implemented, per this
audit's scope

**NAME:** A Binance-side "withdrawal disabled" + IP-allowlist configuration
**PURPOSE:** Bound the blast radius of a leaked trading key to "can trade" not "can withdraw funds"
**WHERE IT SHOULD LIVE:** Binance account console (not a Hermes secret at all, but a prerequisite)
**WHO SHOULD HAVE ACCESS:** Whoever administers the Binance account
**ROTATION:** N/A (a setting, not a secret)
**STATUS:** MISSING — must be configured before the API key above is even created

**NAME:** An encryption-at-rest key for any Binance credential stored in Postgres (only needed if
Hermes ever stores per-user trading credentials rather than one operator-level key)
**PURPOSE:** Protect trading secrets if they ever live in the database rather than only in the
backend's process environment
**WHERE IT SHOULD LIVE:** Not yet decided — depends on whether Hermes ends up with one trading
identity or many
**WHO SHOULD HAVE ACCESS:** Backend process only
**ROTATION:** Define before creation
**STATUS:** MISSING — design question, not yet needed

## Optional

**NAME:** A dedicated secrets manager (e.g. a self-hosted Vault, or even Docker secrets instead of
plain env files)
**PURPOSE:** Reduce the blast radius of a compromised host reading `.env` in plaintext
**WHERE IT SHOULD LIVE:** N/A — infrastructure decision
**WHO SHOULD HAVE ACCESS:** N/A
**ROTATION:** N/A
**STATUS:** Not present today; current plain-env-file approach is a reasonable tradeoff at this
scale and should be revisited specifically when Binance secrets enter the picture (§22), not
necessarily before

**NAME:** `GITHUB_TOKEN` scoping / GitHub Environment protection rules for the publish workflows
**PURPOSE:** Require a manual approval gate before an image reaches `:latest` in GHCR
**WHERE IT SHOULD LIVE:** GitHub repo settings (Environments), not a secret value
**WHO SHOULD HAVE ACCESS:** Repo admins configure it; approvers use it per-deploy
**ROTATION:** N/A
**STATUS:** Not configured — optional defense-in-depth (§11, finding #14)
