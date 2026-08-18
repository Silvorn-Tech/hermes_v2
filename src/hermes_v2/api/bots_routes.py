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
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.authorization import require_permission
from hermes_v2.auth.rate_limiting import rate_limit
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceClient,
    BinanceError,
    BinanceRateLimitError,
)
from hermes_v2.trading.binance_credentials_service import (
    CredentialsNotConfiguredError,
    get_decrypted_client,
)
from hermes_v2.trading.bot_service import (
    BotNotFoundError,
    BotService,
    BotServiceError,
    InvalidBotTransitionError,
)
from hermes_v2.trading.credentials_encryption import CredentialsEncryptionError
from hermes_v2.trading.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from hermes_v2.trading.models import (
    Bot,
    BotExecutionMode,
    Order,
    OrderStatus,
    SimulationAccount,
    SimulationOrder,
    SimulationOrderStatus,
    SimulationSnapshot,
)
from hermes_v2.trading.live_portfolio_service import (
    compute_live_realized_pnl_today,
    compute_live_trade_stats,
)
from hermes_v2.trading.origin_check import require_trusted_origin
from hermes_v2.trading.portfolio_snapshot_service import compute_max_drawdown_pct
from hermes_v2.trading.rate_limiting import (
    BOT_ACTIVATE_LIVE_RATE_LIMITER,
    BOT_CREATE_RATE_LIMITER,
    BOT_DELETE_RATE_LIMITER,
    BOT_PAUSE_RATE_LIMITER,
    BOT_RESUME_RATE_LIMITER,
    BOT_STOP_RATE_LIMITER,
    BOT_UPDATE_RATE_LIMITER,
)
from hermes_v2.trading.simulation_config import (
    default_simulation_initial_capital,
    default_simulation_quote_asset,
)
from hermes_v2.trading.simulation_order_service import simulation_order_to_response
from hermes_v2.trading.simulation_portfolio_service import (
    compute_position_value,
    compute_realized_pnl_today,
    compute_return_pct,
    compute_total_value,
    compute_trade_stats,
)

router = APIRouter()

# No SimulationAccount.quote_asset equivalent exists for a LIVE bot (it
# has no per-bot account row at all) -- matches trading_routes.py's own
# constant for the same reason.
_DEFAULT_QUOTE_ASSET = "USDT"


# --- session / client plumbing ------------------------------------------------


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(), autoflush=False, expire_on_commit=False
    )


def _new_public_binance_client() -> BinanceClient:
    """No credentials -- for market-data-only reads. SIMULATION bots
    never need more than this (they only ever call the unsigned
    `get_market_data`)."""
    return BinanceClient(api_key="", api_secret="")  # nosec B106 - deliberately empty


def _new_optional_binance_client_for_user(
    session: Session, user_id: uuid.UUID
) -> BinanceClient | None:
    """Best-effort -- `None` if this user has no connected/verified
    Binance account, or credential encryption isn't configured. A real,
    credentialed client also works fine for a SIMULATION bot's
    market-data-only reads (`get_market_data` is unsigned regardless of
    the client's credentials), so resolving this unconditionally for
    every mutating bot action is always safe -- it's only *required*,
    not merely helpful, once the bot turns out to be LIVE, since
    `OrderService.create_order()` needs a real signed client to place a
    real order. See `_run_bot_service_action`."""
    try:
        return get_decrypted_client(session, user_id)
    except (CredentialsNotConfiguredError, CredentialsEncryptionError):
        return None


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


def _get_owned_bot(session: Session, user_id: uuid.UUID, bot_id: uuid.UUID) -> Bot:
    bot = session.get(Bot, bot_id)
    if bot is None or bot.user_id != user_id:
        raise HTTPException(status_code=404, detail="Bot not found.")
    return bot


def _live_order_to_trade_response(order: Order) -> dict[str, Any]:
    return {
        "side": order.side.value,
        "fill_price": (
            str(order.average_fill_price)
            if order.average_fill_price is not None
            else None
        ),
        "executed_quantity": str(order.executed_quantity),
        "terminal_at": order.terminal_at.isoformat() if order.terminal_at else None,
    }


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
    user_id: uuid.UUID,
    action: Callable[[BotService], dict[str, Any]],
) -> dict[str, Any]:
    """Shared transaction/error-handling wrapper, mirroring
    trading_routes.py's `_run_order_service_action` exactly — a
    BotServiceError still commits, since the audit/idempotency rows
    BotService already wrote before raising are real and must survive.

    Resolves a best-effort per-user credentialed client up front (falls
    back to the public, credential-less one if this user has none
    connected) rather than branching on the bot's `execution_mode` --
    that would require loading the bot before this wrapper even calls
    into BotService, and a real client works identically to the public
    one for SIMULATION's market-data-only reads. This is what makes a
    LIVE bot's pause/resume actually place a real, correctly-signed
    order instead of silently getting a blank-credential client; see
    `_new_optional_binance_client_for_user`'s own docstring for why the
    fallback can never silently succeed with the wrong credentials."""
    session_factory = _session_factory()
    with session_factory() as session:
        client = (
            _new_optional_binance_client_for_user(session, user_id)
            or _new_public_binance_client()
        )
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
        client = _new_public_binance_client()
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
        client = _new_public_binance_client()
        service = BotService(session, client)
        try:
            return service.get_bot(user_id, bot_id)
        except BotNotFoundError as exc:
            raise _http_exception_for_bot_service_error(exc) from exc


@router.get("/config/simulation")
async def get_simulation_config_route() -> dict[str, Any]:
    """No permission gate: this is a documented, operator-set default
    (not account data or a secret) — the frontend's Bot Creation Form
    reads it to size the budget slider for a new bot without hardcoding
    the number, matching whatever `HERMES_SIMULATION_INITIAL_CAPITAL_USD`
    is currently configured to."""
    return {
        "initial_capital_quote": str(default_simulation_initial_capital()),
        "quote_asset": default_simulation_quote_asset(),
    }


@router.get("/bots/{bot_id}/portfolio")
async def get_bot_portfolio_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.read")),
    _portfolio_permission: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        bot = _get_owned_bot(session, user_id, bot_id)
        position = bot.position
        current_quantity = (
            Decimal(position.current_quantity) if position else Decimal("0")
        )

        market_price = Decimal("0")
        if current_quantity > 0:
            client = _new_public_binance_client()
            try:
                market_data = client.get_market_data(bot.instrument)
            except BinanceError as exc:
                raise _http_exception_for_binance_error(exc) from exc
            market_price = Decimal(str(market_data["last_price"]))

        if bot.execution_mode == BotExecutionMode.LIVE:
            # No ring-fenced per-bot capital exists for a LIVE bot (it
            # trades against the user's one shared real Binance balance),
            # so there is no cash/exposure/return% to report -- see
            # live_portfolio_service.py's module docstring.
            position_value = compute_position_value(current_quantity, market_price)
            return {
                "available": True,
                "execution_mode": bot.execution_mode.value,
                "quote_asset": _DEFAULT_QUOTE_ASSET,
                "current_quantity": str(current_quantity),
                "position_value_quote": str(position_value),
                "total_value_quote": str(position_value),
                "return_pct": None,
            }

        account = session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot.id)
        )
        cash_balance = Decimal(account.cash_balance_quote)
        position_value = compute_position_value(current_quantity, market_price)
        total_value = compute_total_value(cash_balance, current_quantity, market_price)
        exposure_pct = (
            (position_value / total_value * 100) if total_value > 0 else Decimal("0")
        )
        return_pct = compute_return_pct(
            total_value, Decimal(account.initial_capital_quote)
        )

        return {
            "available": True,
            "execution_mode": bot.execution_mode.value,
            "quote_asset": account.quote_asset,
            "initial_capital_quote": str(account.initial_capital_quote),
            "cash_balance_quote": str(cash_balance),
            "current_quantity": str(current_quantity),
            "position_value_quote": str(position_value),
            "total_value_quote": str(total_value),
            "exposure_pct": str(exposure_pct),
            "return_pct": str(return_pct) if return_pct is not None else None,
        }


@router.get("/bots/{bot_id}/performance")
async def get_bot_performance_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.read")),
    _portfolio_permission: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        bot = _get_owned_bot(session, user_id, bot_id)
        position = bot.position
        current_quantity = (
            Decimal(position.current_quantity) if position else Decimal("0")
        )

        market_price = Decimal("0")
        if current_quantity > 0:
            client = _new_public_binance_client()
            try:
                market_data = client.get_market_data(bot.instrument)
            except BinanceError as exc:
                raise _http_exception_for_binance_error(exc) from exc
            market_price = Decimal(str(market_data["last_price"]))

        if bot.execution_mode == BotExecutionMode.LIVE:
            live_orders = session.scalars(
                select(Order).where(
                    Order.bot_id == bot.id, Order.status == OrderStatus.FILLED
                )
            ).all()
            live_realized_pnl_today = compute_live_realized_pnl_today(live_orders)
            live_trade_stats = compute_live_trade_stats(live_orders)
            position_value = compute_position_value(current_quantity, market_price)
            return {
                "available": True,
                "execution_mode": bot.execution_mode.value,
                "total_value_quote": str(position_value),
                "return_pct": None,
                "max_drawdown_pct": None,
                "realized_pnl_today_quote": str(live_realized_pnl_today),
                "trade_count": live_trade_stats.trade_count,
                "win_rate_pct": (
                    str(live_trade_stats.win_rate_pct)
                    if live_trade_stats.win_rate_pct is not None
                    else None
                ),
            }

        account = session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot.id)
        )
        cash_balance = Decimal(account.cash_balance_quote)
        position_value = compute_position_value(current_quantity, market_price)
        total_value = compute_total_value(cash_balance, current_quantity, market_price)
        exposure_pct = (
            (position_value / total_value * 100) if total_value > 0 else Decimal("0")
        )
        return_pct = compute_return_pct(
            total_value, Decimal(account.initial_capital_quote)
        )

        orders = session.scalars(
            select(SimulationOrder).where(SimulationOrder.bot_id == bot.id)
        ).all()
        realized_pnl_today = compute_realized_pnl_today(orders)
        trade_stats = compute_trade_stats(orders)

        snapshots = session.scalars(
            select(SimulationSnapshot)
            .where(SimulationSnapshot.bot_id == bot.id)
            .order_by(SimulationSnapshot.snapshot_at.asc())
        ).all()
        max_drawdown_pct = compute_max_drawdown_pct(
            [s.total_value_quote for s in snapshots]
        )

        return {
            "available": True,
            "execution_mode": bot.execution_mode.value,
            "total_value_quote": str(total_value),
            "return_pct": str(return_pct) if return_pct is not None else None,
            "max_drawdown_pct": (
                str(max_drawdown_pct) if max_drawdown_pct is not None else None
            ),
            "realized_pnl_today_quote": str(realized_pnl_today),
            "trade_count": trade_stats.trade_count,
            "win_rate_pct": (
                str(trade_stats.win_rate_pct)
                if trade_stats.win_rate_pct is not None
                else None
            ),
            "exposure_pct": str(exposure_pct),
        }


@router.get("/bots/{bot_id}/trades")
async def get_bot_trades_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.read")),
    _portfolio_permission: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    """Every FILLED order for this bot, oldest first -- powers the entry/
    exit markers on the bot's price chart, for SIMULATION and LIVE alike
    (the chart itself is always visible; only the source table differs).
    Terminal states other than FILLED (REJECTED, FAILED) never opened or
    closed a real position, so they carry no fill price and are excluded
    rather than shown as a marker with nothing meaningful to plot."""
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        bot = _get_owned_bot(session, user_id, bot_id)

        if bot.execution_mode == BotExecutionMode.LIVE:
            live_orders = session.scalars(
                select(Order)
                .where(Order.bot_id == bot.id, Order.status == OrderStatus.FILLED)
                .order_by(Order.terminal_at.asc())
            ).all()
            return {
                "available": True,
                "execution_mode": bot.execution_mode.value,
                "trades": [
                    _live_order_to_trade_response(order) for order in live_orders
                ],
            }

        orders = session.scalars(
            select(SimulationOrder)
            .where(
                SimulationOrder.bot_id == bot.id,
                SimulationOrder.status == SimulationOrderStatus.FILLED,
            )
            .order_by(SimulationOrder.terminal_at.asc())
        ).all()

        return {
            "available": True,
            "execution_mode": bot.execution_mode.value,
            "trades": [simulation_order_to_response(order) for order in orders],
        }


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
        user_id,
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
        ),
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
        user_id,
        lambda service: service.update_bot(
            user_id=user_id,
            bot_id=bot_id,
            idempotency_key=idempotency_key,
            name=body.name,
            target_quantity=body.target_quantity,
            strategy_model=body.strategy_model,
            strategy_config=body.strategy_config,
        ),
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
        user_id,
        lambda service: service.pause(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        ),
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
        user_id,
        lambda service: service.resume(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        ),
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
        user_id,
        lambda service: service.stop(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        ),
    )


@router.delete("/bots/{bot_id}")
async def delete_bot_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.delete")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(BOT_DELETE_RATE_LIMITER, "bots.delete")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        user_id,
        lambda service: service.delete_bot(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        ),
    )


@router.post("/bots/{bot_id}/activate-live")
async def activate_bot_live_route(
    bot_id: uuid.UUID,
    current_user: dict = Depends(require_permission("bots.activate_live")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(BOT_ACTIVATE_LIVE_RATE_LIMITER, "bots.activate_live")
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_bot_service_action(
        user_id,
        lambda service: service.activate_live(
            user_id=user_id, bot_id=bot_id, idempotency_key=idempotency_key
        ),
    )


__all__ = ["router"]
