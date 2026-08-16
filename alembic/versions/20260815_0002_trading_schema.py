"""Create trading schema: orders, order_events, idempotency_keys, audit_log.

Revision ID: 20260815_0002
Revises: 20260815_0001
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0002"
down_revision: str | None = "20260815_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

order_side = postgresql.ENUM("BUY", "SELL", name="order_side", create_type=False)
order_type = postgresql.ENUM("MARKET", "LIMIT", name="order_type", create_type=False)
order_status = postgresql.ENUM(
    "PENDING",
    "NEW",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "PENDING_CANCEL",
    "REJECTED",
    "EXPIRED",
    "FAILED",
    name="order_status",
    create_type=False,
)
order_event_type = postgresql.ENUM(
    "VALIDATION_FAILED",
    "RISK_REJECTED",
    "SUBMITTED",
    "BINANCE_ACK",
    "BINANCE_ERROR",
    "RECONCILED",
    "CANCEL_REQUESTED",
    "CANCELED",
    name="order_event_type",
    create_type=False,
)
audit_result = postgresql.ENUM(
    "SUCCESS", "REJECTED", "FAILED", name="audit_result", create_type=False
)


def upgrade() -> None:
    """Create the trading order-lifecycle, idempotency, and audit schema."""
    order_side.create(op.get_bind(), checkfirst=True)
    order_type.create(op.get_bind(), checkfirst=True)
    order_status.create(op.get_bind(), checkfirst=True)
    order_event_type.create(op.get_bind(), checkfirst=True)
    audit_result.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", order_side, nullable=False),
        sa.Column("order_type", order_type, nullable=False),
        sa.Column("status", order_status, nullable=False, server_default="PENDING"),
        sa.Column(
            "requested_quantity", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column("requested_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column(
            "executed_quantity",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "average_fill_price", sa.Numeric(precision=28, scale=10), nullable=True
        ),
        sa.Column("binance_order_id", sa.String(length=64), nullable=True),
        sa.Column("binance_client_order_id", sa.String(length=36), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
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
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("binance_client_order_id"),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"], unique=False)
    op.create_index("ix_orders_symbol", "orders", ["symbol"], unique=False)
    op.create_index(
        "ix_orders_binance_order_id", "orders", ["binance_order_id"], unique=False
    )
    op.create_index(
        "ix_orders_binance_client_order_id",
        "orders",
        ["binance_client_order_id"],
        unique=False,
    )

    op.create_table(
        "order_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", order_event_type, nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_order_events_order_id", "order_events", ["order_id"], unique=False
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "endpoint", "idempotency_key", name="uq_idempotency_key"
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("result", audit_result, nullable=False),
        sa.Column("hermes_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("binance_order_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"], unique=False)
    op.create_index(
        "ix_audit_log_created_at", "audit_log", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Drop the trading order-lifecycle, idempotency, and audit schema."""
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_table("idempotency_keys")

    op.drop_index("ix_order_events_order_id", table_name="order_events")
    op.drop_table("order_events")

    op.drop_index("ix_orders_binance_client_order_id", table_name="orders")
    op.drop_index("ix_orders_binance_order_id", table_name="orders")
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_table("orders")

    audit_result.drop(op.get_bind(), checkfirst=True)
    order_event_type.drop(op.get_bind(), checkfirst=True)
    order_status.drop(op.get_bind(), checkfirst=True)
    order_type.drop(op.get_bind(), checkfirst=True)
    order_side.drop(op.get_bind(), checkfirst=True)
