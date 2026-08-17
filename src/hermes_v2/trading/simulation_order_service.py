"""SimulationOrderService — Simulation Mode's execution path. Mirrors
`OrderService`'s pipeline *sequence* exactly (idempotency reservation ->
kill switch -> `OrderValidator` -> `RiskEngine` -> execute -> persist ->
audit -> idempotency finalization), reusing every one of those shared
components **directly, unmodified** — nothing about how an order is
validated or risk-checked is reimplemented here.

The only things that differ from `OrderService` are exactly the two
things Simulation Mode is *for*: execution (a virtual fill against real
market data, never `BinanceClient.create_order`/`cancel_order`) and
portfolio accounting (the bot's own `SimulationAccount`, never
`PortfolioService`/`PositionsService`, which are real-Binance-account-
backed). `OrderService` itself is untouched by this module — it stays
the only path for manual orders and (in a future phase) LIVE bots.

This module's source is statically verified
(`tests/test_simulation_isolation.py`) to never reference `create_order`/
`cancel_order` — not just documented here, enforced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient, BinanceError
from hermes_v2.trading.exchange_info_cache import EXCHANGE_INFO_CACHE, ExchangeInfoCache
from hermes_v2.trading.idempotency import IdempotencyReservation, finalize, reserve
from hermes_v2.trading.kill_switch import is_trading_permitted
from hermes_v2.trading.models import (
    AuditLogEntry,
    AuditResult,
    Bot,
    OrderSide,
    OrderType,
    SimulationAccount,
    SimulationOrder,
    SimulationOrderStatus,
)
from hermes_v2.trading.order_service import TradingDisabledError
from hermes_v2.trading.order_validator import (
    OrderValidationRequest,
    OrderValidator,
    symbol_filters_from_exchange_info,
)
from hermes_v2.trading.risk_engine import (
    AccountRiskSnapshot,
    OrderRiskRequest,
    RiskEngine,
)
from hermes_v2.trading.simulation_config import (
    simulation_fee_rate_pct,
    simulation_slippage_rate_pct,
)
from hermes_v2.trading.user_risk_settings_service import get_user_simulation_risk_limits
from hermes_v2.trading.simulation_portfolio_service import (
    compute_realized_pnl_today,
    compute_total_value,
)

_ENDPOINT_TEMPLATE = "POST /bots/{bot_id}/simulation-order"


def simulation_order_to_response(order: SimulationOrder) -> dict[str, Any]:
    return {
        "id": str(order.id),
        "bot_id": str(order.bot_id),
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "requested_quantity": str(order.requested_quantity),
        "fill_price": str(order.fill_price) if order.fill_price is not None else None,
        "executed_quantity": str(order.executed_quantity),
        "fee_quote": str(order.fee_quote),
        "slippage_quote": str(order.slippage_quote),
        "reason": order.reason,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "terminal_at": order.terminal_at.isoformat() if order.terminal_at else None,
    }


class SimulationOrderService:
    def __init__(
        self,
        session: Session,
        client: BinanceClient,
        quote_asset: str = "USDT",
        validator: OrderValidator | None = None,
        exchange_info_cache: ExchangeInfoCache | None = None,
    ) -> None:
        self._session = session
        # ONLY ever used for get_market_data()/get_exchange_info() below —
        # read-only market data. Never call .create_order()/.cancel_order()
        # on this client anywhere in this class.
        self._client = client
        self._quote_asset = quote_asset.upper()
        self._validator = validator or OrderValidator()
        self._exchange_info_cache = exchange_info_cache or EXCHANGE_INFO_CACHE

    def place_bot_order(
        self,
        user_id: uuid.UUID,
        bot: Bot,
        side: str,
        quantity: Decimal,
        idempotency_key: str,
    ) -> dict[str, Any]:
        endpoint = _ENDPOINT_TEMPLATE.format(bot_id=bot.id)
        request_payload = {
            "bot_id": str(bot.id),
            "side": side,
            "quantity": str(quantity),
        }
        reservation = reserve(
            self._session, user_id, endpoint, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        # Studied deliberately (see docs/architecture/simulation.md's
        # "Kill switch" section): the global switch being off blocks
        # Simulation fills too -- it means "Hermes is not placing trades
        # right now" -- full stop -- not "not placing *real* trades right
        # now." The per-user switch gates Simulation for symmetry with
        # that same policy, scoped to one user (see kill_switch.py).
        if not is_trading_permitted(self._session, user_id):
            finalize(
                self._session,
                reservation.key_row_id,
                {"order": None, "status": "REJECTED", "reason": "Trading is disabled."},
            )
            self._audit(
                user_id,
                bot,
                AuditResult.REJECTED,
                None,
                "Trading is not permitted (global or per-user switch is off)",
            )
            raise TradingDisabledError("Trading is disabled.")

        account = self._session.scalar(
            select(SimulationAccount).where(SimulationAccount.bot_id == bot.id)
        )
        if account is None:
            # Defensive only -- BotService.create_bot() always creates the
            # SimulationAccount alongside a SIMULATION bot; this should be
            # unreachable in practice.
            response = {
                "order": None,
                "status": "FAILED",
                "reason": "No simulation account exists for this bot.",
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id, bot, AuditResult.FAILED, None, "No simulation account found"
            )
            return response

        symbol = bot.position.instrument

        try:
            market_data = self._client.get_market_data(symbol)
            market_price = Decimal(str(market_data["last_price"]))
        except BinanceError as exc:
            return self._terminal(
                reservation,
                user_id,
                bot,
                account,
                side,
                quantity,
                SimulationOrderStatus.FAILED,
                f"Could not fetch market price: {exc}",
            )

        try:
            exchange_info = self._exchange_info_cache.get(self._client, symbol)
            filters = symbol_filters_from_exchange_info(exchange_info)
        except BinanceError as exc:
            return self._terminal(
                reservation,
                user_id,
                bot,
                account,
                side,
                quantity,
                SimulationOrderStatus.FAILED,
                f"Could not fetch exchange info: {exc}",
            )

        validation = self._validator.validate(
            OrderValidationRequest(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=quantity,
                price=None,
            ),
            market_price,
            filters,
        )
        if not validation.approved:
            return self._terminal(
                reservation,
                user_id,
                bot,
                account,
                side,
                quantity,
                SimulationOrderStatus.REJECTED,
                validation.reason,
            )

        estimated_notional = validation.estimated_notional_quote or (
            market_price * quantity
        )
        risk_decision = self._evaluate_risk(
            user_id, bot, account, side, market_price, estimated_notional
        )
        if not risk_decision.approved:
            return self._terminal(
                reservation,
                user_id,
                bot,
                account,
                side,
                quantity,
                SimulationOrderStatus.REJECTED,
                risk_decision.reason,
            )

        if side == "BUY":
            available = Decimal(account.cash_balance_quote)
            projected_cost = estimated_notional * (
                1 + self._fee_rate_pct() / 100 + self._slippage_rate_pct() / 100
            )
            if projected_cost > available:
                return self._terminal(
                    reservation,
                    user_id,
                    bot,
                    account,
                    side,
                    quantity,
                    SimulationOrderStatus.REJECTED,
                    (
                        f"Insufficient virtual balance: needs ~{projected_cost} "
                        f"{account.quote_asset}, has {available}."
                    ),
                )

        return self._fill(
            reservation, user_id, bot, account, side, quantity, market_price
        )

    # --- risk -----------------------------------------------------------------

    def _evaluate_risk(
        self,
        user_id: uuid.UUID,
        bot: Bot,
        account: SimulationAccount,
        side: str,
        market_price: Decimal,
        estimated_notional: Decimal,
    ):
        risk_engine = RiskEngine(
            get_user_simulation_risk_limits(self._session, user_id)
        )
        position = bot.position
        current_quantity = (
            Decimal(position.current_quantity) if position else Decimal("0")
        )
        # BotPosition is one instrument per bot (see its own docstring),
        # so this bot's symbol exposure and total exposure are the same
        # single number -- there is nothing else this simulation account
        # could be holding.
        current_exposure = current_quantity * market_price
        total_value = compute_total_value(
            Decimal(account.cash_balance_quote), current_quantity, market_price
        )

        orders_today = self._session.scalars(
            select(SimulationOrder).where(SimulationOrder.bot_id == bot.id)
        ).all()
        realized_pnl_today = compute_realized_pnl_today(orders_today)
        realized_loss_today = max(Decimal("0"), -realized_pnl_today)

        snapshot = AccountRiskSnapshot(
            total_portfolio_value_quote=total_value,
            current_symbol_exposure_quote=current_exposure,
            current_total_exposure_quote=current_exposure,
            open_position_count=1 if current_quantity > 0 else 0,
            realized_loss_today_quote=realized_loss_today,
        )
        request = OrderRiskRequest(
            symbol=bot.position.instrument,
            side=side,
            estimated_notional_quote=estimated_notional,
            is_new_symbol_for_account=(side == "BUY" and current_quantity == 0),
        )
        return risk_engine.validate_order(request, snapshot)

    # --- fill / rejection -------------------------------------------------------

    def _fee_rate_pct(self) -> Decimal:
        return simulation_fee_rate_pct()

    def _slippage_rate_pct(self) -> Decimal:
        return simulation_slippage_rate_pct()

    def _fill(
        self,
        reservation: IdempotencyReservation,
        user_id: uuid.UUID,
        bot: Bot,
        account: SimulationAccount,
        side: str,
        quantity: Decimal,
        market_price: Decimal,
    ) -> dict[str, Any]:
        slippage_pct = self._slippage_rate_pct()
        fee_pct = self._fee_rate_pct()

        # Slippage always moves the fill price against the account,
        # regardless of side -- worse for a BUY (pay more), worse for a
        # SELL (receive less) -- the same direction real slippage does.
        if side == "BUY":
            fill_price = market_price * (1 + slippage_pct / 100)
        else:
            fill_price = market_price * (1 - slippage_pct / 100)

        notional = fill_price * quantity
        fee = notional * fee_pct / 100
        slippage_amount = abs(fill_price - market_price) * quantity

        order = SimulationOrder(
            bot_id=bot.id,
            simulation_account_id=account.id,
            symbol=bot.position.instrument,
            side=OrderSide(side),
            order_type=OrderType.MARKET,
            status=SimulationOrderStatus.FILLED,
            requested_quantity=quantity,
            fill_price=fill_price,
            executed_quantity=quantity,
            fee_quote=fee,
            slippage_quote=slippage_amount,
            terminal_at=datetime.now(UTC),
        )
        self._session.add(order)

        if side == "BUY":
            account.cash_balance_quote = Decimal(account.cash_balance_quote) - (
                notional + fee
            )
        else:
            account.cash_balance_quote = Decimal(account.cash_balance_quote) + (
                notional - fee
            )
        self._session.flush()

        self._audit(
            user_id,
            bot,
            AuditResult.SUCCESS,
            order.id,
            f"Simulated {side} {quantity} {bot.position.instrument} @ {fill_price}",
        )
        response = {
            "order": simulation_order_to_response(order),
            "status": "FILLED",
            "reason": None,
        }
        finalize(self._session, reservation.key_row_id, response)
        return response

    def _terminal(
        self,
        reservation: IdempotencyReservation,
        user_id: uuid.UUID,
        bot: Bot,
        account: SimulationAccount,
        side: str,
        quantity: Decimal,
        status: SimulationOrderStatus,
        reason: str | None,
    ) -> dict[str, Any]:
        order = SimulationOrder(
            bot_id=bot.id,
            simulation_account_id=account.id,
            symbol=bot.position.instrument,
            side=OrderSide(side),
            order_type=OrderType.MARKET,
            status=status,
            requested_quantity=quantity,
            reason=(reason or "")[:500],
            terminal_at=datetime.now(UTC),
        )
        self._session.add(order)
        self._session.flush()

        audit_result = (
            AuditResult.REJECTED
            if status == SimulationOrderStatus.REJECTED
            else AuditResult.FAILED
        )
        self._audit(user_id, bot, audit_result, order.id, reason)
        response = {
            "order": simulation_order_to_response(order),
            "status": status.value,
            "reason": reason,
        }
        finalize(self._session, reservation.key_row_id, response)
        return response

    # --- audit --------------------------------------------------------------------

    def _audit(
        self,
        user_id: uuid.UUID,
        bot: Bot,
        result: AuditResult,
        simulation_order_id: uuid.UUID | None,
        detail: str | None,
    ) -> None:
        full_detail = (
            f"bot_id={bot.id} (SIMULATION): {detail}"
            if detail
            else f"bot_id={bot.id} (SIMULATION)"
        )
        self._session.add(
            AuditLogEntry(
                user_id=user_id,
                action="bot.simulation_order",
                symbol=bot.position.instrument if bot.position else None,
                quantity=None,
                result=result,
                hermes_order_id=simulation_order_id,
                binance_order_id=None,
                detail=full_detail[:1000],
            )
        )
        self._session.flush()


__all__ = ["SimulationOrderService", "simulation_order_to_response"]
