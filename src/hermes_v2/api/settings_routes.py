"""Settings REST API: each user's own Binance credentials, risk limits,
and personal trading switch. Mirrors `trading_routes.py`'s shape exactly
(thin routes, one DB session per request, business logic lives in
`hermes_v2.trading.*`), and duplicates its own small session/client
helpers rather than importing them, for the same reason `bots_routes.py`
does: `BinanceClient` needs to be independently monkeypatchable by
module attribute name in tests.

Every route is gated by `secrets.*`/`risk.*` (already-reserved catalog
permissions, granted by role) rather than a new self-service exemption —
these mean "may act on your own resources," the same way `bots.create`
does, not "admin only." There is no `user_id` path parameter on any
route here; each one can only ever read or write the caller's own row.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from hermes_v2.auth.authorization import require_permission
from hermes_v2.auth.rate_limiting import rate_limit
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceClient
from hermes_v2.trading.binance_credentials_service import (
    CredentialStatus,
    CredentialsUnsafeError,
    CredentialsVerificationFailedError,
    connect_credentials,
    disconnect_credentials,
    get_credential_status,
)
from hermes_v2.trading.credentials_encryption import CredentialsEncryptionError
from hermes_v2.trading.origin_check import require_trusted_origin
from hermes_v2.trading.rate_limiting import (
    SETTINGS_CREDENTIALS_DELETE_RATE_LIMITER,
    SETTINGS_CREDENTIALS_PUT_RATE_LIMITER,
    SETTINGS_RISK_LIMITS_RATE_LIMITER,
    SETTINGS_TRADING_SWITCH_RATE_LIMITER,
)
from hermes_v2.trading.risk_engine import RiskLimits
from hermes_v2.trading.user_risk_settings_service import (
    get_user_risk_limits,
    is_user_trading_enabled,
    save_user_risk_limits,
    set_user_trading_enabled,
)

router = APIRouter()


# --- session / client plumbing ------------------------------------------------


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(), autoflush=False, expire_on_commit=False
    )


def _current_user_id(current_user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(current_user["id"])


def _idempotency_key_header(
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> str:
    return idempotency_key


# --- serialization ---------------------------------------------------------------


def _serialize_credential_status(status: CredentialStatus) -> dict[str, Any]:
    return {
        "configured": status.configured,
        "api_key_last4": status.api_key_last4,
        "verified_at": status.verified_at.isoformat() if status.verified_at else None,
        "updated_at": status.updated_at.isoformat() if status.updated_at else None,
    }


def _serialize_risk_limits(limits: RiskLimits) -> dict[str, Any]:
    return {
        "max_order_notional_quote": (
            str(limits.max_order_notional_quote)
            if limits.max_order_notional_quote is not None
            else None
        ),
        "max_symbol_exposure_pct": (
            str(limits.max_symbol_exposure_pct)
            if limits.max_symbol_exposure_pct is not None
            else None
        ),
        "max_total_exposure_pct": (
            str(limits.max_total_exposure_pct)
            if limits.max_total_exposure_pct is not None
            else None
        ),
        "max_daily_loss_pct": (
            str(limits.max_daily_loss_pct)
            if limits.max_daily_loss_pct is not None
            else None
        ),
        "max_open_positions": limits.max_open_positions,
        "allowed_symbols": (
            sorted(limits.allowed_symbols) if limits.allowed_symbols else None
        ),
    }


# --- request bodies -----------------------------------------------------------


class ConnectBinanceCredentialsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=200)
    api_secret: str = Field(min_length=1, max_length=200)


class RiskLimitsRequest(BaseModel):
    max_order_notional_quote: Decimal | None = Field(default=None, gt=0)
    max_symbol_exposure_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_total_exposure_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_daily_loss_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_open_positions: int | None = Field(default=None, ge=1)
    allowed_symbols: list[str] | None = None

    @field_validator("allowed_symbols")
    @classmethod
    def _normalize_symbols(cls, value: list[str] | None) -> list[str] | None:
        """`null`/empty means "not configured" -- the same fail-closed
        semantics an unset HERMES_RISK_ALLOWED_SYMBOLS env var already
        has today. Uppercased and deduplicated so "btcusdt" and "BTCUSDT"
        in the same request can't silently produce two entries."""
        if not value:
            return None
        normalized = sorted(
            {symbol.strip().upper() for symbol in value if symbol.strip()}
        )
        return normalized or None

    def to_risk_limits(self) -> RiskLimits:
        return RiskLimits(
            max_order_notional_quote=self.max_order_notional_quote,
            max_symbol_exposure_pct=self.max_symbol_exposure_pct,
            max_total_exposure_pct=self.max_total_exposure_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_open_positions=self.max_open_positions,
            allowed_symbols=(
                frozenset(self.allowed_symbols) if self.allowed_symbols else None
            ),
        )


class TradingSwitchRequest(BaseModel):
    enabled: bool


# --- Binance credentials --------------------------------------------------------


@router.get("/settings/binance-credentials")
async def get_binance_credentials_route(
    current_user: dict = Depends(require_permission("secrets.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        return _serialize_credential_status(get_credential_status(session, user_id))


@router.put("/settings/binance-credentials")
async def put_binance_credentials_route(
    body: ConnectBinanceCredentialsRequest,
    current_user: dict = Depends(require_permission("secrets.manage")),
    _idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(
            SETTINGS_CREDENTIALS_PUT_RATE_LIMITER, "settings.binance_credentials.put"
        )
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    client = BinanceClient(api_key=body.api_key, api_secret=body.api_secret)
    session_factory = _session_factory()
    with session_factory() as session:
        try:
            status = await run_in_threadpool(
                connect_credentials,
                session,
                user_id,
                client,
                body.api_key,
                body.api_secret,
            )
        except CredentialsEncryptionError as exc:
            session.rollback()
            raise HTTPException(
                status_code=503,
                detail="Binance credential encryption is not configured.",
            ) from exc
        except (CredentialsVerificationFailedError, CredentialsUnsafeError) as exc:
            session.rollback()
            raise HTTPException(
                status_code=409, detail={"connected": False, "reason": str(exc)}
            ) from exc

        session.commit()
        return {
            "connected": True,
            "api_key_last4": status.api_key_last4,
            "verified_at": (
                status.verified_at.isoformat() if status.verified_at else None
            ),
        }


@router.delete("/settings/binance-credentials")
async def delete_binance_credentials_route(
    current_user: dict = Depends(require_permission("secrets.manage")),
    _idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(
            SETTINGS_CREDENTIALS_DELETE_RATE_LIMITER,
            "settings.binance_credentials.delete",
        )
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        disconnect_credentials(session, user_id)
        session.commit()
        return {"connected": False}


# --- risk limits -----------------------------------------------------------------


@router.get("/settings/risk-limits")
async def get_risk_limits_route(
    current_user: dict = Depends(require_permission("risk.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        return _serialize_risk_limits(get_user_risk_limits(session, user_id))


@router.put("/settings/risk-limits")
async def put_risk_limits_route(
    body: RiskLimitsRequest,
    current_user: dict = Depends(require_permission("risk.manage")),
    _idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(SETTINGS_RISK_LIMITS_RATE_LIMITER, "settings.risk_limits.put")
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        updated = save_user_risk_limits(session, user_id, body.to_risk_limits())
        session.commit()
        return _serialize_risk_limits(updated)


# --- personal trading switch ------------------------------------------------------


@router.get("/settings/trading-switch")
async def get_trading_switch_route(
    current_user: dict = Depends(require_permission("risk.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        return {"enabled": is_user_trading_enabled(session, user_id)}


@router.put("/settings/trading-switch")
async def put_trading_switch_route(
    body: TradingSwitchRequest,
    current_user: dict = Depends(require_permission("risk.manage")),
    _idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(SETTINGS_TRADING_SWITCH_RATE_LIMITER, "settings.trading_switch.put")
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        enabled = set_user_trading_enabled(session, user_id, body.enabled)
        session.commit()
        return {"enabled": enabled}


__all__ = ["router"]
