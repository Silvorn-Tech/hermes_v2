"""OrderValidator — checks an order against Binance's own trading rules.

Pure and stateless: every call is a function of `(request, market_price,
filters)`, none of which this module fetches itself — `OrderService` gathers
`market_price` (`BinanceClient.get_market_data`) and `filters`
(`ExchangeInfoCache`) and passes them in, so this module never talks to
Binance and stays trivially unit-testable.

This runs *before* `RiskEngine` in `OrderService`'s pipeline: there is no
point evaluating exposure limits against an order Binance would reject
outright for a bad tick size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class OrderValidationRequest:
    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "MARKET" or "LIMIT"
    quantity: Decimal
    price: Decimal | None  # required for LIMIT, must be None for MARKET


@dataclass(frozen=True)
class SymbolFilters:
    status: str
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_notional: Decimal


class SymbolFiltersError(ValueError):
    """Raised when Binance's exchangeInfo for a symbol is missing a filter
    this validator needs — never silently skipped."""


def symbol_filters_from_exchange_info(info: dict[str, Any]) -> SymbolFilters:
    """Convert `BinanceClient.get_exchange_info()`'s curated dict (string
    fields) into a `SymbolFilters` of exact `Decimal`s."""
    filters = info.get("filters", {})
    required = (
        "min_qty",
        "max_qty",
        "step_size",
        "min_price",
        "max_price",
        "tick_size",
        "min_notional",
    )
    missing = [name for name in required if filters.get(name) is None]
    if missing:
        raise SymbolFiltersError(
            f"exchangeInfo for {info.get('symbol')!r} is missing filter(s): "
            f"{', '.join(missing)}"
        )
    try:
        return SymbolFilters(
            status=info.get("status", ""),
            min_qty=Decimal(filters["min_qty"]),
            max_qty=Decimal(filters["max_qty"]),
            step_size=Decimal(filters["step_size"]),
            min_price=Decimal(filters["min_price"]),
            max_price=Decimal(filters["max_price"]),
            tick_size=Decimal(filters["tick_size"]),
            min_notional=Decimal(filters["min_notional"]),
        )
    except InvalidOperation as exc:
        raise SymbolFiltersError(
            f"exchangeInfo for {info.get('symbol')!r} has a non-numeric filter value"
        ) from exc


@dataclass(frozen=True)
class OrderValidationResult:
    approved: bool
    reason: str | None = None
    estimated_notional_quote: Decimal | None = None


_VALID_SIDES = frozenset({"BUY", "SELL"})
_VALID_TYPES = frozenset({"MARKET", "LIMIT"})


class OrderValidator:
    def validate(
        self,
        request: OrderValidationRequest,
        market_price: Decimal,
        filters: SymbolFilters,
    ) -> OrderValidationResult:
        if request.side not in _VALID_SIDES:
            return OrderValidationResult(
                approved=False, reason=f"Invalid side: {request.side!r}"
            )
        if request.order_type not in _VALID_TYPES:
            return OrderValidationResult(
                approved=False, reason=f"Invalid order type: {request.order_type!r}"
            )

        # Decimal NaN/Infinity comparisons (<=, >, etc.) raise InvalidOperation
        # instead of returning False the way float NaN comparisons silently
        # do — so this must be checked with .is_finite() (a query, never
        # raises) *before* any ordering comparison touches these values,
        # including market_price and filters below. Reject explicitly rather
        # than letting an InvalidOperation escape uncaught: OrderService's
        # idempotency reservation is only finalized by the return value this
        # function produces, never by an exception handler above it.
        if not request.quantity.is_finite():
            return OrderValidationResult(
                approved=False,
                reason=f"Quantity must be a finite number, got {request.quantity}",
            )
        if request.price is not None and not request.price.is_finite():
            return OrderValidationResult(
                approved=False,
                reason=f"Price must be a finite number, got {request.price}",
            )
        if not market_price.is_finite():
            return OrderValidationResult(
                approved=False, reason="Market price is unavailable or non-finite"
            )

        if request.quantity <= 0:
            return OrderValidationResult(
                approved=False, reason="Quantity must be positive"
            )

        if request.order_type == "MARKET" and request.price is not None:
            return OrderValidationResult(
                approved=False, reason="MARKET orders must not specify a price"
            )
        if request.order_type == "LIMIT" and (
            request.price is None or request.price <= 0
        ):
            return OrderValidationResult(
                approved=False, reason="LIMIT orders require a positive price"
            )

        if filters.status != "TRADING":
            return OrderValidationResult(
                approved=False,
                reason=f"{request.symbol} is not currently tradable ({filters.status})",
            )

        if not (filters.min_qty <= request.quantity <= filters.max_qty):
            return OrderValidationResult(
                approved=False,
                reason=(
                    f"Quantity {request.quantity} is outside the allowed range "
                    f"[{filters.min_qty}, {filters.max_qty}]"
                ),
            )
        if filters.step_size > 0 and (
            (request.quantity - filters.min_qty) % filters.step_size != 0
        ):
            return OrderValidationResult(
                approved=False,
                reason=(
                    f"Quantity {request.quantity} does not match step size "
                    f"{filters.step_size}"
                ),
            )

        effective_price = market_price
        if request.order_type == "LIMIT":
            effective_price = request.price
            if not (filters.min_price <= request.price <= filters.max_price):
                return OrderValidationResult(
                    approved=False,
                    reason=(
                        f"Price {request.price} is outside the allowed range "
                        f"[{filters.min_price}, {filters.max_price}]"
                    ),
                )
            if filters.tick_size > 0 and (
                (request.price - filters.min_price) % filters.tick_size != 0
            ):
                return OrderValidationResult(
                    approved=False,
                    reason=(
                        f"Price {request.price} does not match tick size "
                        f"{filters.tick_size}"
                    ),
                )

        estimated_notional = request.quantity * effective_price
        if estimated_notional < filters.min_notional:
            return OrderValidationResult(
                approved=False,
                reason=(
                    f"Order notional {estimated_notional} is below the minimum "
                    f"{filters.min_notional}"
                ),
            )

        return OrderValidationResult(
            approved=True, estimated_notional_quote=estimated_notional
        )


__all__ = [
    "OrderValidationRequest",
    "OrderValidationResult",
    "OrderValidator",
    "SymbolFilters",
    "SymbolFiltersError",
    "symbol_filters_from_exchange_info",
]
