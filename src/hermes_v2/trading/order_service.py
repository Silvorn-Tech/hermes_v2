"""OrderService — the only caller of `BinanceClient.create_order()`/
`cancel_order()` anywhere in Hermes. Every mutating trading action goes
through here:

    idempotency reservation
        -> kill switch
        -> OrderValidator
        -> RiskEngine
        -> BinanceClient
        -> reconciliation
        -> persistence
        -> audit log
        -> idempotency finalization

No HTTP route calls `BinanceClient` directly — only this module does, and
only after every gate above has passed. Callers (the API routes) own the
`Session`'s transaction boundary (`session.commit()` after a method
returns), matching the rest of this codebase's convention
(`auth/oauth.py`); `OrderService` itself never commits, only adds/flushes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient, BinanceError
from hermes_v2.trading.config import is_trading_enabled
from hermes_v2.trading.exchange_info_cache import EXCHANGE_INFO_CACHE, ExchangeInfoCache
from hermes_v2.trading.idempotency import (
    derive_binance_client_order_id,
    finalize,
    reserve,
)
from hermes_v2.trading.models import (
    AuditLogEntry,
    AuditResult,
    Order,
    OrderEvent,
    OrderEventType,
    OrderSide,
    OrderStatus,
    OrderType,
    is_cancelable_status,
)
from hermes_v2.trading.order_validator import (
    OrderValidationRequest,
    OrderValidator,
    symbol_filters_from_exchange_info,
)
from hermes_v2.trading.portfolio_service import PortfolioService
from hermes_v2.trading.positions_service import PositionsService
from hermes_v2.trading.reconciliation import reconcile_from_binance_payload
from hermes_v2.trading.risk_engine import (
    AccountRiskSnapshot,
    OrderRiskRequest,
    RiskEngine,
    load_risk_limits,
)

_CREATE_ENDPOINT = "POST /orders"
_CANCEL_ENDPOINT = "POST /orders/{id}/cancel"
_CLOSE_ENDPOINT = "POST /positions/{symbol}/close"


class OrderServiceError(RuntimeError):
    """Base class for errors that mean no order-shaped resource exists to
    return — the API layer maps these to a specific HTTP status, never a
    generic 500."""


class TradingDisabledError(OrderServiceError):
    """The kill switch (`TRADING_ENABLED`) is off. Nothing was attempted —
    no order row, no Binance call."""


class OrderNotFoundError(OrderServiceError):
    """No order with this id exists for this user."""


class OrderNotCancelableError(OrderServiceError):
    """The order exists but isn't in a cancelable state."""


class PositionNotFoundError(OrderServiceError):
    """No held balance for this symbol — nothing to close."""


def _order_response(order: Order) -> dict[str, Any]:
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "requested_quantity": str(order.requested_quantity),
        "requested_price": (
            str(order.requested_price) if order.requested_price is not None else None
        ),
        "executed_quantity": str(order.executed_quantity),
        "average_fill_price": (
            str(order.average_fill_price)
            if order.average_fill_price is not None
            else None
        ),
        "binance_order_id": order.binance_order_id,
        "error_message": order.error_message,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        "terminal_at": order.terminal_at.isoformat() if order.terminal_at else None,
    }


class OrderService:
    def __init__(
        self,
        session: Session,
        client: BinanceClient,
        quote_asset: str = "USDT",
        validator: OrderValidator | None = None,
        exchange_info_cache: ExchangeInfoCache | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._quote_asset = quote_asset.upper()
        self._validator = validator or OrderValidator()
        self._exchange_info_cache = exchange_info_cache or EXCHANGE_INFO_CACHE
        self._portfolio_service = PortfolioService(
            client, quote_asset=self._quote_asset
        )
        self._positions_service = PositionsService(
            client, quote_asset=self._quote_asset
        )

    # --- create -----------------------------------------------------------------

    def create_order(
        self,
        user_id: uuid.UUID,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        request_payload = {
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": str(quantity),
            "price": str(price) if price is not None else None,
        }
        reservation = reserve(
            self._session, user_id, _CREATE_ENDPOINT, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        return self._create_and_execute(
            reservation.key_row_id,
            user_id,
            symbol,
            side,
            order_type,
            quantity,
            price,
            idempotency_key,
            endpoint=_CREATE_ENDPOINT,
            audit_action="orders.create",
        )

    def _create_and_execute(
        self,
        reservation_key_row_id: uuid.UUID,
        user_id: uuid.UUID,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        idempotency_key: str,
        endpoint: str,
        audit_action: str,
    ) -> dict[str, Any]:
        if not is_trading_enabled():
            finalize(
                self._session,
                reservation_key_row_id,
                {"order": None, "status": "REJECTED", "reason": "Trading is disabled."},
            )
            self._audit(
                user_id,
                audit_action,
                symbol,
                quantity,
                AuditResult.REJECTED,
                None,
                None,
                "TRADING_ENABLED is false",
            )
            raise TradingDisabledError("Trading is disabled.")

        binance_client_order_id = derive_binance_client_order_id(
            user_id, endpoint, idempotency_key
        )
        order = Order(
            user_id=user_id,
            symbol=symbol,
            side=OrderSide(side),
            order_type=OrderType(order_type),
            status=OrderStatus.PENDING,
            requested_quantity=quantity,
            requested_price=price,
            binance_client_order_id=binance_client_order_id,
        )
        self._session.add(order)
        self._session.flush()

        response = self._validate_and_execute(
            order, side, order_type, quantity, price, audit_action
        )
        finalize(self._session, reservation_key_row_id, response)
        return response

    def _validate_and_execute(
        self,
        order: Order,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        audit_action: str,
    ) -> dict[str, Any]:
        try:
            market_price = Decimal(
                str(self._client.get_market_data(order.symbol)["last_price"])
            )
        except BinanceError as exc:
            return self._reject_order(
                order,
                OrderEventType.VALIDATION_FAILED,
                f"Could not fetch market price: {exc}",
                audit_action,
            )

        try:
            exchange_info = self._exchange_info_cache.get(self._client, order.symbol)
            filters = symbol_filters_from_exchange_info(exchange_info)
        except BinanceError as exc:
            return self._reject_order(
                order,
                OrderEventType.VALIDATION_FAILED,
                f"Could not fetch exchange info: {exc}",
                audit_action,
            )

        validation = self._validator.validate(
            OrderValidationRequest(
                symbol=order.symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
            ),
            market_price,
            filters,
        )
        if not validation.approved:
            return self._reject_order(
                order, OrderEventType.VALIDATION_FAILED, validation.reason, audit_action
            )

        try:
            risk_decision = self._evaluate_risk(
                order, side, validation.estimated_notional_quote
            )
        except BinanceError as exc:
            return self._reject_order(
                order,
                OrderEventType.RISK_REJECTED,
                f"Could not evaluate risk (account state unavailable): {exc}",
                audit_action,
            )
        if not risk_decision.approved:
            return self._reject_order(
                order, OrderEventType.RISK_REJECTED, risk_decision.reason, audit_action
            )

        return self._submit_to_binance(
            order, side, order_type, quantity, price, audit_action
        )

    def _evaluate_risk(self, order: Order, side: str, estimated_notional: Decimal):
        """May raise BinanceError — the caller above must not let that
        escape unhandled, or the idempotency reservation around this whole
        flow would never get finalized (see idempotency.py's `finalize`
        docstring: an unfinalized row blocks every future retry forever)."""
        risk_engine = RiskEngine(load_risk_limits())
        existing_position = self._positions_service.get_position(order.symbol)
        all_positions = self._positions_service.get_positions()

        current_symbol_exposure = (
            existing_position.value_quote
            if existing_position is not None
            and existing_position.value_quote is not None
            else Decimal("0")
        )
        current_total_exposure = sum(
            (p.value_quote for p in all_positions if p.value_quote is not None),
            Decimal("0"),
        )

        snapshot = AccountRiskSnapshot(
            total_portfolio_value_quote=self._portfolio_service.get_total_value_quote(),
            current_symbol_exposure_quote=current_symbol_exposure,
            current_total_exposure_quote=current_total_exposure,
            open_position_count=len(all_positions),
            realized_loss_today_quote=self._positions_service.get_realized_loss_today_quote(),
        )
        request = OrderRiskRequest(
            symbol=order.symbol,
            side=side,
            estimated_notional_quote=estimated_notional,
            is_new_symbol_for_account=(side == "BUY" and existing_position is None),
        )
        return risk_engine.validate_order(request, snapshot)

    def _reject_order(
        self, order: Order, event_type: OrderEventType, reason: str, audit_action: str
    ) -> dict[str, Any]:
        order.status = OrderStatus.REJECTED
        order.error_message = reason[:500]
        order.terminal_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(order_id=order.id, event_type=event_type, detail=reason[:1000])
        )
        self._session.flush()
        self._audit(
            order.user_id,
            audit_action,
            order.symbol,
            order.requested_quantity,
            AuditResult.REJECTED,
            order.id,
            None,
            reason,
        )
        return {"order": _order_response(order), "status": "REJECTED", "reason": reason}

    def _submit_to_binance(
        self,
        order: Order,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        audit_action: str,
    ) -> dict[str, Any]:
        order.submitted_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(
                order_id=order.id, event_type=OrderEventType.SUBMITTED, detail=None
            )
        )

        try:
            binance_order = self._client.create_order(
                symbol=order.symbol,
                side=side,
                order_type=order_type,
                quantity=str(quantity),
                price=str(price) if price is not None else None,
                client_order_id=order.binance_client_order_id,
            )
        except BinanceError as exc:
            return self._handle_ambiguous_create_failure(order, exc, audit_action)

        reconcile_from_binance_payload(order, binance_order)
        if order.status.value in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            order.terminal_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(
                order_id=order.id, event_type=OrderEventType.BINANCE_ACK, detail=None
            )
        )
        self._session.flush()
        self._audit(
            order.user_id,
            audit_action,
            order.symbol,
            order.requested_quantity,
            AuditResult.SUCCESS,
            order.id,
            order.binance_order_id,
            None,
        )
        return {
            "order": _order_response(order),
            "status": order.status.value,
            "reason": None,
        }

    def _handle_ambiguous_create_failure(
        self, order: Order, original_error: BinanceError, audit_action: str
    ):
        """create_order's HTTP call failed — but Binance may have already
        accepted it. Confirm exactly once via get_order before concluding
        anything; never blindly retry create_order itself."""
        try:
            binance_order = self._client.get_order(
                symbol=order.symbol, client_order_id=order.binance_client_order_id
            )
        except BinanceError:
            order.status = OrderStatus.FAILED
            order.error_message = (
                f"Order submission failed and could not be confirmed: {original_error}"
            )[:500]
            order.terminal_at = datetime.now(UTC)
            self._session.add(
                OrderEvent(
                    order_id=order.id,
                    event_type=OrderEventType.BINANCE_ERROR,
                    detail=str(original_error)[:1000],
                )
            )
            self._session.flush()
            self._audit(
                order.user_id,
                audit_action,
                order.symbol,
                order.requested_quantity,
                AuditResult.FAILED,
                order.id,
                None,
                order.error_message,
            )
            return {
                "order": _order_response(order),
                "status": "FAILED",
                "reason": order.error_message,
            }

        reconcile_from_binance_payload(order, binance_order)
        if order.status.value in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            order.terminal_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(
                order_id=order.id,
                event_type=OrderEventType.RECONCILED,
                detail="Confirmed on Binance after an ambiguous submission failure.",
            )
        )
        self._session.flush()
        self._audit(
            order.user_id,
            audit_action,
            order.symbol,
            order.requested_quantity,
            AuditResult.SUCCESS,
            order.id,
            order.binance_order_id,
            "Confirmed after ambiguous failure",
        )
        return {
            "order": _order_response(order),
            "status": order.status.value,
            "reason": None,
        }

    # --- cancel -----------------------------------------------------------------

    def cancel_order(
        self, user_id: uuid.UUID, order_id: uuid.UUID, idempotency_key: str
    ) -> dict[str, Any]:
        request_payload = {"order_id": str(order_id)}
        reservation = reserve(
            self._session, user_id, _CANCEL_ENDPOINT, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        if not is_trading_enabled():
            finalize(
                self._session,
                reservation.key_row_id,
                {"order": None, "status": "REJECTED", "reason": "Trading is disabled."},
            )
            raise TradingDisabledError("Trading is disabled.")

        order = self._session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            finalize(
                self._session,
                reservation.key_row_id,
                {"order": None, "status": "REJECTED", "reason": "Order not found."},
            )
            raise OrderNotFoundError(f"No order {order_id} for this user.")

        if not is_cancelable_status(order.status):
            finalize(
                self._session,
                reservation.key_row_id,
                {
                    "order": _order_response(order),
                    "status": "REJECTED",
                    "reason": f"Order is {order.status.value}, not cancelable.",
                },
            )
            raise OrderNotCancelableError(
                f"Order {order_id} is {order.status.value}, not cancelable."
            )

        self._session.add(
            OrderEvent(
                order_id=order.id,
                event_type=OrderEventType.CANCEL_REQUESTED,
                detail=None,
            )
        )

        try:
            binance_order = self._client.cancel_order(
                symbol=order.symbol,
                order_id=int(order.binance_order_id)
                if order.binance_order_id
                else None,
                client_order_id=(
                    order.binance_client_order_id
                    if not order.binance_order_id
                    else None
                ),
            )
        except BinanceError as exc:
            order.error_message = str(exc)[:500]
            self._session.add(
                OrderEvent(
                    order_id=order.id,
                    event_type=OrderEventType.BINANCE_ERROR,
                    detail=str(exc)[:1000],
                )
            )
            self._session.flush()
            self._audit(
                user_id,
                "orders.cancel",
                order.symbol,
                order.requested_quantity,
                AuditResult.FAILED,
                order.id,
                order.binance_order_id,
                order.error_message,
            )
            response = {
                "order": _order_response(order),
                "status": "FAILED",
                "reason": order.error_message,
            }
            finalize(self._session, reservation.key_row_id, response)
            return response

        reconcile_from_binance_payload(order, binance_order)
        order.terminal_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(
                order_id=order.id, event_type=OrderEventType.CANCELED, detail=None
            )
        )
        self._session.flush()
        self._audit(
            user_id,
            "orders.cancel",
            order.symbol,
            order.requested_quantity,
            AuditResult.SUCCESS,
            order.id,
            order.binance_order_id,
            None,
        )
        response = {
            "order": _order_response(order),
            "status": order.status.value,
            "reason": None,
        }
        finalize(self._session, reservation.key_row_id, response)
        return response

    # --- close position -----------------------------------------------------------

    def close_position(
        self, user_id: uuid.UUID, symbol: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Closing is always a full-quantity MARKET SELL through the exact
        same validate -> risk -> execute pipeline `create_order` uses —
        never a bypass path. Reserves its own idempotency scope
        (`POST /positions/{symbol}/close`), independent of `create_order`'s
        (`POST /orders`), so the same caller-chosen key string can't
        collide between "place a manual order" and "close this position."
        """
        symbol = symbol.upper()
        position = self._positions_service.get_position(symbol)
        if position is None:
            raise PositionNotFoundError(f"No open position for {symbol}.")

        request_payload = {"symbol": symbol, "quantity": str(position.quantity)}
        reservation = reserve(
            self._session, user_id, _CLOSE_ENDPOINT, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        return self._create_and_execute(
            reservation.key_row_id,
            user_id,
            symbol,
            "SELL",
            "MARKET",
            position.quantity,
            None,
            idempotency_key,
            endpoint=_CLOSE_ENDPOINT,
            audit_action="positions.close",
        )

    # --- audit --------------------------------------------------------------------

    def _audit(
        self,
        user_id: uuid.UUID,
        action: str,
        symbol: str | None,
        quantity: Decimal | None,
        result: AuditResult,
        hermes_order_id: uuid.UUID | None,
        binance_order_id: str | None,
        detail: str | None,
    ) -> None:
        self._session.add(
            AuditLogEntry(
                user_id=user_id,
                action=action,
                symbol=symbol,
                quantity=quantity,
                result=result,
                hermes_order_id=hermes_order_id,
                binance_order_id=binance_order_id,
                detail=detail[:1000] if detail else None,
            )
        )
        self._session.flush()


__all__ = [
    "OrderNotCancelableError",
    "OrderNotFoundError",
    "OrderService",
    "OrderServiceError",
    "PositionNotFoundError",
    "TradingDisabledError",
]
