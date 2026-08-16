"""Add positions.read/positions.close to the permission catalog, grant to
SUPER_ADMIN — mirrors the seeding pattern from 20260814_0001.

Revision ID: 20260815_0003
Revises: 20260815_0002
Create Date: 2026-08-15
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0003"
down_revision: str | None = "20260815_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same namespace as 20260814_0001_auth_foundation.py, so these IDs are
# deterministic and stable if this migration is ever replayed.
seed_namespace = uuid.UUID("6d215b42-5144-43ac-9299-5092d82a4930")
new_permissions = {
    "positions.read": "Read derived Binance Spot positions.",
    "positions.close": "Close a position (submits a market sell order).",
}


def upgrade() -> None:
    """Seed the two new permissions and grant them to SUPER_ADMIN."""
    connection = op.get_bind()
    permissions_table = sa.table(
        "permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("permission_id", postgresql.UUID(as_uuid=True)),
    )
    roles_table = sa.table(
        "roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
    )

    super_admin_id = connection.execute(
        sa.select(roles_table.c.id).where(roles_table.c.name == "SUPER_ADMIN")
    ).scalar_one()

    permission_ids = {
        name: uuid.uuid5(seed_namespace, f"permission:{name}")
        for name in new_permissions
    }
    op.bulk_insert(
        permissions_table,
        [
            {"id": permission_ids[name], "name": name, "description": description}
            for name, description in new_permissions.items()
        ],
    )
    op.bulk_insert(
        role_permissions_table,
        [
            {"role_id": super_admin_id, "permission_id": permission_id}
            for permission_id in permission_ids.values()
        ],
    )


def downgrade() -> None:
    """Remove the two new permissions and their SUPER_ADMIN grant."""
    connection = op.get_bind()
    permissions_table = sa.table(
        "permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("permission_id", postgresql.UUID(as_uuid=True)),
    )
    permission_ids = [
        uuid.uuid5(seed_namespace, f"permission:{name}") for name in new_permissions
    ]
    connection.execute(
        role_permissions_table.delete().where(
            role_permissions_table.c.permission_id.in_(permission_ids)
        )
    )
    connection.execute(
        permissions_table.delete().where(permissions_table.c.id.in_(permission_ids))
    )
