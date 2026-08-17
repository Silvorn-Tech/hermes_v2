"""Integration tests for binance_credentials_service.py against real
Postgres, with a hand-written fake BinanceClient (never real network I/O)
for the verify-before-persist call."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceAuthenticationError
from hermes_v2.trading.binance_credentials_service import (
    CredentialsNotConfiguredError,
    CredentialsUnsafeError,
    CredentialsVerificationFailedError,
    connect_credentials,
    disconnect_credentials,
    get_credential_status,
    get_decrypted_client,
)
from hermes_v2.trading.models import UserBinanceCredential

pytestmark = pytest.mark.database


class _FakeBinanceClient:
    def __init__(
        self, can_withdraw: bool = False, error: Exception | None = None
    ) -> None:
        self._can_withdraw = can_withdraw
        self._error = error
        self.get_api_key_permissions_calls = 0

    def get_api_key_permissions(self) -> dict:
        self.get_api_key_permissions_calls += 1
        if self._error is not None:
            raise self._error
        return {"can_withdraw": self._can_withdraw}


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE user_binance_credentials, role_permissions, "
                "user_roles, identities, sessions, permissions, roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HERMES_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


def _make_user(session: Session) -> User:
    user = User(email="trader@example.com")
    session.add(user)
    session.flush()
    return user


def test_get_credential_status_when_not_configured(session: Session) -> None:
    user = _make_user(session)
    status = get_credential_status(session, user.id)
    assert status.configured is False
    assert status.api_key_last4 is None


def test_connect_verifies_before_persisting(session: Session) -> None:
    user = _make_user(session)
    client = _FakeBinanceClient(can_withdraw=False)

    status = connect_credentials(
        session, user.id, client, "my-api-key-1234", "my-secret"
    )
    session.commit()

    assert client.get_api_key_permissions_calls == 1
    assert status.configured is True
    assert status.api_key_last4 == "1234"
    assert status.verified_at is not None


def test_connect_never_stores_the_plaintext_secret(session: Session) -> None:
    user = _make_user(session)
    client = _FakeBinanceClient(can_withdraw=False)
    connect_credentials(
        session, user.id, client, "my-api-key-1234", "super-secret-value"
    )
    session.commit()

    row = session.scalar(
        select(UserBinanceCredential).where(UserBinanceCredential.user_id == user.id)
    )
    assert "super-secret-value" not in row.api_secret_ciphertext
    assert "my-api-key-1234" not in row.api_key_ciphertext


def test_connect_rejects_a_key_that_fails_verification(session: Session) -> None:
    user = _make_user(session)
    client = _FakeBinanceClient(error=BinanceAuthenticationError("bad signature"))

    with pytest.raises(CredentialsVerificationFailedError):
        connect_credentials(session, user.id, client, "bad-key", "bad-secret")
    session.commit()

    status = get_credential_status(session, user.id)
    assert status.configured is False


def test_connect_rejects_a_key_with_withdrawals_enabled(session: Session) -> None:
    user = _make_user(session)
    client = _FakeBinanceClient(can_withdraw=True)

    with pytest.raises(CredentialsUnsafeError):
        connect_credentials(session, user.id, client, "unsafe-key", "unsafe-secret")
    session.commit()

    status = get_credential_status(session, user.id)
    assert status.configured is False


def test_connect_twice_overwrites_the_previous_credentials(session: Session) -> None:
    user = _make_user(session)
    connect_credentials(
        session, user.id, _FakeBinanceClient(), "first-key-0001", "first-secret"
    )
    session.commit()

    connect_credentials(
        session, user.id, _FakeBinanceClient(), "second-key-9999", "second-secret"
    )
    session.commit()

    status = get_credential_status(session, user.id)
    assert status.api_key_last4 == "9999"
    rows = session.scalars(
        select(UserBinanceCredential).where(UserBinanceCredential.user_id == user.id)
    ).all()
    assert len(rows) == 1  # upsert, not a second row


def test_disconnect_is_idempotent(session: Session) -> None:
    user = _make_user(session)
    connect_credentials(
        session, user.id, _FakeBinanceClient(), "a-key-1234", "a-secret"
    )
    session.commit()

    disconnect_credentials(session, user.id)
    session.commit()
    assert get_credential_status(session, user.id).configured is False

    disconnect_credentials(session, user.id)  # no-op, doesn't raise
    session.commit()


def test_get_decrypted_client_round_trips_the_original_credentials(
    session: Session,
) -> None:
    user = _make_user(session)
    connect_credentials(
        session, user.id, _FakeBinanceClient(), "round-trip-key", "round-trip-secret"
    )
    session.commit()

    client = get_decrypted_client(session, user.id)
    assert client._api_key == "round-trip-key"
    assert client._api_secret == "round-trip-secret"


def test_get_decrypted_client_without_credentials_raises(session: Session) -> None:
    user = _make_user(session)
    with pytest.raises(CredentialsNotConfiguredError):
        get_decrypted_client(session, user.id)
