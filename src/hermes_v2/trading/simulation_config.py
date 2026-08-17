"""Simulation Mode configuration — read at call time from the environment,
never cached (same rotation model as every other `HERMES_*` tunable, see
`risk_engine.py`). These are operator-level *defaults*, not secrets:
`SimulationAccount.initial_capital_quote` is frozen per-account at
creation time from whatever `default_simulation_initial_capital()`
returns then, and never re-read afterward.

No fee/slippage modeling exists anywhere else in Hermes today (`Order`
has no fee column, `OrderValidator` doesn't model fees) — these two
rates default to `0`, a documented v1 limitation
(`docs/architecture/simulation.md`), not an invented "reasonable"
number.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

_DEFAULT_INITIAL_CAPITAL_QUOTE = Decimal("10000")
_DEFAULT_QUOTE_ASSET = "USDT"
_DEFAULT_FEE_RATE_PCT = Decimal("0")
_DEFAULT_SLIPPAGE_RATE_PCT = Decimal("0")


def _parse_decimal_env(name: str, default: Decimal) -> Decimal:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return Decimal(raw_value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc


def default_simulation_initial_capital() -> Decimal:
    """`HERMES_SIMULATION_INITIAL_CAPITAL_USD`, default `10000`. A new
    `SimulationAccount` is seeded with this value at bot-creation time;
    changing this env var afterward never affects an already-created
    account."""
    return _parse_decimal_env(
        "HERMES_SIMULATION_INITIAL_CAPITAL_USD", _DEFAULT_INITIAL_CAPITAL_QUOTE
    )


def default_simulation_quote_asset() -> str:
    return _DEFAULT_QUOTE_ASSET


def simulation_fee_rate_pct() -> Decimal:
    """`HERMES_SIMULATION_FEE_RATE_PCT`, default `0`. Applied to a
    simulated fill's notional value; see `SimulationOrderService`."""
    return _parse_decimal_env("HERMES_SIMULATION_FEE_RATE_PCT", _DEFAULT_FEE_RATE_PCT)


def simulation_slippage_rate_pct() -> Decimal:
    """`HERMES_SIMULATION_SLIPPAGE_RATE_PCT`, default `0`. Applied to the
    real market price to produce a simulated fill price — worse for the
    account regardless of side (higher for a BUY, lower for a SELL), the
    same direction real slippage would move against a trader."""
    return _parse_decimal_env(
        "HERMES_SIMULATION_SLIPPAGE_RATE_PCT", _DEFAULT_SLIPPAGE_RATE_PCT
    )


__all__ = [
    "default_simulation_initial_capital",
    "default_simulation_quote_asset",
    "simulation_fee_rate_pct",
    "simulation_slippage_rate_pct",
]
