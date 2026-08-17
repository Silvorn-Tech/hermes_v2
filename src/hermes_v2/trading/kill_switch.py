"""The two-tier kill switch every order-placing call site should check.

Two independent switches, both required (`AND`, never `OR`):

- The **global** switch (`trading.config.is_trading_enabled()`) — the
  platform's one real emergency stop, unchanged by multi-tenancy: an
  env var, set by hand on the host, defaults to disabled, never touched
  by code. See `trading/config.py`'s own docstring.
- Each user's **personal** switch (`user_risk_settings_service`) — a
  self-service convenience pause, defaults enabled, lets one user stop
  their own trading without affecting anyone else's.

`is_trading_permitted()` is the one function `OrderService` and
`SimulationOrderService` should call instead of the bare global
`is_trading_enabled()` — kept in its own module (not folded into
`trading/config.py`, which stays a pure, zero-I/O env reader, or into
`user_risk_settings_service.py`, which has no reason to know about the
global switch) so each half stays independently testable and the
combination logic lives in exactly one place.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from hermes_v2.trading.config import is_trading_enabled
from hermes_v2.trading.user_risk_settings_service import is_user_trading_enabled


def is_trading_permitted(session: Session, user_id: uuid.UUID) -> bool:
    """`False` if either switch is off. Order matters only for cost: the
    global check is a plain env read, so it runs first and skips the
    database round-trip entirely once the platform-wide switch is off."""
    if not is_trading_enabled():
        return False
    return is_user_trading_enabled(session, user_id)


__all__ = ["is_trading_permitted"]
