"""BotService — the Bot domain's only entry point for lifecycle actions.

A Bot never calls `BinanceClient` directly and never bypasses
`OrderService`/`RiskEngine`/the kill switch — pause/resume place their
sell/buy exclusively through `OrderService.create_order()`, exactly the
same execution path a manual order uses.

**Why not `OrderService.close_position()` for pause**: that method always
sells 100% of whatever `PositionsService` reports as the account's live
Binance balance for a symbol — it has no quantity parameter, and there is
no database row representing "a position" anywhere; positions are
computed purely from Binance, identified only by symbol. If two bots (or
a bot and a manual trade) ever held the same symbol, `close_position()`
would liquidate the *entire account's* holding, not just this bot's
share. `BotPosition` is Hermes's own book of record for what a bot holds,
and pause/resume always sell/buy an *explicit* quantity drawn from it via
`create_order()` — never the shared, symbol-wide account balance.

**Pause/resume outcome table** (`create_order()`'s result, not a
bespoke retry loop):

| outcome | pause (ACTIVE->?) | resume (PAUSED->?) |
|---|---|---|
| raises `TradingDisabledError` | -> ACTIVE (nothing attempted) | -> PAUSED (same) |
| `status="REJECTED"` | -> ACTIVE (safe) | -> PAUSED (safe) |
| `status="FILLED"` (incl. after reconcile) | -> PAUSED | -> ACTIVE |
| `status="FAILED"`, or non-terminal after reconcile | -> ERROR | -> ERROR |
| any other exception | -> ERROR | -> ERROR |

`TradingDisabledError` is special-cased because `OrderService` *raises*
it rather than returning a `REJECTED` dict — conflating it with "any
exception -> ERROR" would land a bot in ERROR every time it's paused/
resumed while the kill switch is off, even though nothing was attempted.

Idempotency is two-layered, reusing `hermes_v2.trading.idempotency`'s
generic `reserve()`/`finalize()` directly (no `OrderService` changes):
an outer reservation scoped to `f"POST /bots/{bot_id}/{op}"` (protects
against a duplicate client request), and an inner, server-derived key
`f"bot-{op}:{bot_id}:{outer_key}"` passed to `OrderService.create_order`
(so the underlying order is itself deduped on retry). This derivation is
only safe because `ERROR` has no automatic retry path in v1 (its only
exit is `ERROR -> stop()`) — revisit if that ever changes.

The outer `reserve()` call alone does not stop two *differently*-keyed
concurrent requests (e.g. two browser tabs) from both reading the bot as
ACTIVE and both submitting an order — `_load_bot_for_update` takes a
`SELECT ... FOR UPDATE` row lock, always called *after* `reserve()` so a
legitimate retry with the same key still replays instead of failing a
"must be ACTIVE" precondition against an already-completed transition.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient, BinanceError
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.idempotency import (
    derive_binance_client_order_id,
    finalize,
    reserve,
)
from hermes_v2.trading.models import (
    AssetClass,
    AuditLogEntry,
    AuditResult,
    Bot,
    BotExecutionMode,
    BotPosition,
    BotStatus,
    ExecutionVenue,
    Order,
    OrderEvent,
    OrderEventType,
    RiskProfile,
    SimulationAccount,
    is_terminal_status,
)
from hermes_v2.trading.order_service import OrderService, TradingDisabledError
from hermes_v2.trading.reconciliation import reconcile_from_binance_payload
from hermes_v2.trading.simulation_config import (
    default_simulation_initial_capital,
    default_simulation_quote_asset,
)
from hermes_v2.trading.simulation_order_service import SimulationOrderService

_CREATE_ENDPOINT = "POST /bots"
_STOPPABLE_STATUSES = frozenset({BotStatus.ACTIVE, BotStatus.PAUSED, BotStatus.ERROR})


class BotServiceError(RuntimeError):
    """Base class for errors that mean no bot-shaped resource exists to
    return — the API layer maps these to a specific HTTP status, never a
    generic 500."""


class BotNotFoundError(BotServiceError):
    """No bot with this id exists for this user."""


class InvalidBotTransitionError(BotServiceError):
    """The requested operation isn't valid for the bot's current status."""


@dataclass(frozen=True)
class _TransitionConfig:
    endpoint_template: str
    from_status: BotStatus
    in_progress_status: BotStatus
    side: str
    audit_action: str


_TRANSITION_CONFIG: dict[str, _TransitionConfig] = {
    "pause": _TransitionConfig(
        endpoint_template="POST /bots/{bot_id}/pause",
        from_status=BotStatus.ACTIVE,
        in_progress_status=BotStatus.PAUSING,
        side="SELL",
        audit_action="bot.pause",
    ),
    "resume": _TransitionConfig(
        endpoint_template="POST /bots/{bot_id}/resume",
        from_status=BotStatus.PAUSED,
        in_progress_status=BotStatus.RESUMING,
        side="BUY",
        audit_action="bot.resume",
    ),
}


def bot_to_response(bot: Bot) -> dict[str, Any]:
    position = bot.position
    return {
        "id": str(bot.id),
        "name": bot.name,
        "risk_profile": bot.risk_profile.value,
        "asset_class": bot.asset_class.value,
        "execution_venue": bot.execution_venue.value,
        "execution_mode": bot.execution_mode.value,
        "instrument": bot.instrument,
        "strategy_model": bot.strategy_model,
        "strategy_config": bot.strategy_config,
        "status": bot.status.value,
        "current_quantity": str(position.current_quantity) if position else "0",
        "target_quantity": str(position.target_quantity) if position else None,
        "paused_at": (
            position.paused_at.isoformat() if position and position.paused_at else None
        ),
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
        "updated_at": bot.updated_at.isoformat() if bot.updated_at else None,
    }


class BotService:
    def __init__(
        self,
        session: Session,
        client: BinanceClient,
        quote_asset: str = "USDT",
        exchange_info_cache: ExchangeInfoCache | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._quote_asset = quote_asset.upper()
        # Threaded into the OrderService this constructs internally — same
        # reason OrderService itself accepts this (tests inject a fresh
        # cache instead of sharing the process-wide default).
        self._exchange_info_cache = exchange_info_cache

    # --- read ---------------------------------------------------------------

    def list_bots(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        bots = self._session.scalars(
            select(Bot).where(Bot.user_id == user_id).order_by(Bot.created_at.desc())
        ).all()
        return [bot_to_response(b) for b in bots]

    def get_bot(self, user_id: uuid.UUID, bot_id: uuid.UUID) -> dict[str, Any]:
        bot = self._session.get(Bot, bot_id)
        if bot is None or bot.user_id != user_id:
            raise BotNotFoundError(f"No bot {bot_id} for this user.")
        return bot_to_response(bot)

    # --- create / update ------------------------------------------------------

    def create_bot(
        self,
        user_id: uuid.UUID,
        name: str,
        risk_profile: str,
        asset_class: str,
        execution_venue: str,
        instrument: str,
        target_quantity: Decimal,
        idempotency_key: str,
        strategy_model: str | None = None,
        strategy_config: dict | None = None,
    ) -> dict[str, Any]:
        # Validated before touching the idempotency table — an invalid
        # enum value must fail cleanly, not leave a reservation stuck at
        # PROCESSING.
        risk_profile_enum = RiskProfile(risk_profile)
        asset_class_enum = AssetClass(asset_class)
        execution_venue_enum = ExecutionVenue(execution_venue)
        instrument = instrument.upper()

        request_payload = {
            "name": name,
            "risk_profile": risk_profile,
            "asset_class": asset_class,
            "execution_venue": execution_venue,
            "instrument": instrument,
            "target_quantity": str(target_quantity),
            "strategy_model": strategy_model,
            "strategy_config": strategy_config,
        }
        reservation = reserve(
            self._session, user_id, _CREATE_ENDPOINT, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        # Nothing is traded on creation — a bot starts PAUSED with zero
        # current exposure; the user's first Resume click is what places
        # its first-ever order, going through the exact same validated
        # path as any later resume.
        #
        # execution_mode is deliberately not a parameter here — every bot
        # is created SIMULATION in v1 (see BotExecutionMode's docstring
        # and docs/architecture/simulation.md); there is no request field
        # or code path that can set LIVE yet.
        bot = Bot(
            user_id=user_id,
            name=name,
            risk_profile=risk_profile_enum,
            asset_class=asset_class_enum,
            execution_venue=execution_venue_enum,
            execution_mode=BotExecutionMode.SIMULATION,
            instrument=instrument,
            strategy_model=strategy_model,
            strategy_config=strategy_config,
            status=BotStatus.PAUSED,
        )
        self._session.add(bot)
        self._session.flush()

        position = BotPosition(
            bot_id=bot.id,
            instrument=instrument,
            current_quantity=Decimal("0"),
            target_quantity=target_quantity,
        )
        self._session.add(position)

        simulation_account = SimulationAccount(
            bot_id=bot.id,
            quote_asset=default_simulation_quote_asset(),
            initial_capital_quote=default_simulation_initial_capital(),
            cash_balance_quote=default_simulation_initial_capital(),
        )
        self._session.add(simulation_account)
        self._session.flush()

        self._audit(
            user_id,
            "bot.create",
            AuditResult.SUCCESS,
            f"Created bot {name!r} ({instrument}), SIMULATION, "
            f"initial capital {simulation_account.initial_capital_quote} "
            f"{simulation_account.quote_asset}.",
            bot=bot,
        )
        response = {"bot": bot_to_response(bot), "status": "PAUSED", "reason": None}
        finalize(self._session, reservation.key_row_id, response)
        return response

    def update_bot(
        self,
        user_id: uuid.UUID,
        bot_id: uuid.UUID,
        idempotency_key: str,
        name: str | None = None,
        target_quantity: Decimal | None = None,
        strategy_model: str | None = None,
        strategy_config: dict | None = None,
    ) -> dict[str, Any]:
        """Only `name`, `target_quantity`, `strategy_model`/`strategy_config`
        are editable, and only while PAUSED — `asset_class`/
        `execution_venue`/`instrument` are immutable after creation."""
        endpoint = f"PATCH /bots/{bot_id}"
        request_payload = {
            "bot_id": str(bot_id),
            "name": name,
            "target_quantity": str(target_quantity)
            if target_quantity is not None
            else None,
            "strategy_model": strategy_model,
            "strategy_config": strategy_config,
        }
        reservation = reserve(
            self._session, user_id, endpoint, idempotency_key, request_payload
        )
        if not reservation.is_new:
            return reservation.stored_response

        bot = self._load_bot_for_update(user_id, bot_id)
        if bot is None:
            response = {"bot": None, "status": "REJECTED", "reason": "Bot not found."}
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id,
                "bot.update",
                AuditResult.REJECTED,
                "Bot not found",
                bot_id=bot_id,
            )
            raise BotNotFoundError(f"No bot {bot_id} for this user.")

        if bot.status != BotStatus.PAUSED:
            reason = f"Bot is {bot.status.value}; can only update while PAUSED."
            response = {
                "bot": bot_to_response(bot),
                "status": "REJECTED",
                "reason": reason,
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(user_id, "bot.update", AuditResult.REJECTED, reason, bot=bot)
            raise InvalidBotTransitionError(reason)

        if name is not None:
            bot.name = name
        if strategy_model is not None:
            bot.strategy_model = strategy_model
        if strategy_config is not None:
            bot.strategy_config = strategy_config
        if target_quantity is not None:
            bot.position.target_quantity = target_quantity
        self._session.flush()

        self._audit(
            user_id,
            "bot.update",
            AuditResult.SUCCESS,
            "Updated bot configuration.",
            bot=bot,
        )
        response = {
            "bot": bot_to_response(bot),
            "status": bot.status.value,
            "reason": None,
        }
        finalize(self._session, reservation.key_row_id, response)
        return response

    # --- lifecycle ------------------------------------------------------------

    def pause(
        self, user_id: uuid.UUID, bot_id: uuid.UUID, idempotency_key: str
    ) -> dict[str, Any]:
        return self._transition(user_id, bot_id, idempotency_key, op="pause")

    def resume(
        self, user_id: uuid.UUID, bot_id: uuid.UUID, idempotency_key: str
    ) -> dict[str, Any]:
        return self._transition(user_id, bot_id, idempotency_key, op="resume")

    def stop(
        self, user_id: uuid.UUID, bot_id: uuid.UUID, idempotency_key: str
    ) -> dict[str, Any]:
        """Pure status transition — never calls OrderService/Binance. Any
        open position stays open; closing it is the existing manual
        Trading screen's job, not this method's (STOP != PAUSE)."""
        endpoint = f"POST /bots/{bot_id}/stop"
        reservation = reserve(
            self._session, user_id, endpoint, idempotency_key, {"bot_id": str(bot_id)}
        )
        if not reservation.is_new:
            return reservation.stored_response

        bot = self._load_bot_for_update(user_id, bot_id)
        if bot is None:
            response = {"bot": None, "status": "REJECTED", "reason": "Bot not found."}
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id,
                "bot.stop",
                AuditResult.REJECTED,
                "Bot not found",
                bot_id=bot_id,
            )
            raise BotNotFoundError(f"No bot {bot_id} for this user.")

        if bot.status not in _STOPPABLE_STATUSES:
            reason = f"Bot is {bot.status.value}; cannot stop from this state."
            response = {
                "bot": bot_to_response(bot),
                "status": "REJECTED",
                "reason": reason,
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(user_id, "bot.stop", AuditResult.REJECTED, reason, bot=bot)
            raise InvalidBotTransitionError(reason)

        bot.status = BotStatus.STOPPED
        self._session.flush()
        response = {"bot": bot_to_response(bot), "status": "STOPPED", "reason": None}
        finalize(self._session, reservation.key_row_id, response)
        self._audit(user_id, "bot.stop", AuditResult.SUCCESS, "Bot stopped.", bot=bot)
        return response

    def _transition(
        self, user_id: uuid.UUID, bot_id: uuid.UUID, idempotency_key: str, *, op: str
    ) -> dict[str, Any]:
        cfg = _TRANSITION_CONFIG[op]
        endpoint = cfg.endpoint_template.format(bot_id=bot_id)
        reservation = reserve(
            self._session, user_id, endpoint, idempotency_key, {"bot_id": str(bot_id)}
        )
        if not reservation.is_new:
            return reservation.stored_response

        bot = self._load_bot_for_update(user_id, bot_id)
        if bot is None:
            response = {"bot": None, "status": "REJECTED", "reason": "Bot not found."}
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id,
                cfg.audit_action,
                AuditResult.REJECTED,
                "Bot not found",
                bot_id=bot_id,
            )
            raise BotNotFoundError(f"No bot {bot_id} for this user.")

        if bot.status != cfg.from_status:
            reason = f"Bot is {bot.status.value}; cannot {op} from this state."
            response = {
                "bot": bot_to_response(bot),
                "status": "REJECTED",
                "reason": reason,
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id, cfg.audit_action, AuditResult.REJECTED, reason, bot=bot
            )
            raise InvalidBotTransitionError(reason)

        prior_status = bot.status
        bot.status = cfg.in_progress_status
        self._session.flush()

        position = bot.position
        quantity = (
            position.current_quantity if op == "pause" else position.target_quantity
        )
        inner_key = f"bot-{op}:{bot_id}:{idempotency_key}"
        is_simulation = bot.execution_mode == BotExecutionMode.SIMULATION

        try:
            if is_simulation:
                simulation_service = SimulationOrderService(
                    self._session,
                    self._client,
                    self._quote_asset,
                    exchange_info_cache=self._exchange_info_cache,
                )
                result = simulation_service.place_bot_order(
                    user_id=user_id,
                    bot=bot,
                    side=cfg.side,
                    quantity=quantity,
                    idempotency_key=inner_key,
                )
            else:
                order_service = OrderService(
                    self._session,
                    self._client,
                    self._quote_asset,
                    exchange_info_cache=self._exchange_info_cache,
                )
                result = order_service.create_order(
                    user_id=user_id,
                    symbol=position.instrument,
                    side=cfg.side,
                    order_type="MARKET",
                    quantity=quantity,
                    price=None,
                    idempotency_key=inner_key,
                )
        except TradingDisabledError:
            # Nothing was attempted -- revert to the prior stable status.
            bot.status = prior_status
            self._session.flush()
            response = {
                "bot": bot_to_response(bot),
                "status": "REJECTED",
                "reason": "Trading is disabled.",
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id,
                cfg.audit_action,
                AuditResult.REJECTED,
                "TRADING_ENABLED is false",
                bot=bot,
            )
            return response
        except Exception as exc:
            if not is_simulation:
                self._link_order(user_id, inner_key, bot)
            bot.status = BotStatus.ERROR
            self._session.flush()
            reason = "An unexpected error occurred; this bot requires manual review."
            response = {
                "bot": bot_to_response(bot),
                "status": "ERROR",
                "reason": reason,
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id, cfg.audit_action, AuditResult.FAILED, str(exc)[:1000], bot=bot
            )
            raise

        # Simulation orders resolve fully and synchronously -- there is no
        # NEW/PARTIALLY_FILLED to reconcile, and their id lives in
        # simulation_orders, not orders, so BotPosition's
        # last_close_order_id/last_open_order_id (a real FK into orders)
        # is left untouched for a simulation fill rather than pointed at
        # an id that table doesn't contain.
        order: Order | None = None
        order_id_display: str | None = None
        outcome = result["status"]
        if is_simulation:
            simulated_order = result.get("order")
            order_id_display = simulated_order["id"] if simulated_order else None
        else:
            order = self._link_order(user_id, inner_key, bot)
            if outcome in ("NEW", "PARTIALLY_FILLED") and order is not None:
                outcome = self._reconcile_once(order)
            order_id_display = str(order.id) if order else None

        if outcome == "REJECTED":
            bot.status = prior_status
            self._session.flush()
            response = {
                "bot": bot_to_response(bot),
                "status": "REJECTED",
                "reason": result["reason"],
            }
            finalize(self._session, reservation.key_row_id, response)
            self._audit(
                user_id,
                cfg.audit_action,
                AuditResult.REJECTED,
                result["reason"],
                bot=bot,
            )
            return response

        if outcome == "FILLED":
            if op == "pause":
                position.current_quantity = Decimal("0")
                if not is_simulation:
                    position.last_close_order_id = order.id if order else None
                position.paused_at = datetime.now(UTC)
                bot.status = BotStatus.PAUSED
            else:
                position.current_quantity = position.target_quantity
                if not is_simulation:
                    position.last_open_order_id = order.id if order else None
                position.paused_at = None
                bot.status = BotStatus.ACTIVE
            self._session.flush()
            response = {
                "bot": bot_to_response(bot),
                "status": bot.status.value,
                "reason": None,
            }
            finalize(self._session, reservation.key_row_id, response)
            detail = f"{op.capitalize()} confirmed via order {order_id_display or '?'}."
            self._audit(user_id, cfg.audit_action, AuditResult.SUCCESS, detail, bot=bot)
            return response

        # FAILED, or still non-terminal after the one reconciliation read --
        # ambiguous, never assumed safe.
        bot.status = BotStatus.ERROR
        self._session.flush()
        reason = (
            result.get("reason")
            or "Order outcome could not be confirmed; manual review required."
        )
        response = {"bot": bot_to_response(bot), "status": "ERROR", "reason": reason}
        finalize(self._session, reservation.key_row_id, response)
        self._audit(user_id, cfg.audit_action, AuditResult.FAILED, reason, bot=bot)
        return response

    # --- internals --------------------------------------------------------------

    def _load_bot_for_update(self, user_id: uuid.UUID, bot_id: uuid.UUID) -> Bot | None:
        return self._session.execute(
            select(Bot)
            .where(Bot.id == bot_id, Bot.user_id == user_id)
            .with_for_update()
        ).scalar_one_or_none()

    def _link_order(self, user_id: uuid.UUID, inner_key: str, bot: Bot) -> Order | None:
        """Find the Order `create_order()` just wrote (via the same
        deterministic client-order-id derivation it used internally) and
        tag it with this bot — zero `OrderService` changes needed. Returns
        None only for the TradingDisabledError path, where no Order row
        was ever created."""
        client_order_id = derive_binance_client_order_id(
            user_id, "POST /orders", inner_key
        )
        order = self._session.scalar(
            select(Order).where(Order.binance_client_order_id == client_order_id)
        )
        if order is not None:
            order.bot_id = bot.id
            self._session.flush()
        return order

    def _reconcile_once(self, order: Order) -> str:
        """One reconciliation read for a non-terminal create_order outcome
        — mirrors trading_routes.py's reconcile-on-read, never a retry
        loop. If Binance can't be reached, the order stays non-terminal
        and the caller treats that as unresolved (-> ERROR)."""
        try:
            binance_order = self._client.get_order(
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
        except BinanceError:
            return order.status.value

        reconcile_from_binance_payload(order, binance_order)
        if is_terminal_status(order.status):
            order.terminal_at = datetime.now(UTC)
        self._session.add(
            OrderEvent(
                order_id=order.id,
                event_type=OrderEventType.RECONCILED,
                detail="bot-service reconcile-on-transition",
            )
        )
        self._session.flush()
        return order.status.value

    def _audit(
        self,
        user_id: uuid.UUID,
        action: str,
        result: AuditResult,
        detail: str | None,
        *,
        bot: Bot | None = None,
        bot_id: uuid.UUID | None = None,
    ) -> None:
        resolved_bot_id = bot.id if bot else bot_id
        symbol = bot.position.instrument if bot is not None and bot.position else None
        full_detail = (
            f"bot_id={resolved_bot_id}: {detail}"
            if detail
            else f"bot_id={resolved_bot_id}"
        )
        self._session.add(
            AuditLogEntry(
                user_id=user_id,
                action=action,
                symbol=symbol,
                quantity=None,
                result=result,
                hermes_order_id=None,
                binance_order_id=None,
                detail=full_detail[:1000],
            )
        )
        self._session.flush()


__all__ = [
    "BotNotFoundError",
    "BotService",
    "BotServiceError",
    "InvalidBotTransitionError",
    "bot_to_response",
]
