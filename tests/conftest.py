"""Shared pytest fixtures for the Hermes v2 test suite."""

from __future__ import annotations

import pytest

from hermes_v2.auth.rate_limiting import reset_all_rate_limiters
from hermes_v2.trading.rate_limiting import reset_trading_rate_limiters


@pytest.fixture(autouse=True)
def _reset_hermes_rate_limits() -> None:
    """Rate limiter state is a module-level, in-process singleton (mirrors
    OAuthStateStore) shared by every request the test app handles —
    including requests from unrelated tests, since Starlette's TestClient
    reports every request as coming from the same fake client ("testclient")
    by default. Without resetting between tests, a test late in the suite
    could get 429'd by requests earlier tests already made under the same
    apparent IP. This runs for every test, not just rate-limiting ones,
    since any test may exercise a rate-limited route incidentally.
    """
    reset_all_rate_limiters()
    reset_trading_rate_limiters()
