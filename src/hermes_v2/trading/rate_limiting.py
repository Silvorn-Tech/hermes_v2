"""Hermes-side rate limits for mutating trading endpoints.

Independent of Binance's own rate limits (see
`hermes_v2.integrations.binance.BinanceRateLimitError` for those) — this is
about bounding how fast *Hermes* lets one caller submit trading actions,
regardless of what Binance would otherwise allow. Reuses the same
`SlidingWindowRateLimiter` the auth routes already use
(`auth/rate_limiting.py`), keyed per caller IP the same way.
"""

from __future__ import annotations

from hermes_v2.auth.rate_limiting import SlidingWindowRateLimiter

# Conservative defaults for a single-operator account, not a tuned business
# threshold like RiskEngine's limits — this is abuse protection, not a risk
# rule, so a reasonable fixed default (unlike RiskEngine, which refuses to
# invent one) is appropriate here.
CREATE_ORDER_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)
CANCEL_ORDER_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)
CLOSE_POSITION_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=20, window_seconds=60
)
BOT_CREATE_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)
BOT_UPDATE_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)
BOT_PAUSE_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)
BOT_RESUME_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)
BOT_STOP_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)
BOT_DELETE_RATE_LIMITER = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)

# Tighter than the others: this one hits real Binance to verify the
# submitted key/secret before anything is persisted, unlike every other
# settings write below (plain DB upserts).
SETTINGS_CREDENTIALS_PUT_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=5, window_seconds=60
)
SETTINGS_CREDENTIALS_DELETE_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=20, window_seconds=60
)
SETTINGS_RISK_LIMITS_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=20, window_seconds=60
)
SETTINGS_SIMULATION_RISK_LIMITS_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=20, window_seconds=60
)
SETTINGS_TRADING_SWITCH_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=20, window_seconds=60
)

_ALL_TRADING_RATE_LIMITERS = (
    CREATE_ORDER_RATE_LIMITER,
    CANCEL_ORDER_RATE_LIMITER,
    CLOSE_POSITION_RATE_LIMITER,
    BOT_CREATE_RATE_LIMITER,
    BOT_UPDATE_RATE_LIMITER,
    BOT_PAUSE_RATE_LIMITER,
    BOT_RESUME_RATE_LIMITER,
    BOT_STOP_RATE_LIMITER,
    BOT_DELETE_RATE_LIMITER,
    SETTINGS_CREDENTIALS_PUT_RATE_LIMITER,
    SETTINGS_CREDENTIALS_DELETE_RATE_LIMITER,
    SETTINGS_RISK_LIMITS_RATE_LIMITER,
    SETTINGS_SIMULATION_RISK_LIMITS_RATE_LIMITER,
    SETTINGS_TRADING_SWITCH_RATE_LIMITER,
)


def reset_trading_rate_limiters() -> None:
    """Clear every trading limiter's state. Test-only, for isolation between
    tests — mirrors `auth.rate_limiting.reset_all_rate_limiters()`."""
    for limiter in _ALL_TRADING_RATE_LIMITERS:
        limiter.reset()


__all__ = [
    "BOT_CREATE_RATE_LIMITER",
    "BOT_DELETE_RATE_LIMITER",
    "BOT_PAUSE_RATE_LIMITER",
    "BOT_RESUME_RATE_LIMITER",
    "BOT_STOP_RATE_LIMITER",
    "BOT_UPDATE_RATE_LIMITER",
    "CANCEL_ORDER_RATE_LIMITER",
    "CLOSE_POSITION_RATE_LIMITER",
    "CREATE_ORDER_RATE_LIMITER",
    "SETTINGS_CREDENTIALS_DELETE_RATE_LIMITER",
    "SETTINGS_CREDENTIALS_PUT_RATE_LIMITER",
    "SETTINGS_RISK_LIMITS_RATE_LIMITER",
    "SETTINGS_SIMULATION_RISK_LIMITS_RATE_LIMITER",
    "SETTINGS_TRADING_SWITCH_RATE_LIMITER",
    "reset_trading_rate_limiters",
]
