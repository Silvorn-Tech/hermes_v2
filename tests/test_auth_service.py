"""Tests for Google identity resolution in Hermes auth."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.auth.models import Identity, User, UserStatus
from hermes_v2.auth.service import (
    GoogleIdentityError,
    GoogleUserDisabledError,
    GoogleUserNotFoundError,
    resolve_google_user,
)


@pytest.fixture()
def session() -> Session:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    from hermes_v2.database.connection import Base

    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    with SessionFactory() as database_session:
        yield database_session


def test_resolve_google_user_with_existing_identity_updates_last_login(
    session: Session,
) -> None:
    user = User(email="alice@example.com", status=UserStatus.ACTIVE)
    session.add(user)
    session.flush()
    identity = Identity(
        user_id=user.id,
        provider="google",
        provider_subject="google-sub-1",
    )
    session.add(identity)
    session.commit()

    claims = {
        "sub": "google-sub-1",
        "email": "alice@example.com",
        "name": "Alice Example",
        "picture": "https://example.com/alice.png",
    }

    resolved = resolve_google_user(session, claims)

    assert resolved is user
    session.refresh(user)
    assert user.last_login_at is not None


def test_resolve_google_user_by_email_creates_google_identity(
    session: Session,
) -> None:
    user = User(email="bob@example.com", status=UserStatus.ACTIVE)
    session.add(user)
    session.commit()

    claims = {
        "sub": "google-sub-2",
        "email": "BOB@example.com",
        "email_verified": True,
        "name": "Bob Example",
        "picture": "https://example.com/bob.png",
    }

    resolved = resolve_google_user(session, claims)

    assert resolved is user
    session.refresh(user)
    assert user.last_login_at is not None

    identity = session.scalar(
        select(Identity).where(
            Identity.provider == "google",
            Identity.provider_subject == "google-sub-2",
        )
    )
    assert identity is not None
    assert identity.user_id == user.id


def test_resolve_google_user_existing_identity_does_not_require_email_verification(
    session: Session,
) -> None:
    user = User(email="existing@example.com", status=UserStatus.ACTIVE)
    identity = Identity(
        user=user,
        provider="google",
        provider_subject="google-sub-existing",
    )
    session.add_all([user, identity])
    session.commit()

    claims = {
        "sub": "google-sub-existing",
        "email": "existing@example.com",
        "email_verified": False,
        "name": "Existing User",
        "picture": "https://example.com/existing.png",
    }

    resolved = resolve_google_user(session, claims)

    assert resolved is user
    session.refresh(user)
    assert user.last_login_at is not None


def test_resolve_google_user_rejects_missing_user(session: Session) -> None:
    claims = {
        "sub": "google-sub-unknown",
        "email": "missing@example.com",
        "email_verified": True,
        "name": "Missing User",
        "picture": "https://example.com/missing.png",
    }

    with pytest.raises(GoogleUserNotFoundError):
        resolve_google_user(session, claims)

    assert (
        session.scalar(select(User).where(User.email == "missing@example.com")) is None
    )
    assert session.scalar(select(Identity).where(Identity.provider == "google")) is None


def test_resolve_google_user_rejects_disabled_user(session: Session) -> None:
    user = User(email="disabled@example.com", status=UserStatus.DISABLED)
    session.add(user)
    session.commit()

    claims = {
        "sub": "google-sub-disabled",
        "email": "disabled@example.com",
        "email_verified": True,
        "name": "Disabled User",
        "picture": "https://example.com/disabled.png",
    }

    with pytest.raises(GoogleUserDisabledError):
        resolve_google_user(session, claims)

    session.refresh(user)
    assert user.last_login_at is None
    assert (
        session.scalar(
            select(Identity).where(
                Identity.provider == "google",
                Identity.provider_subject == "google-sub-disabled",
            )
        )
        is None
    )


def test_resolve_google_user_rejects_missing_sub(session: Session) -> None:
    claims = {
        "email": "alice@example.com",
        "name": "Alice Example",
        "picture": "https://example.com/alice.png",
    }

    with pytest.raises(GoogleIdentityError):
        resolve_google_user(session, claims)


def test_resolve_google_user_rejects_missing_email_when_needed(
    session: Session,
) -> None:
    claims = {
        "sub": "google-sub-no-email",
        "name": "Anonymous User",
        "picture": "https://example.com/noemail.png",
    }

    with pytest.raises(GoogleIdentityError):
        resolve_google_user(session, claims)


def test_resolve_google_user_uses_google_sub_as_provider_subject(
    session: Session,
) -> None:
    user = User(email="provider@example.com", status=UserStatus.ACTIVE)
    session.add(user)
    session.commit()

    google_sub = "google-sub-provider-key"
    claims = {
        "sub": google_sub,
        "email": "Provider@Example.com",
        "email_verified": True,
        "name": "Provider User",
        "picture": "https://example.com/provider.png",
    }

    resolve_google_user(session, claims)

    identity = session.scalar(
        select(Identity).where(
            Identity.provider == "google",
            Identity.provider_subject == google_sub,
        )
    )
    assert identity is not None
    assert identity.provider_subject == google_sub
    assert identity.provider_subject != user.email
