"""PostgreSQL integration tests for the initial Super Admin bootstrap."""

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.bootstrap import (
    BootstrapConfigurationError,
    bootstrap_super_admin,
)
from hermes_v2.auth.models import Identity, Role, User, UserStatus, user_roles
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.database.connection import create_engine_from_environment

pytestmark = pytest.mark.database


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE role_permissions, user_roles, identities, "
                "permissions, roles, users"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        seed_authorization_data(database_session)
        database_session.commit()
        yield database_session
        database_session.rollback()
    engine.dispose()


def configure_admin(
    monkeypatch: pytest.MonkeyPatch, value: str = "admin@example.com"
) -> None:
    """Configure a test-only administrator email."""
    monkeypatch.setenv("HERMES_ADMIN_EMAIL", value)


def test_missing_admin_email_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_ADMIN_EMAIL", raising=False)

    with pytest.raises(BootstrapConfigurationError):
        bootstrap_super_admin(session)


def test_invalid_admin_email_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_admin(monkeypatch, "not-an-email")

    with pytest.raises(BootstrapConfigurationError):
        bootstrap_super_admin(session)


def test_bootstrap_creates_normalized_admin_with_super_admin_role(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_admin(monkeypatch, "  ADMIN@EXAMPLE.COM ")

    bootstrap_super_admin(session)

    user = session.scalar(select(User).where(User.email == "admin@example.com"))
    assert user is not None
    assert user.status is UserStatus.ACTIVE
    assert [role.name for role in user.roles] == ["SUPER_ADMIN"]
    assert session.scalar(select(func.count()).select_from(Identity)) == 0


def test_bootstrap_is_idempotent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_admin(monkeypatch)

    bootstrap_super_admin(session)
    bootstrap_super_admin(session)

    assert session.scalar(select(func.count()).select_from(User)) == 1
    assert session.scalar(select(func.count()).select_from(user_roles)) == 1


def test_bootstrap_reuses_existing_user_and_preserves_other_roles(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_admin(monkeypatch)
    user = User(email="admin@example.com")
    custom_role = Role(name="CUSTOM_ROLE")
    user.roles.append(custom_role)
    session.add(user)
    session.commit()

    bootstrap_super_admin(session)

    assert session.scalar(select(func.count()).select_from(User)) == 1
    assert {role.name for role in user.roles} == {"CUSTOM_ROLE", "SUPER_ADMIN"}


def test_database_failure_rolls_back_new_user(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_admin(monkeypatch)
    original_flush = session.flush

    def fail_flush() -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(session, "flush", fail_flush)

    with pytest.raises(RuntimeError, match="simulated database failure"):
        bootstrap_super_admin(session)

    monkeypatch.setattr(session, "flush", original_flush)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(User)) == 0
