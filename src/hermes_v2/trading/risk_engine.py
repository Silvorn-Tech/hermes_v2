"""RiskEngine — the last gate between a validated order and Binance.

Every limit comes from a `RiskLimits` the caller builds and passes in —
this module invents no thresholds of its own and reads no configuration
directly. If a specific limit is `None` ("not configured"), the check
that depends on it **rejects** rather than passing silently. Concretely:
until every one of the six fields is set, `RiskEngine.validate_order()`
rejects every order, regardless of how safe it might be. This mirrors
the codebase's existing fail-closed precedent — `HERMES_ALLOWED_ORIGINS`
unset means CORS denies every origin (`api/app.py`), an unrecognized
permission means `require_permission()` denies by construction
(`auth/authorization.py`) — applied here to money instead of access.

Where a caller's `RiskLimits` come from is deliberately their own
decision, not this module's: `OrderService` and `SimulationOrderService`
each source per-user limits from `user_risk_settings_service.py`, with
opposite defaults for "no row yet" (fail-closed for real orders, since
real money is at stake and a user must consciously choose their own
limits; sensible non-`None` defaults for Simulation, since it's virtual
money and must never require setup) — see that module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    """One user's configured risk limits. `None` means "not configured"
    (only a valid state for real-order limits — see
    `user_risk_settings_service.py`)."""

    max_order_notional_quote: Decimal | None
    max_symbol_exposure_pct: Decimal | None
    max_total_exposure_pct: Decimal | None
    max_daily_loss_pct: Decimal | None
    max_open_positions: int | None
    allowed_symbols: frozenset[str] | None


@dataclass(frozen=True)
class OrderRiskRequest:
    """The order-shaped facts RiskEngine needs. Built by OrderService from an
    already-validated order (OrderValidator has already run)."""

    symbol: str
    side: str  # "BUY" or "SELL"
    estimated_notional_quote: Decimal
    is_new_symbol_for_account: (
        bool  # True only for a BUY opening a symbol the account doesn't already hold
    )


@dataclass(frozen=True)
class AccountRiskSnapshot:
    """The account-shaped facts RiskEngine needs, built by OrderService from
    PortfolioService/PositionsService. Never mutated by RiskEngine."""

    total_portfolio_value_quote: Decimal
    current_symbol_exposure_quote: Decimal
    current_total_exposure_quote: Decimal
    open_position_count: int
    realized_loss_today_quote: Decimal  # 0 or positive; a positive number is a loss


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str | None = None


class RiskEngine:
    """Stateless: every call is a pure function of (limits, request, snapshot)."""

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def validate_order(
        self, request: OrderRiskRequest, snapshot: AccountRiskSnapshot
    ) -> RiskDecision:
        # Decimal NaN/Infinity comparisons (<=, >, ...) raise
        # InvalidOperation instead of returning False the way float NaN
        # does. OrderValidator already rejects a non-finite order
        # quantity/price before RiskEngine ever runs, but the account-state
        # values here (from PortfolioService/PositionsService, ultimately
        # from Binance's own balance/trade data) aren't re-validated
        # upstream — so this checks them too, before any comparison touches
        # them, rather than letting an uncaught InvalidOperation escape as
        # a generic internal error instead of RiskEngine's own clean,
        # fail-closed rejection.
        non_finite_check = self._check_all_values_finite(request, snapshot)
        if not non_finite_check.approved:
            return non_finite_check

        symbol_check = self._check_allowed_symbol(request)
        if not symbol_check.approved:
            return symbol_check

        daily_loss_check = self._check_daily_loss(snapshot)
        if not daily_loss_check.approved:
            return daily_loss_check

        notional_check = self._check_order_notional(request)
        if not notional_check.approved:
            return notional_check

        if request.side == "BUY":
            exposure_check = self._check_symbol_exposure(request, snapshot)
            if not exposure_check.approved:
                return exposure_check

            total_exposure_check = self._check_total_exposure(request, snapshot)
            if not total_exposure_check.approved:
                return total_exposure_check

            open_positions_check = self._check_open_positions(request, snapshot)
            if not open_positions_check.approved:
                return open_positions_check

        return RiskDecision(approved=True)

    def _check_all_values_finite(
        self, request: OrderRiskRequest, snapshot: AccountRiskSnapshot
    ) -> RiskDecision:
        values = {
            "estimated_notional_quote": request.estimated_notional_quote,
            "total_portfolio_value_quote": snapshot.total_portfolio_value_quote,
            "current_symbol_exposure_quote": snapshot.current_symbol_exposure_quote,
            "current_total_exposure_quote": snapshot.current_total_exposure_quote,
            "realized_loss_today_quote": snapshot.realized_loss_today_quote,
        }
        for name, value in values.items():
            if not value.is_finite():
                return RiskDecision(
                    approved=False, reason=f"{name} is not a finite number ({value})"
                )
        return RiskDecision(approved=True)

    def _check_allowed_symbol(self, request: OrderRiskRequest) -> RiskDecision:
        if not self._limits.allowed_symbols:
            return RiskDecision(
                approved=False,
                reason=(
                    "HERMES_RISK_ALLOWED_SYMBOLS is not configured; "
                    "no symbol is tradable"
                ),
            )
        if request.symbol.upper() not in self._limits.allowed_symbols:
            return RiskDecision(
                approved=False, reason=f"{request.symbol} is not an allowed symbol"
            )
        return RiskDecision(approved=True)

    def _check_daily_loss(self, snapshot: AccountRiskSnapshot) -> RiskDecision:
        limit = self._limits.max_daily_loss_pct
        if limit is None:
            return RiskDecision(
                approved=False,
                reason="HERMES_RISK_MAX_DAILY_LOSS_PCT is not configured",
            )
        if snapshot.total_portfolio_value_quote <= 0:
            return RiskDecision(
                approved=False, reason="Portfolio value is zero or unavailable"
            )
        loss_pct = (
            snapshot.realized_loss_today_quote
            / snapshot.total_portfolio_value_quote
            * 100
        )
        if loss_pct >= limit:
            return RiskDecision(
                approved=False,
                reason=f"Daily loss {loss_pct:.2f}% has reached the {limit}% limit",
            )
        return RiskDecision(approved=True)

    def _check_order_notional(self, request: OrderRiskRequest) -> RiskDecision:
        limit = self._limits.max_order_notional_quote
        if limit is None:
            return RiskDecision(
                approved=False,
                reason="HERMES_RISK_MAX_ORDER_NOTIONAL_USD is not configured",
            )
        if request.estimated_notional_quote > limit:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Order notional {request.estimated_notional_quote} exceeds the "
                    f"{limit} limit"
                ),
            )
        return RiskDecision(approved=True)

    def _check_symbol_exposure(
        self, request: OrderRiskRequest, snapshot: AccountRiskSnapshot
    ) -> RiskDecision:
        limit = self._limits.max_symbol_exposure_pct
        if limit is None:
            return RiskDecision(
                approved=False,
                reason="HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT is not configured",
            )
        if snapshot.total_portfolio_value_quote <= 0:
            return RiskDecision(
                approved=False, reason="Portfolio value is zero or unavailable"
            )
        projected_exposure = (
            snapshot.current_symbol_exposure_quote + request.estimated_notional_quote
        )
        projected_pct = projected_exposure / snapshot.total_portfolio_value_quote * 100
        if projected_pct > limit:
            return RiskDecision(
                approved=False,
                reason=(
                    f"{request.symbol} exposure would reach {projected_pct:.2f}%, "
                    f"over the {limit}% limit"
                ),
            )
        return RiskDecision(approved=True)

    def _check_total_exposure(
        self, request: OrderRiskRequest, snapshot: AccountRiskSnapshot
    ) -> RiskDecision:
        limit = self._limits.max_total_exposure_pct
        if limit is None:
            return RiskDecision(
                approved=False,
                reason="HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT is not configured",
            )
        if snapshot.total_portfolio_value_quote <= 0:
            return RiskDecision(
                approved=False, reason="Portfolio value is zero or unavailable"
            )
        projected_exposure = (
            snapshot.current_total_exposure_quote + request.estimated_notional_quote
        )
        projected_pct = projected_exposure / snapshot.total_portfolio_value_quote * 100
        if projected_pct > limit:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Total exposure would reach {projected_pct:.2f}%, over the "
                    f"{limit}% limit"
                ),
            )
        return RiskDecision(approved=True)

    def _check_open_positions(
        self, request: OrderRiskRequest, snapshot: AccountRiskSnapshot
    ) -> RiskDecision:
        limit = self._limits.max_open_positions
        if limit is None:
            return RiskDecision(
                approved=False,
                reason="HERMES_RISK_MAX_OPEN_POSITIONS is not configured",
            )
        if request.is_new_symbol_for_account and snapshot.open_position_count >= limit:
            return RiskDecision(
                approved=False,
                reason=f"Already at the {limit}-open-position limit",
            )
        return RiskDecision(approved=True)


__all__ = [
    "AccountRiskSnapshot",
    "OrderRiskRequest",
    "RiskDecision",
    "RiskEngine",
    "RiskLimits",
]
