"""Per-user Simulation risk limits, self-service with working defaults.

`user_trading_settings` already holds each user's **real-order** risk
limits (nullable, fail-closed: `NULL` means "not configured, reject on
that dimension" -- a deliberate safety default, since real money is at
stake and an operator/user must consciously choose their own limits
before Hermes will place a real order). Simulation deliberately stayed
on a *global*, env-var-only `HERMES_RISK_*` configuration instead
(`docs/architecture/multi-tenant-trading.md` #2) specifically so a new
user's Simulation "just works" with no setup -- but that only holds if
an operator has actually set those six env vars on the deployment; if
not (as happened on ROMEO), Simulation fails closed for *everyone*,
with no way for a user to fix it themselves.

This migration adds a parallel set of `simulation_*` columns, `NOT
NULL` with sensible `server_default`s (unlike the nullable real-order
columns) -- every row, whether created by this feature or any other
existing per-user-settings write path (the personal trading switch,
the real risk-limits form), gets working Simulation defaults for free.
A user can then override them from Settings exactly like their
real-order limits, without ever being blocked waiting on operator
access to the server. Real-order columns and their fail-closed-on-NULL
semantics are completely untouched.

Revision ID: 20260818_0001
Revises: 20260817_0002
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260818_0001"
down_revision: str | None = "20260817_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Deliberately more generous than the operator-chosen ROMEO env values
# from this same investigation, since this is the shared out-of-the-box
# default for every user's *virtual* money, not one operator's real
# account -- see hermes_v2's user_risk_settings_service.py for the
# matching Python-side constant these must stay in sync with.
_DEFAULT_MAX_ORDER_NOTIONAL_QUOTE = "1000"
_DEFAULT_MAX_SYMBOL_EXPOSURE_PCT = "50"
_DEFAULT_MAX_TOTAL_EXPOSURE_PCT = "100"
_DEFAULT_MAX_DAILY_LOSS_PCT = "20"
_DEFAULT_MAX_OPEN_POSITIONS = "5"
_DEFAULT_ALLOWED_SYMBOLS = "{BTCUSDT,ETHUSDT}"


def upgrade() -> None:
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_max_order_notional_quote",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default=_DEFAULT_MAX_ORDER_NOTIONAL_QUOTE,
        ),
    )
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_max_symbol_exposure_pct",
            sa.Numeric(precision=6, scale=3),
            nullable=False,
            server_default=_DEFAULT_MAX_SYMBOL_EXPOSURE_PCT,
        ),
    )
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_max_total_exposure_pct",
            sa.Numeric(precision=6, scale=3),
            nullable=False,
            server_default=_DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
        ),
    )
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_max_daily_loss_pct",
            sa.Numeric(precision=6, scale=3),
            nullable=False,
            server_default=_DEFAULT_MAX_DAILY_LOSS_PCT,
        ),
    )
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_max_open_positions",
            sa.Integer(),
            nullable=False,
            server_default=_DEFAULT_MAX_OPEN_POSITIONS,
        ),
    )
    op.add_column(
        "user_trading_settings",
        sa.Column(
            "simulation_allowed_symbols",
            sa.ARRAY(sa.String(length=20)),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_ALLOWED_SYMBOLS}'::varchar[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_trading_settings", "simulation_allowed_symbols")
    op.drop_column("user_trading_settings", "simulation_max_open_positions")
    op.drop_column("user_trading_settings", "simulation_max_daily_loss_pct")
    op.drop_column("user_trading_settings", "simulation_max_total_exposure_pct")
    op.drop_column("user_trading_settings", "simulation_max_symbol_exposure_pct")
    op.drop_column("user_trading_settings", "simulation_max_order_notional_quote")
