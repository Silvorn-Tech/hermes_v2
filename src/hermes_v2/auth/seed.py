"""Idempotent seed data for protected authorization records."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.auth.models import Permission, Role

PERMISSION_CATALOG = {
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


def seed_authorization_data(session: Session) -> None:
    """Create the permission catalog and protected SUPER_ADMIN role if absent."""
    permissions_by_name = {
        permission.name: permission
        for permission in session.scalars(select(Permission)).all()
    }
    for name, description in PERMISSION_CATALOG.items():
        if name not in permissions_by_name:
            permission = Permission(name=name, description=description)
            session.add(permission)
            permissions_by_name[name] = permission

    super_admin = session.scalar(select(Role).where(Role.name == "SUPER_ADMIN"))
    if super_admin is None:
        super_admin = Role(
            name="SUPER_ADMIN",
            description="Protected system administrator role.",
            system_role=True,
        )
        session.add(super_admin)

    session.flush()
    super_admin.permissions = list(permissions_by_name.values())
