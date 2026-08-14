"""Create authentication and authorization foundation.

Revision ID: 20260814_0001
Revises:
Create Date: 2026-08-14
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260814_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_status = postgresql.ENUM(
    "ACTIVE", "DISABLED", name="user_status", create_type=False
)
seed_namespace = uuid.UUID("6d215b42-5144-43ac-9299-5092d82a4930")
permission_catalog = {
    "system.status": "Read system status.",
    "dashboard.read": "Read dashboard data.",
    "users.read": "Read users.",
    "users.manage": "Manage users.",
    "roles.read": "Read roles.",
    "roles.manage": "Manage roles.",
    "permissions.read": "Read permissions.",
    "portfolio.read": "Read portfolio data.",
    "trades.read": "Read trades.",
    "strategies.read": "Read strategies.",
    "strategies.manage": "Manage strategies.",
    "orders.read": "Read orders.",
    "orders.create": "Create orders.",
    "orders.cancel": "Cancel orders.",
    "risk.read": "Read risk data.",
    "risk.manage": "Manage risk settings.",
    "secrets.read": "Read managed secret metadata and values.",
    "secrets.manage": "Manage secrets.",
    "deployments.read": "Read deployment state.",
    "deployments.execute": "Execute deployments.",
    "audit.read": "Read audit records.",
}


def upgrade() -> None:
    """Create the internal identity and RBAC schema."""
    user_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=2048), nullable=True),
        sa.Column("status", user_status, nullable=False),
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
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "system_role", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_subject"),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"], unique=False)
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    super_admin_id = uuid.uuid5(seed_namespace, "role:SUPER_ADMIN")
    permission_ids = {
        name: uuid.uuid5(seed_namespace, f"permission:{name}")
        for name in permission_catalog
    }
    op.bulk_insert(
        sa.table(
            "roles",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("name", sa.String()),
            sa.column("description", sa.Text()),
            sa.column("system_role", sa.Boolean()),
        ),
        [
            {
                "id": super_admin_id,
                "name": "SUPER_ADMIN",
                "description": "Protected system administrator role.",
                "system_role": True,
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("name", sa.String()),
            sa.column("description", sa.Text()),
        ),
        [
            {"id": permission_ids[name], "name": name, "description": description}
            for name, description in permission_catalog.items()
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_id", postgresql.UUID(as_uuid=True)),
            sa.column("permission_id", postgresql.UUID(as_uuid=True)),
        ),
        [
            {"role_id": super_admin_id, "permission_id": permission_id}
            for permission_id in permission_ids.values()
        ],
    )


def downgrade() -> None:
    """Drop the internal identity and RBAC schema."""
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_index("ix_identities_user_id", table_name="identities")
    op.drop_table("identities")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users")
    user_status.drop(op.get_bind(), checkfirst=True)
