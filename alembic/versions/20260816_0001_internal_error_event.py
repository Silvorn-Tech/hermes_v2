"""Add INTERNAL_ERROR to order_event_type — OrderService's safety-net catch
for an unexpected (non-Binance, non-validation/risk) exception mid-flow
needs an honest event label, not a mislabeled BINANCE_ERROR.

Revision ID: 20260816_0001
Revises: 20260815_0003
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260816_0001"
down_revision: str | None = "20260815_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the INTERNAL_ERROR value to order_event_type."""
    op.execute("ALTER TYPE order_event_type ADD VALUE IF NOT EXISTS 'INTERNAL_ERROR'")


def downgrade() -> None:
    """Postgres cannot drop a single enum value; downgrading this migration
    is a no-op. A real rollback would require recreating the type without
    the value and remapping every column that uses it — not attempted here
    since no downgrade in this codebase's migration history has needed to
    do that either."""
