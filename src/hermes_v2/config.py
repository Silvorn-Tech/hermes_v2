"""Cross-cutting environment configuration shared by more than one module.

Domain-specific config stays colocated with its own module (session cookies
in `auth/session.py`, risk limits in `trading/risk_engine.py`, and so on).
This module exists only for the rare case where two otherwise-unrelated
modules need the exact same value — today, `HERMES_ALLOWED_ORIGINS`, read by
both `api/app.py`'s CORS middleware and `trading/origin_check.py`'s CSRF
guard. Before this module existed, each had its own copy of the same
parsing logic, which is exactly the kind of duplication that silently
diverges on a future edit — a config module and a CSRF check reading
different origin allowlists is worse than either reading none.
"""

from __future__ import annotations

import os


def configured_allowed_origins() -> list[str]:
    """Frontend origins Hermes trusts for both CORS and the trading
    Origin-header CSRF check. Empty when unset, which makes both fail
    closed rather than defaulting to permissive."""
    raw_value = os.environ.get("HERMES_ALLOWED_ORIGINS", "")
    return [entry.strip() for entry in raw_value.split(",") if entry.strip()]


__all__ = ["configured_allowed_origins"]
