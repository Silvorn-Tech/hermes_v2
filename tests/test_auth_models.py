"""PostgreSQL integration tests for the authentication domain."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import Identity, Permission, Role, User
from hermes_v2.auth.seed import PERMISSION_CATALOG, seed_authorization_data
from hermes_v2.database.connection import create_engine_from_environment

pytestmark = pytest.mark.database


def test_auth_models_package_exports_expected_symbols_and_metadata() -> None:
    from hermes_v2.auth.models import (
        Identity,
        Permission,
        Role,
        Session,
        User,
        role_permissions,
        user_roles,
    )
    from hermes_v2.auth.models.user import User as UserFromModule
    from hermes_v2.auth.models.identity import Identity as IdentityFromModule
    from hermes_v2.auth.models.role import Role as RoleFromModule
    from hermes_v2.auth.models.permission import Permission as PermissionFromModule
    from hermes_v2.auth.models.session import Session as SessionFromModule

    assert User is UserFromModule
    assert Identity is IdentityFromModule
    assert Role is RoleFromModule
    assert Permission is PermissionFromModule
    assert Session is SessionFromModule

    expected_tables = {
        "users",
        "identities",
        "roles",
        "permissions",
        "sessions",
        "user_roles",
        "role_permissions",
    }
    assert {
        table.name for table in User.__table__.metadata.tables.values()
    } >= expected_tables
    assert user_roles.name == "user_roles"
    assert role_permissions.name == "role_permissions"


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE role_permissions, user_roles, identities, "
                "sessions, permissions, roles, users"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def test_users_can_be_created_and_emails_are_unique(session: Session) -> None:
    session.add(User(email="user@example.com"))
    session.commit()

    session.add(User(email="user@example.com"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_identities_belong_to_users_and_are_unique(session: Session) -> None:
    first_user = User(email="first@example.com")
    second_user = User(email="second@example.com")
    first_user.identities.append(
        Identity(provider="google", provider_subject="google-subject")
    )
    first_user.identities.append(
        Identity(provider="telegram", provider_subject="123456")
    )
    session.add_all([first_user, second_user])
    session.commit()

    assert len(first_user.identities) == 2

    session.add(
        Identity(
            user_id=second_user.id, provider="google", provider_subject="google-subject"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_users_roles_and_permissions_are_many_to_many(session: Session) -> None:
    user = User(email="admin@example.com")
    role = Role(name="CUSTOM_ROLE")
    permission = Permission(name="dashboard.read")
    user.roles.append(role)
    role.permissions.append(permission)
    session.add(user)
    session.commit()

    assert user.roles == [role]
    assert role.permissions == [permission]


def test_duplicate_user_role_assignment_is_prevented(session: Session) -> None:
    user = User(email="user@example.com")
    role = Role(name="CUSTOM_ROLE")
    session.add_all([user, role])
    session.commit()

    session.execute(
        text("INSERT INTO user_roles (user_id, role_id) VALUES (:user_id, :role_id)"),
        {"user_id": user.id, "role_id": role.id},
    )
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) VALUES (:user_id, :role_id)"
            ),
            {"user_id": user.id, "role_id": role.id},
        )
        session.commit()


def test_duplicate_role_permission_assignment_is_prevented(session: Session) -> None:
    role = Role(name="CUSTOM_ROLE")
    permission = Permission(name="dashboard.read")
    session.add_all([role, permission])
    session.commit()

    values = {"role_id": role.id, "permission_id": permission.id}
    session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "VALUES (:role_id, :permission_id)"
        ),
        values,
    )
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "VALUES (:role_id, :permission_id)"
            ),
            values,
        )
        session.commit()


def test_seed_creates_protected_super_admin_and_permissions(session: Session) -> None:
    seed_authorization_data(session)
    session.commit()

    super_admin = session.query(Role).filter_by(name="SUPER_ADMIN").one()
    assert super_admin.system_role is True
    assert {permission.name for permission in super_admin.permissions} == set(
        PERMISSION_CATALOG
    )
