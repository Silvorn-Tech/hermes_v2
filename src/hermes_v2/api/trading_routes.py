"""Trading REST API — the only place an HTTP request can reach a Binance
write. Every route requires an authenticated session (`require_permission`)
and, for the three mutating routes, a trusted `Origin` header
(`require_trusted_origin`) and an `Idempotency-Key`.

Routes are thin by design: resolve the caller, open one DB session +
`BinanceClient` per request, delegate to `OrderService` /
`PortfolioService` / `PositionsService`, map the result or exception to an
HTTP response. No business logic lives here — see `hermes_v2.trading.*`
for that.

`BinanceClient` uses the synchronous `requests` library, and uvicorn runs
a single worker/event loop (see `runtime.py`) — an `async def` route that
calls it directly would block that one event loop thread for the whole
Binance round-trip, serializing every other in-flight request behind it.
Every read route that touches Binance (directly or via
`PortfolioService`/`PositionsService`/order reconciliation) runs that call
through `run_in_threadpool` so it executes on a worker thread instead,
letting the event loop keep dispatching other requests while it waits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

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
from hermes_v2.trading.credentials_encryption import CredentialsEncryptionError
from hermes_v2.trading.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from hermes_v2.trading.models import (
    Order,
    OrderEvent,
    OrderEventType,
    OrderStatus,
    PortfolioSnapshot,
    is_terminal_status,
)
from hermes_v2.trading.order_service import (
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderService,
    OrderServiceError,
    PositionNotFoundError,
    TradingDisabledError,
    order_to_response,
)
from hermes_v2.trading.origin_check import require_trusted_origin
from hermes_v2.trading.portfolio_service import PortfolioService
from hermes_v2.trading.portfolio_snapshot_service import (
    compute_max_drawdown_pct,
    compute_return_pct,
    downsample,
)
from hermes_v2.trading.positions_service import Position, PositionsService
from hermes_v2.trading.rate_limiting import (
    CANCEL_ORDER_RATE_LIMITER,
    CLOSE_POSITION_RATE_LIMITER,
    CREATE_ORDER_RATE_LIMITER,
)
from hermes_v2.trading.reconciliation import reconcile_from_binance_payload

router = APIRouter()

_DEFAULT_QUOTE_ASSET = "USDT"
_DEFAULT_ORDERS_LIMIT = 50
_MAX_ORDERS_LIMIT = 200


# --- session / client plumbing ------------------------------------------------


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_engine_from_environment(), autoflush=False, expire_on_commit=False
    )


def _new_binance_client_for_user(session: Session, user_id: uuid.UUID) -> BinanceClient:
    """Required -- 409s with a structured "not connected" body (never a
    500) if this user hasn't connected a Binance account yet; 503 if the
    encryption key itself isn't configured on this server."""
    try:
        return get_decrypted_client(session, user_id)
    except CredentialsNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "available": False,
                "reason": "Connect your Binance account in Settings first.",
            },
        ) from exc
    except CredentialsEncryptionError as exc:
        raise HTTPException(
            status_code=503, detail="Binance credential encryption is not configured."
        ) from exc


def _new_optional_binance_client_for_user(
    session: Session, user_id: uuid.UUID
) -> BinanceClient | None:
    """Best-effort -- for read paths that already degrade gracefully
    without Binance (order reconciliation already swallows BinanceError)."""
    try:
        return get_decrypted_client(session, user_id)
    except (CredentialsNotConfiguredError, CredentialsEncryptionError):
        return None


def _new_public_binance_client() -> BinanceClient:
    """No credentials -- only for call sites that exclusively use
    Binance's unsigned endpoints."""
    return BinanceClient(api_key="", api_secret="")


def _current_user_id(current_user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(current_user["id"])


# --- error mapping --------------------------------------------------------------


def _http_exception_for_order_service_error(exc: OrderServiceError) -> HTTPException:
    if isinstance(exc, TradingDisabledError):
        return HTTPException(status_code=403, detail="Trading is currently disabled.")
    if isinstance(exc, OrderNotFoundError):
        return HTTPException(status_code=404, detail="Order not found.")
    if isinstance(exc, OrderNotCancelableError):
        return HTTPException(
            status_code=409, detail="Order is not in a cancelable state."
        )
    if isinstance(exc, PositionNotFoundError):
        return HTTPException(
            status_code=404, detail="No open position for that symbol."
        )
    return HTTPException(status_code=500, detail="Unexpected trading error.")


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


def _run_order_service_action(
    user_id: uuid.UUID,
    action: Callable[[OrderService], dict[str, Any]],
) -> dict[str, Any]:
    """Shared transaction/error-handling wrapper for the three mutating
    routes. An OrderServiceError still commits — the audit/idempotency rows
    OrderService already wrote before raising are real and must survive."""
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client_for_user(session, user_id)
        service = OrderService(session, client)
        try:
            result = action(service)
        except (
            TradingDisabledError,
            OrderNotFoundError,
            OrderNotCancelableError,
            PositionNotFoundError,
        ) as exc:
            session.commit()
            raise _http_exception_for_order_service_error(exc) from exc
        except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
            session.commit()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BinanceError as exc:
            session.rollback()
            raise _http_exception_for_binance_error(exc) from exc
        except Exception:
            # Anything else reaching here already went through
            # OrderService's own safety-net catch (see
            # order_service.py's _create_and_execute), which recorded a
            # FAILED order, an audit row, and finalized the idempotency
            # reservation before re-raising. Session.close() on the `with`
            # block's exit rolls back anything uncommitted, which would
            # silently discard all of that — commit here so it survives,
            # then let FastAPI's default handler produce a plain 500 with
            # no internal detail, same as everywhere else in this codebase.
            session.commit()
            raise

        session.commit()
        return result


# --- serialization ---------------------------------------------------------------


def _serialize_balance(balance: Any) -> dict[str, Any]:
    return {
        "asset": balance.asset,
        "free": str(balance.free),
        "locked": str(balance.locked),
        "value_quote": str(balance.value_quote)
        if balance.value_quote is not None
        else None,
        "priced": balance.priced,
    }


def _serialize_position(position: Position) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "asset": position.asset,
        "quantity": str(position.quantity),
        "average_entry_price": (
            str(position.average_entry_price)
            if position.average_entry_price is not None
            else None
        ),
        "current_price": (
            str(position.current_price) if position.current_price is not None else None
        ),
        "value_quote": (
            str(position.value_quote) if position.value_quote is not None else None
        ),
        "unrealized_pnl_quote": (
            str(position.unrealized_pnl_quote)
            if position.unrealized_pnl_quote is not None
            else None
        ),
        "unrealized_pnl_pct": (
            str(position.unrealized_pnl_pct)
            if position.unrealized_pnl_pct is not None
            else None
        ),
    }


def _reconcile_on_read(
    order: Order, client: BinanceClient | None, session: Session
) -> None:
    """Refresh a non-terminal order from Binance before it's returned.
    Best-effort: if Binance can't be reached right now (or this user has
    no connected account at all), the last known (possibly stale) state is
    returned rather than failing the whole read — a read must never 5xx
    just because reconciliation couldn't run this instant."""
    if client is None or is_terminal_status(order.status):
        return
    try:
        binance_order = client.get_order(
            symbol=order.symbol,
            order_id=int(order.binance_order_id) if order.binance_order_id else None,
            client_order_id=(
                order.binance_client_order_id if not order.binance_order_id else None
            ),
        )
    except BinanceError:
        return

    changed = reconcile_from_binance_payload(order, binance_order)
    if not changed:
        return
    if is_terminal_status(order.status):
        order.terminal_at = datetime.now(UTC)
    session.add(
        OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.RECONCILED,
            detail="reconcile-on-read",
        )
    )
    session.flush()
    session.commit()


# --- read endpoints ------------------------------------------------------------


@router.get("/portfolio")
async def get_portfolio(
    current_user: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client_for_user(session, user_id)
    try:
        portfolio = await run_in_threadpool(
            PortfolioService(client, quote_asset=_DEFAULT_QUOTE_ASSET).get_portfolio
        )
    except BinanceError as exc:
        raise _http_exception_for_binance_error(exc) from exc

    return {
        "quote_asset": portfolio["quote_asset"],
        "total_value_quote": str(portfolio["total_value_quote"]),
        "as_of": portfolio["as_of"],
        "balances": [
            {
                "asset": b["asset"],
                "free": str(b["free"]),
                "locked": str(b["locked"]),
                "value_quote": str(b["value_quote"])
                if b["value_quote"] is not None
                else None,
                "priced": b["priced"],
            }
            for b in portfolio["balances"]
        ],
    }


@router.get("/balances")
async def get_balances(
    current_user: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client_for_user(session, user_id)
    try:
        balances = await run_in_threadpool(
            PortfolioService(client, quote_asset=_DEFAULT_QUOTE_ASSET).get_balances
        )
    except BinanceError as exc:
        raise _http_exception_for_binance_error(exc) from exc

    return {"balances": [_serialize_balance(b) for b in balances]}


# 1D stays at native (snapshot-interval) resolution; longer periods are
# downsampled (see portfolio_snapshot_service.downsample) so the response
# stays chart-sized regardless of how long history has accumulated.
_PORTFOLIO_HISTORY_WINDOWS: dict[str, timedelta] = {
    "1D": timedelta(days=1),
    "7D": timedelta(days=7),
    "30D": timedelta(days=30),
    "90D": timedelta(days=90),
    "1Y": timedelta(days=365),
}
_PORTFOLIO_HISTORY_DOWNSAMPLE_MINUTES: dict[str, int] = {
    "1D": 0,
    "7D": 60,
    "30D": 240,
    "90D": 720,
    "1Y": 4320,
}


@router.get("/portfolio/history")
async def get_portfolio_history(
    period: Literal["1D", "7D", "30D", "90D", "1Y"] = Query(...),
    current_user: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    session_factory = _session_factory()
    with session_factory() as session:
        window = _PORTFOLIO_HISTORY_WINDOWS[period]
        since = datetime.now(UTC) - window
        rows = session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.snapshot_at >= since)
            .order_by(PortfolioSnapshot.snapshot_at.asc())
        ).all()

        bucket_minutes = _PORTFOLIO_HISTORY_DOWNSAMPLE_MINUTES[period]
        points = (
            downsample(list(rows), bucket_minutes) if bucket_minutes else list(rows)
        )
        values = [point.total_value_quote for point in points]
        return_pct = compute_return_pct(values)
        max_drawdown_pct = compute_max_drawdown_pct(values)

        return {
            "period": period,
            "quote_asset": points[0].quote_asset if points else _DEFAULT_QUOTE_ASSET,
            "points": [
                {"t": point.snapshot_at.isoformat(), "v": str(point.total_value_quote)}
                for point in points
            ],
            "return_pct": str(return_pct) if return_pct is not None else None,
            "max_drawdown_pct": (
                str(max_drawdown_pct) if max_drawdown_pct is not None else None
            ),
        }


@router.get("/market-data")
async def get_market_data_route(
    symbol: str = Query(..., min_length=1, max_length=20),
    current_user: dict = Depends(require_permission("portfolio.read")),
) -> dict[str, Any]:
    client = _new_public_binance_client()
    try:
        return await run_in_threadpool(client.get_market_data, symbol.upper())
    except BinanceError as exc:
        raise _http_exception_for_binance_error(exc) from exc


@router.get("/positions")
async def get_positions(
    current_user: dict = Depends(require_permission("positions.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        client = _new_binance_client_for_user(session, user_id)
    try:
        positions = await run_in_threadpool(
            PositionsService(client, quote_asset=_DEFAULT_QUOTE_ASSET).get_positions
        )
    except BinanceError as exc:
        raise _http_exception_for_binance_error(exc) from exc

    return {"positions": [_serialize_position(p) for p in positions]}


@router.get("/orders")
async def list_orders(
    symbol: str | None = Query(default=None, max_length=20),
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=_DEFAULT_ORDERS_LIMIT, ge=1, le=_MAX_ORDERS_LIMIT),
    current_user: dict = Depends(require_permission("orders.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        query = select(Order).where(Order.user_id == user_id)
        if symbol:
            query = query.where(Order.symbol == symbol.upper())
        if status:
            try:
                query = query.where(Order.status == OrderStatus(status.upper()))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Unknown order status: {status!r}"
                ) from exc
        query = query.order_by(Order.created_at.desc()).limit(limit)
        orders = session.scalars(query).all()

        client = _new_optional_binance_client_for_user(session, user_id)
        for order in orders:
            await run_in_threadpool(_reconcile_on_read, order, client, session)

        return {
            "orders": [order_to_response(order) for order in orders],
            "count": len(orders),
        }


@router.get("/orders/{order_id}")
async def get_order_route(
    order_id: uuid.UUID,
    current_user: dict = Depends(require_permission("orders.read")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    session_factory = _session_factory()
    with session_factory() as session:
        order = session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            raise HTTPException(status_code=404, detail="Order not found.")

        client = _new_optional_binance_client_for_user(session, user_id)
        await run_in_threadpool(_reconcile_on_read, order, client, session)
        return order_to_response(order)


# --- request bodies for mutating routes -------------------------------------------


class CreateOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT"]
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _price_matches_order_type(self) -> "CreateOrderRequest":
        if self.type == "MARKET" and self.price is not None:
            raise ValueError("MARKET orders must not specify a price")
        if self.type == "LIMIT" and self.price is None:
            raise ValueError("LIMIT orders require a price")
        return self


def _idempotency_key_header(
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> str:
    return idempotency_key


# --- mutating endpoints -----------------------------------------------------------


@router.post("/orders", status_code=201)
async def create_order_route(
    body: CreateOrderRequest,
    current_user: dict = Depends(require_permission("orders.create")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(CREATE_ORDER_RATE_LIMITER, "orders.create")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_order_service_action(
        user_id,
        lambda service: service.create_order(
            user_id=user_id,
            symbol=body.symbol,
            side=body.side,
            order_type=body.type,
            quantity=body.quantity,
            price=body.price,
            idempotency_key=idempotency_key,
        ),
    )


@router.post("/orders/{order_id}/cancel")
async def cancel_order_route(
    order_id: uuid.UUID,
    current_user: dict = Depends(require_permission("orders.cancel")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(rate_limit(CANCEL_ORDER_RATE_LIMITER, "orders.cancel")),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_order_service_action(
        user_id,
        lambda service: service.cancel_order(
            user_id=user_id, order_id=order_id, idempotency_key=idempotency_key
        ),
    )


@router.post("/positions/{symbol}/close", status_code=201)
async def close_position_route(
    symbol: str,
    current_user: dict = Depends(require_permission("positions.close")),
    idempotency_key: str = Depends(_idempotency_key_header),
    _origin_check: None = Depends(require_trusted_origin),
    _rate_limit: None = Depends(
        rate_limit(CLOSE_POSITION_RATE_LIMITER, "positions.close")
    ),
) -> dict[str, Any]:
    user_id = _current_user_id(current_user)
    return _run_order_service_action(
        user_id,
        lambda service: service.close_position(
            user_id=user_id, symbol=symbol, idempotency_key=idempotency_key
        ),
    )


__all__ = ["router"]
