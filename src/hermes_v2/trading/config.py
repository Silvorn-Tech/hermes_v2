"""Trading-wide configuration read from the environment.

Same idiom as `auth/session.py`'s `is_cookie_secure()`: no config module, no
`pydantic-settings`, a small function reading one env var at the point of
use, matching every other boolean toggle in this codebase.
"""

from __future__ import annotations

import os

_FALSY_VALUES = {"", "0", "false", "no", "off"}


def is_trading_enabled() -> bool:
    """The global kill switch. Defaults to disabled.

    This is checked inside `OrderService`, not only at the API layer, so no
    code path anywhere — a route, a future internal caller, a script run by
    hand — can place or cancel a real order while this is off. Nothing in
    this codebase ever sets `TRADING_ENABLED=true` automatically: not
    `docker compose up`, not a migration, not a deploy. An operator sets it
    by hand on the host after deliberately reviewing what they're enabling.
    """
    value = os.environ.get("TRADING_ENABLED", "false")
    return str(value).strip().lower() not in _FALSY_VALUES


__all__ = ["is_trading_enabled"]
