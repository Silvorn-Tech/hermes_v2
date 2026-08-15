"""Database-backed session authentication tests for Hermes."""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session as DatabaseSession, sessionmaker

from hermes_v2.auth.models import Session as SessionModel, User, UserStatus
from hermes_v2.auth.session import (
    create_session,
    get_user_from_session,
    revoke_session,
)
from hermes_v2.database.connection import create_engine_from_environment

pytestmark = pytest.mark.database


@pytest.fixture()
def session() -> DatabaseSession:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE sessions, identities, user_roles, role_permissions, "
                "permissions, roles, users"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def test_session_token_is_cryptographically_generated(session: DatabaseSession) -> None:
    user = User(email="session@example.com")
    session.add(user)
    session.commit()

    session_record, raw_token = create_session(session, user, timedelta(minutes=30))

    assert raw_token
    assert len(raw_token) >= 20
    assert session_record.token_hash
    assert session_record.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()


def test_raw_token_is_not_stored_in_database(session: DatabaseSession) -> None:
    user = User(email="token-secret@example.com")
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=30))
    session.commit()

    stored_hash = session.scalar(
        select(SessionModel.token_hash).where(SessionModel.user_id == user.id)
    )

    assert stored_hash is not None
    assert stored_hash != raw_token
    assert stored_hash == hashlib.sha256(raw_token.encode()).hexdigest()


def test_created_session_can_resolve_the_correct_user(
    session: DatabaseSession,
) -> None:
    user = User(email="resolve@example.com")
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=15))
    session.commit()

    resolved_user = get_user_from_session(session, raw_token)

    assert resolved_user is not None
    assert resolved_user.id == user.id
    assert resolved_user.email == user.email


def test_invalid_token_returns_none(session: DatabaseSession) -> None:
    user = User(email="invalid@example.com")
    session.add(user)
    session.commit()

    create_session(session, user, timedelta(minutes=15))
    session.commit()

    invalid_token = "definitely-not-a-valid-session-token"
    assert get_user_from_session(session, invalid_token) is None


def test_expired_session_returns_none(session: DatabaseSession) -> None:
    user = User(email="expired@example.com")
    session.add(user)
    session.commit()

    session_record, raw_token = create_session(session, user, timedelta(minutes=-5))
    session_record.expires_at = session_record.created_at - timedelta(minutes=1)
    session.commit()

    assert get_user_from_session(session, raw_token) is None


def test_revoked_session_returns_none(session: DatabaseSession) -> None:
    user = User(email="revoked@example.com")
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=15))
    session.commit()

    assert revoke_session(session, raw_token) is True
    session.commit()

    assert get_user_from_session(session, raw_token) is None


def test_disabled_user_returns_none(session: DatabaseSession) -> None:
    user = User(email="disabled@example.com", status=UserStatus.DISABLED)
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=15))
    session.commit()

    assert get_user_from_session(session, raw_token) is None


def test_revoke_session_revokes_an_active_session(session: DatabaseSession) -> None:
    user = User(email="revoke@example.com")
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=30))
    session.commit()

    assert revoke_session(session, raw_token) is True
    session.commit()

    revoked_record = session.scalar(
        select(SessionModel).where(SessionModel.user_id == user.id)
    )
    assert revoked_record is not None
    assert revoked_record.revoked_at is not None


def test_revoke_session_does_not_incorrectly_revive_a_revoked_session(
    session: DatabaseSession,
) -> None:
    user = User(email="revoke-idempotent@example.com")
    session.add(user)
    session.commit()

    _, raw_token = create_session(session, user, timedelta(minutes=30))
    session.commit()

    assert revoke_session(session, raw_token) is True
    session.commit()

    assert revoke_session(session, raw_token) is False
    session.commit()

    revoked_record = session.scalar(
        select(SessionModel).where(SessionModel.user_id == user.id)
    )
    assert revoked_record is not None
    assert revoked_record.revoked_at is not None


def test_multiple_sessions_can_exist_for_the_same_user(
    session: DatabaseSession,
) -> None:
    user = User(email="multi@example.com")
    session.add(user)
    session.commit()

    _, first_token = create_session(session, user, timedelta(minutes=15))
    _, second_token = create_session(session, user, timedelta(minutes=45))
    session.commit()

    assert get_user_from_session(session, first_token) is not None
    assert get_user_from_session(session, second_token) is not None

    sessions = session.scalars(
        select(SessionModel).where(SessionModel.user_id == user.id)
    ).all()
    assert len(sessions) == 2


def test_different_generated_sessions_have_different_tokens(
    session: DatabaseSession,
) -> None:
    user = User(email="different@example.com")
    session.add(user)
    session.commit()

    _, first_token = create_session(session, user, timedelta(minutes=15))
    _, second_token = create_session(session, user, timedelta(minutes=15))
    session.commit()

    assert first_token != second_token
