"""Create portfolio_snapshots — the persisted history behind the
Dashboard's equity/performance chart.

Revision ID: 20260816_0004
Revises: 20260816_0003
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0004"
down_revision: str | None = "20260816_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the portfolio_snapshots table."""
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_asset", sa.String(length=10), nullable=False),
        sa.Column(
            "total_value_quote", sa.Numeric(precision=28, scale=10), nullable=False
        ),
        sa.Column(
            "available_balance_quote",
            sa.Numeric(precision=28, scale=10),
            nullable=False,
        ),
        sa.Column("exposure_quote", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("exposure_pct", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_at", name="uq_portfolio_snapshots_snapshot_at"),
    )
    op.create_index(
        "ix_portfolio_snapshots_snapshot_at",
        "portfolio_snapshots",
        ["snapshot_at"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the portfolio_snapshots table."""
    op.drop_index(
        "ix_portfolio_snapshots_snapshot_at", table_name="portfolio_snapshots"
    )
    op.drop_table("portfolio_snapshots")
