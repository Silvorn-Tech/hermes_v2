"""Create bots schema: bots, bot_positions, and orders.bot_id.

Revision ID: 20260816_0002
Revises: 20260816_0001
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0002"
down_revision: str | None = "20260816_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

bot_risk_profile = postgresql.ENUM(
    "SENTINEL", "EQUILIBRIUM", "VORTEX", name="bot_risk_profile", create_type=False
)
bot_asset_class = postgresql.ENUM(
    "CRYPTO", "EQUITY", name="bot_asset_class", create_type=False
)
bot_execution_venue = postgresql.ENUM(
    "BINANCE", name="bot_execution_venue", create_type=False
)
bot_status = postgresql.ENUM(
    "ACTIVE",
    "PAUSING",
    "PAUSED",
    "RESUMING",
    "STOPPED",
    "ERROR",
    name="bot_status",
    create_type=False,
)


def upgrade() -> None:
    """Create the bots/bot_positions tables and link orders to bots."""
    bot_risk_profile.create(op.get_bind(), checkfirst=True)
    bot_asset_class.create(op.get_bind(), checkfirst=True)
    bot_execution_venue.create(op.get_bind(), checkfirst=True)
    bot_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "bots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("risk_profile", bot_risk_profile, nullable=False),
        sa.Column("asset_class", bot_asset_class, nullable=False),
        sa.Column("execution_venue", bot_execution_venue, nullable=False),
        sa.Column("instrument", sa.String(length=20), nullable=False),
        sa.Column("strategy_model", sa.String(length=50), nullable=True),
        sa.Column("strategy_config", sa.JSON(), nullable=True),
        sa.Column("status", bot_status, nullable=False, server_default="PAUSED"),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bots_user_id", "bots", ["user_id"], unique=False)

    op.create_table(
        "bot_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument", sa.String(length=20), nullable=False),
        sa.Column(
            "current_quantity",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "target_quantity", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column("last_close_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_open_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_close_order_id"], ["orders.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_open_order_id"], ["orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_positions_bot_id", "bot_positions", ["bot_id"], unique=True)

    op.add_column(
        "orders", sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_orders_bot_id_bots",
        "orders",
        "bots",
        ["bot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_orders_bot_id", "orders", ["bot_id"], unique=False)


def downgrade() -> None:
    """Drop orders.bot_id and the bots/bot_positions tables."""
    op.drop_index("ix_orders_bot_id", table_name="orders")
    op.drop_constraint("fk_orders_bot_id_bots", "orders", type_="foreignkey")
    op.drop_column("orders", "bot_id")

    op.drop_index("ix_bot_positions_bot_id", table_name="bot_positions")
    op.drop_table("bot_positions")

    op.drop_index("ix_bots_user_id", table_name="bots")
    op.drop_table("bots")

    bot_status.drop(op.get_bind(), checkfirst=True)
    bot_execution_venue.drop(op.get_bind(), checkfirst=True)
    bot_asset_class.drop(op.get_bind(), checkfirst=True)
    bot_risk_profile.drop(op.get_bind(), checkfirst=True)
