"""Hermes v2 trading domain: order execution, risk, and reconciliation.

Nothing under `hermes_v2.api` calls `hermes_v2.integrations.binance.BinanceClient`
directly — every mutating action goes through `OrderService`
(`hermes_v2.trading.order_service`), which is the only caller of
`BinanceClient.create_order`/`cancel_order` anywhere in the codebase.
"""
