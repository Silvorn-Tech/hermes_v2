"""Tests for Binance-status-to-Hermes-status reconciliation."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from hermes_v2.trading.models import OrderStatus
from hermes_v2.trading.reconciliation import (
    UnknownBinanceStatusError,
    map_binance_status,
    reconcile_from_binance_payload,
)


def _order(
    status: OrderStatus = OrderStatus.PENDING,
    binance_order_id: str | None = None,
    executed_quantity: Decimal = Decimal("0"),
    average_fill_price: Decimal | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        binance_order_id=binance_order_id,
        executed_quantity=executed_quantity,
        average_fill_price=average_fill_price,
    )


# --- map_binance_status --------------------------------------------------------


@pytest.mark.parametrize(
    ("binance_status", "expected"),
    [
        ("NEW", OrderStatus.NEW),
        ("PARTIALLY_FILLED", OrderStatus.PARTIALLY_FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELED),
        ("PENDING_CANCEL", OrderStatus.PENDING_CANCEL),
        ("REJECTED", OrderStatus.REJECTED),
        ("EXPIRED", OrderStatus.EXPIRED),
    ],
)
def test_map_binance_status_covers_every_documented_state(
    binance_status: str, expected: OrderStatus
) -> None:
    assert map_binance_status(binance_status) == expected


def test_map_binance_status_raises_on_unknown_status() -> None:
    with pytest.raises(UnknownBinanceStatusError, match="SOMETHING_NEW"):
        map_binance_status("SOMETHING_NEW")


# --- reconcile_from_binance_payload ---------------------------------------------


def test_reconcile_updates_status_and_reports_changed() -> None:
    order = _order(status=OrderStatus.PENDING)

    changed = reconcile_from_binance_payload(order, {"status": "NEW"})

    assert order.status == OrderStatus.NEW
    assert changed is True


def test_reconcile_reports_unchanged_when_nothing_differs() -> None:
    order = _order(status=OrderStatus.NEW, binance_order_id="123")

    changed = reconcile_from_binance_payload(order, {"status": "NEW", "order_id": 123})

    assert changed is False


def test_reconcile_sets_binance_order_id() -> None:
    order = _order()

    reconcile_from_binance_payload(order, {"order_id": 555})

    assert order.binance_order_id == "555"


def test_reconcile_updates_executed_quantity() -> None:
    order = _order(executed_quantity=Decimal("0"))

    reconcile_from_binance_payload(order, {"executed_qty": "0.004"})

    assert order.executed_quantity == Decimal("0.004")


def test_reconcile_computes_average_fill_price_from_cumulative_quote_qty() -> None:
    order = _order()

    reconcile_from_binance_payload(
        order, {"executed_qty": "0.01", "cummulative_quote_qty": "500.00"}
    )

    assert order.average_fill_price == Decimal("50000.00")


def test_reconcile_leaves_average_fill_price_none_when_nothing_executed() -> None:
    order = _order()

    reconcile_from_binance_payload(
        order, {"status": "NEW", "executed_qty": "0", "cummulative_quote_qty": "0"}
    )

    assert order.average_fill_price is None


def test_reconcile_ignores_fields_absent_from_a_partial_payload() -> None:
    """cancel_order()'s curated response has no cummulative_quote_qty — must
    not be treated as "reset average price to unknown"."""
    order = _order(
        status=OrderStatus.PARTIALLY_FILLED,
        average_fill_price=Decimal("50000"),
        executed_quantity=Decimal("0.004"),
    )

    changed = reconcile_from_binance_payload(
        order, {"status": "CANCELED", "executed_qty": "0.004"}
    )

    assert order.status == OrderStatus.CANCELED
    assert order.average_fill_price == Decimal("50000")  # untouched
    assert changed is True


def test_reconcile_raises_for_unmapped_status_and_leaves_order_untouched() -> None:
    order = _order(status=OrderStatus.NEW)

    with pytest.raises(UnknownBinanceStatusError):
        reconcile_from_binance_payload(order, {"status": "SOMETHING_NEW"})

    assert order.status == OrderStatus.NEW
