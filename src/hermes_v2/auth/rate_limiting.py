"""In-process rate limiting for Hermes v2's public endpoints.

Mirrors `OAuthStateStore`'s approach (auth/oauth.py): thread-safe in-memory
state guarded by a lock, scoped to a single process. This deployment runs
uvicorn without `--workers` (see runtime.py), so a single process already
matches production — the same constraint OAuthStateStore already accepts.
Before a multi-worker or multi-replica deployment, both would need to move
to a shared store (e.g. Postgres or Redis, if one gets introduced).

No new dependency: a sliding-window log per (scope, client) key is enough
at Hermes's current scale, and keeps this consistent with the rest of the
auth module rather than introducing a second state-management pattern.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

RateLimitDependency = Callable[[Request], Awaitable[None]]

_RATE_LIMIT_MESSAGE = "Too many requests. Please try again later."


class SlidingWindowRateLimiter:
    """Thread-safe, in-process sliding-window request limiter.

    Only *allowed* requests are recorded — a client that keeps retrying
    while blocked does not push its own unblock time further out, and
    memory per key is bounded by `max_requests` regardless of how many
    times a blocked client retries.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Record a hit for `key` if allowed.

        Returns `(allowed, retry_after_seconds)` — `retry_after_seconds` is
        `0.0` when allowed, otherwise the time until the oldest hit in the
        current window ages out.
        """
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.max_requests:
                retry_after = hits[0] + self.window_seconds - now
                return False, max(retry_after, 0.0)

            hits.append(now)
            return True, 0.0

    def reset(self) -> None:
        """Clear all tracked state. Test-only — production never calls this."""
        with self._lock:
            self._hits.clear()


def _trust_proxy_headers() -> bool:
    """Whether to trust `X-Forwarded-For` for the caller's IP.

    Defaults to `False`: `request.client.host` (the direct TCP peer) is
    used unless a deployment explicitly opts in via
    `HERMES_TRUST_PROXY_HEADERS=true`. Only enable this when the app is
    reachable exclusively through a single trusted reverse proxy that sets
    this header itself (e.g. Tailscale Serve bound to a loopback-only
    upstream — see SECURITY_AUDIT.md's Docker findings on verifying that
    binding) — otherwise a direct caller can forge `X-Forwarded-For` to
    reset its own rate limit for free.
    """
    value = os.environ.get("HERMES_TRUST_PROXY_HEADERS", "false")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _client_identifier(request: Request) -> str:
    if _trust_proxy_headers():
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            first_hop = forwarded_for.split(",")[0].strip()
            if first_hop:
                return first_hop

    if request.client is None:
        return "unknown"
    return request.client.host


def rate_limit(limiter: SlidingWindowRateLimiter, scope: str) -> RateLimitDependency:
    """Build a FastAPI dependency enforcing `limiter` for one logical scope.

    `scope` namespaces the limiter's keys so the same limiter instance
    could, in principle, be shared; in practice each protected endpoint in
    `api/app.py` gets its own limiter instance so policies stay fully
    independent per endpoint.

    The 429 response body is a single generic message regardless of scope
    or how close to the limit the caller was — it never reveals the limit,
    the window, or the caller's computed identifier.
    """

    async def dependency(request: Request) -> None:
        client_id = _client_identifier(request)
        allowed, retry_after = limiter.check(f"{scope}:{client_id}")
        if not allowed:
            retry_after_seconds = max(1, math.ceil(retry_after))
            raise HTTPException(
                status_code=429,
                detail=_RATE_LIMIT_MESSAGE,
                headers={"Retry-After": str(retry_after_seconds)},
            )

    dependency.__hermes_rate_limit_scope__ = scope  # type: ignore[attr-defined]
    return dependency


# --- Hermes's concrete per-endpoint policy ---------------------------------
#
# Login and callback get the tightest limits: they're the highest-value
# target (each callback hit does a Google token exchange + a DB write) and
# have no legitimate reason to be called more than a handful of times a
# minute by one real user, even accounting for retries after a cancelled
# consent screen. /auth/me is called on every app boot/reload and needs
# more headroom. /auth/logout is rare but still a mutating, DB-touching
# endpoint, so it isn't left unlimited either.

LOGIN_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)
CALLBACK_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)
ME_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=60, window_seconds=60)
LOGOUT_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)

_ALL_RATE_LIMITERS = (
    LOGIN_RATE_LIMITER,
    CALLBACK_RATE_LIMITER,
    ME_RATE_LIMITER,
    LOGOUT_RATE_LIMITER,
)


def reset_all_rate_limiters() -> None:
    """Clear every limiter's state. Test-only, for isolation between tests."""
    for limiter in _ALL_RATE_LIMITERS:
        limiter.reset()


__all__ = [
    "CALLBACK_RATE_LIMITER",
    "LOGIN_RATE_LIMITER",
    "LOGOUT_RATE_LIMITER",
    "ME_RATE_LIMITER",
    "SlidingWindowRateLimiter",
    "rate_limit",
    "reset_all_rate_limiters",
]
