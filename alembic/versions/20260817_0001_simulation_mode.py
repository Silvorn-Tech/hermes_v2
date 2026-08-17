"""Add Bot.execution_mode and the Simulation (paper trading) virtual
ledger: simulation_accounts, simulation_orders, simulation_snapshots.

Every existing bot is backfilled to SIMULATION — the safest default;
nothing in this deployment's current state depends on a bot being LIVE,
and this guarantees no pre-existing bot can place a real order the
moment this migration lands. There is no code path in this phase that
can set a bot to LIVE.

Revision ID: 20260817_0001
Revises: 20260816_0004
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0001"
down_revision: str | None = "20260816_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

bot_execution_mode = postgresql.ENUM(
    "SIMULATION", "LIVE", name="bot_execution_mode", create_type=False
)
simulation_order_side = postgresql.ENUM(
    "BUY", "SELL", name="simulation_order_side", create_type=False
)
simulation_order_type = postgresql.ENUM(
    "MARKET", "LIMIT", name="simulation_order_type", create_type=False
)
simulation_order_status = postgresql.ENUM(
    "FILLED", "REJECTED", "FAILED", name="simulation_order_status", create_type=False
)


def upgrade() -> None:
    bot_execution_mode.create(op.get_bind(), checkfirst=True)
    simulation_order_side.create(op.get_bind(), checkfirst=True)
    simulation_order_type.create(op.get_bind(), checkfirst=True)
    simulation_order_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "bots",
        sa.Column(
            "execution_mode",
            bot_execution_mode,
            nullable=False,
            server_default="SIMULATION",
        ),
    )

    op.create_table(
        "simulation_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_asset", sa.String(length=10), nullable=False),
        sa.Column(
            "initial_capital_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column(
            "cash_balance_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_simulation_accounts_bot_id",
        "simulation_accounts",
        ["bot_id"],
        unique=True,
    )

    op.create_table(
        "simulation_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "simulation_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", simulation_order_side, nullable=False),
        sa.Column("order_type", simulation_order_type, nullable=False),
        sa.Column("status", simulation_order_status, nullable=False),
        sa.Column(
            "requested_quantity", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column("fill_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column(
            "executed_quantity",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "fee_quote",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "slippage_quote",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default="0",
        ),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["simulation_account_id"], ["simulation_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_simulation_orders_bot_id", "simulation_orders", ["bot_id"], unique=False
    )
    op.create_index(
        "ix_simulation_orders_simulation_account_id",
        "simulation_orders",
        ["simulation_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_simulation_orders_symbol", "simulation_orders", ["symbol"], unique=False
    )

    op.create_table(
        "simulation_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cash_balance_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column(
            "position_value_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column(
            "total_value_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column("exposure_pct", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_id", "snapshot_at", name="uq_simulation_snapshots_bot_snapshot_at"
        ),
    )
    op.create_index(
        "ix_simulation_snapshots_bot_id",
        "simulation_snapshots",
        ["bot_id"],
        unique=False,
    )
    op.create_index(
        "ix_simulation_snapshots_snapshot_at",
        "simulation_snapshots",
        ["snapshot_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_simulation_snapshots_snapshot_at", table_name="simulation_snapshots"
    )
    op.drop_index("ix_simulation_snapshots_bot_id", table_name="simulation_snapshots")
    op.drop_table("simulation_snapshots")

    op.drop_index("ix_simulation_orders_symbol", table_name="simulation_orders")
    op.drop_index(
        "ix_simulation_orders_simulation_account_id", table_name="simulation_orders"
    )
    op.drop_index("ix_simulation_orders_bot_id", table_name="simulation_orders")
    op.drop_table("simulation_orders")

    op.drop_index("ix_simulation_accounts_bot_id", table_name="simulation_accounts")
    op.drop_table("simulation_accounts")

    op.drop_column("bots", "execution_mode")

    simulation_order_status.drop(op.get_bind(), checkfirst=True)
    simulation_order_type.drop(op.get_bind(), checkfirst=True)
    simulation_order_side.drop(op.get_bind(), checkfirst=True)
    bot_execution_mode.drop(op.get_bind(), checkfirst=True)
