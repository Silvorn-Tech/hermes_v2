"""Reconciliation — Binance is the final authority on order state.

A successful HTTP response from `BinanceClient` (create/cancel/get) is
never assumed to mean an order reached its final state — only the mapped
`OrderStatus` here does. This module has two callers, both synchronous
(there is no background worker/scheduler anywhere in this codebase, and
adding one is out of scope for this pass — a deliberate, documented
boundary, not an oversight):

1. `OrderService`, immediately after submitting/cancelling an order — the
   Binance response itself is reconciled into the `Order` row.
2. The `GET /orders`/`GET /orders/{id}` routes, "reconcile-on-read": if a
   stored order is non-terminal, they call `BinanceClient.get_order()`
   again before returning, so a stale `NEW` never lingers past the next
   time anyone actually looks at it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hermes_v2.trading.models import OrderStatus

_BINANCE_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.PENDING_CANCEL,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}


class UnknownBinanceStatusError(ValueError):
    """Binance returned an order status this module doesn't have a mapping
    for. Raised rather than silently guessed at — an operator needs to see
    this and extend `_BINANCE_STATUS_MAP` deliberately, not have Hermes
    invent a status."""


def map_binance_status(binance_status: str) -> OrderStatus:
    try:
        return _BINANCE_STATUS_MAP[binance_status]
    except KeyError:
        raise UnknownBinanceStatusError(
            f"No Hermes OrderStatus mapping for Binance status {binance_status!r}"
        ) from None


def reconcile_from_binance_payload(order: Any, binance_order: dict[str, Any]) -> bool:
    """Update `order` in place from a curated `BinanceClient` order payload
    (the dict shape returned by `create_order`/`cancel_order`/`get_order`).

    Returns whether anything actually changed, so callers can decide
    whether an `OrderEvent` row is worth writing. Never assumes a field's
    absence means zero/unchanged — a partial payload (e.g. `cancel_order`'s
    curated response has no `cummulative_quote_qty`) only updates the
    fields it actually carries.
    """
    changed = False

    binance_status = binance_order.get("status")
    if binance_status is not None:
        new_status = map_binance_status(binance_status)
        if order.status != new_status:
            order.status = new_status
            changed = True

    binance_order_id = binance_order.get("order_id")
    if binance_order_id is not None and order.binance_order_id != str(binance_order_id):
        order.binance_order_id = str(binance_order_id)
        changed = True

    executed_qty = binance_order.get("executed_qty")
    if executed_qty is not None:
        new_executed_qty = Decimal(str(executed_qty))
        if order.executed_quantity != new_executed_qty:
            order.executed_quantity = new_executed_qty
            changed = True

    average_fill_price = _average_fill_price(binance_order)
    if (
        average_fill_price is not None
        and order.average_fill_price != average_fill_price
    ):
        order.average_fill_price = average_fill_price
        changed = True

    return changed


def _average_fill_price(binance_order: dict[str, Any]) -> Decimal | None:
    """Binance doesn't return an average price directly on order
    create/cancel/get responses — it returns cumulative quote quantity and
    executed base quantity, from which the average is derived. Returns
    `None` (never a fabricated 0) when nothing has filled yet."""
    cumulative_quote_qty = binance_order.get("cummulative_quote_qty")
    executed_qty = binance_order.get("executed_qty")
    if cumulative_quote_qty is None or executed_qty is None:
        return None
    executed = Decimal(str(executed_qty))
    if executed == 0:
        return None
    return Decimal(str(cumulative_quote_qty)) / executed


__all__ = [
    "UnknownBinanceStatusError",
    "map_binance_status",
    "reconcile_from_binance_payload",
]
