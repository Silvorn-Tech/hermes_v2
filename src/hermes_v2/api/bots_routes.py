"""Bot REST API. Mirrors `trading_routes.py`'s shape exactly: thin routes,
no business logic — that lives in `hermes_v2.trading.bot_service`. Every
mutating route requires the same guards the trading routes do
(`require_permission`, `Idempotency-Key`, `require_trusted_origin`, a rate
limit).

The small session/client/error-mapping helpers below intentionally
duplicate `trading_routes.py`'s own (rather than being extracted into a
shared module): `tests/test_trading_api.py`'s `authorized_client` fixture
monkeypatches `trading_routes.BinanceClient` by module attribute name, and
each route module needs its own independently-patchable `BinanceClient`
reference for that convention to keep working — this mirrors the existing
test pattern instead of fighting it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.authorization import require_permission
from hermes_v2.auth.rate_limiting import rate_limit
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceClient,
    BinanceConfigurationError,
    BinanceError,
    BinanceRateLimitError,
)
from hermes_v2.trading.bot_service import (
    BotNotFoundError,
    BotService,
    BotServiceError,
    InvalidBotTransitionError,
)
from hermes_v2.trading.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from hermes_v2.trading.origin_check import require_trusted_origin
from hermes_v2.trading.rate_limiting import (
    BOT_CREATE_RATE_LIMITER,
    BOT_PAUSE_RATE_LIMITER,
    BOT_RESUME_RATE_LIMITER,
    BOT_STOP_RATE_LIMITER,
    BOT_UPDATE_RATE_LIMITER,
)

router = APIRouter()


# --- session / client plumbing ------------------------------------------------


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(), autoflush=False, expire_on_commit=False
    )


def _new_binance_client() -> BinanceClient:
    try:
        return BinanceClient()
    except BinanceConfigurationError as exc:
        raise HTTPException(
            status_code=503, detail="Binance integration is not configured."
        ) from exc


def _current_user_id(current_user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(current_user["id"])


def _idempotency_key_header(
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> str:
    return idempotency_key


# --- error mapping --------------------------------------------------------------


def _http_exception_for_bot_service_error(exc: BotServiceError) -> HTTPException:
    if isinstance(exc, BotNotFoundError):
        return HTTPException(status_code=404, detail="Bot not found.")
    if isinstance(exc, InvalidBotTransitionError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="Unexpected bot error.")


def _http_exception_for_binance_error(exc: BinanceError) -> HTTPException:
    if isinstance(exc, BinanceAuthenticationError):
        return HTTPException(
            status_code=502, detail="Binance rejected Hermes's credentials."
        )
    if isinstance(exc, BinanceRateLimitError):
        headers = (
            {"Retry-After": str(int(exc.retry_after_seconds))}
            if exc.retry_after_seconds
            else None
        )
        return HTTPException(
            status_code=429,
            detail="Binance is rate-limiting Hermes. Try again shortly.",
            headers=headers,
        )
    return HTTPException(status_code=503, detail="Binance is temporarily unavailable.")


def _run_bot_service_action(
    action: Callable[[BotService], dict[str, Any]],
) -> dict[str, Any]:
    """Shared transaction/error-handling wrapper, mirroring
    trading_routes.py's `_run_order_service_action` exactly — a
    BotServiceError still commits, since the audit/idempotency rows
    BotService already wrote before raising are real and must survive."""
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client()
        service = BotService(session, client)
        try:
            result = action(service)
        except BotServiceError as exc:
            session.commit()
            raise _http_exception_for_bot_service_error(exc) from exc
        except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
            session.commit()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BinanceError as exc:
            session.rollback()
            raise _http_exception_for_binance_error(exc) from exc
        except Exception:
            # Mirrors _run_order_service_action: BotService's own
            # exception path already recorded a FAILED-shaped state,
            # audit row, and finalized idempotency before re-raising —
            # commit so that survives session.close()'s implicit
            # rollback, then let FastAPI produce a plain 500.
            session.commit()
            raise

        session.commit()
        return result


# --- request bodies -----------------------------------------------------------


class CreateBotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    risk_profile: Literal["SENTINEL", "EQUILIBRIUM", "VORTEX"]
    asset_class: Literal["CRYPTO", "EQUITY"]
    execution_venue: Literal["BINANCE"]
    instrument: str = Field(min_length=1, max_length=20)
    target_quantity: Decimal = Field(gt=0)
    strategy_model: str | None = Field(default=None, max_length=50)
    strategy_config: dict[str, Any] | None = None


class UpdateBotRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    target_quantity: Decimal | None = Field(default=None, gt=0)
    strategy_model: str | None = Field(default=None, max_length=50)
    strategy_config: dict[str, Any] | None = None


# --- read endpoints ------------------------------------------------------------


@router.get("/bots")
async def list_bots_route(
    current_user: dict = Depends(require_permission("bots.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client()
        service = BotService(session, client)
        return {"bots": service.list_bots(user_id)}


@router.get("/bots/{bot_id}")
async def get_bot_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client()
        service = BotService(session, client)
        try:
            return service.get_bot(user_id, bot_id)
        except BotNotFoundError as exc:
            raise _http_exception_for_bot_service_error(exc) from exc


# --- mutating endpoints -----------------------------------------------------------


@router.post("/bots", status_code=201)
async def create_bot_route(
    body: CreateBotRequest,
    current_user: dict = Depends(require_permission("bots.create")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_CREATE_RATE_LIMITER, "bots.create")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        lambda service: service.create_bot(
            user_id=user_id,
            name=body.name,
            risk_profile=body.risk_profile,
            asset_class=body.asset_class,
            execution_venue=body.execution_venue,
            instrument=body.instrument,
            target_quantity=body.target_quantity,
            idempotency_key=idempotency_key,
            strategy_model=body.strategy_model,
            strategy_config=body.strategy_config,
        )
    )


@router.patch("/bots/{bot_id}")
async def update_bot_route(
    bot_id: uuid.UUID,
    body: UpdateBotRequest,
    current_user: dict = Depends(require_permission("bots.update")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_UPDATE_RATE_LIMITER, "bots.update")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        lambda service: service.update_bot(
            user_id=user_id,
            bot_id=bot_id,
            idempotency_key=idempotency_key,
            name=body.name,
            target_quantity=body.target_quantity,
            strategy_model=body.strategy_model,
            strategy_config=body.strategy_config,
        )
    )


@router.post("/bots/{bot_id}/pause")
async def pause_bot_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.pause")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_PAUSE_RATE_LIMITER, "bots.pause")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        lambda service: service.pause(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        )
    )


@router.post("/bots/{bot_id}/resume")
async def resume_bot_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.resume")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_RESUME_RATE_LIMITER, "bots.resume")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        lambda service: service.resume(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        )
    )


@router.post("/bots/{bot_id}/stop")
async def stop_bot_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.stop")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_STOP_RATE_LIMITER, "bots.stop")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        lambda service: service.stop(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        )
    )


__all__ = ["router"]
