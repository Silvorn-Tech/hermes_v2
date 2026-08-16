"""Binance REST client — the sole isolation boundary between Hermes and Binance.

Phase 2 (`feature/binance-trading-integration-v1`) scope: everything Phase 1
had (connectivity, account info, balances, public market data, open orders)
plus the minimum write surface real trading needs — placing an order,
cancelling an order, looking up a single order's status, trade history (for
position cost-basis), and exchange filters (for order validation). There is
still no `withdraw`, `transfer`, `deposit_address`, or any other
funds-movement method anywhere in this module, and none should ever be added
here without a new, explicitly-scoped phase — this file is the isolation
boundary between Hermes and Binance, so every funds-movement capability
Binance's API exposes stays unreachable from Hermes by construction, not by
convention. `tests/test_binance_client.py::test_client_exposes_no_write_beyond_orders`
enforces this as a whitelist, not an allowlist-by-omission.

Nothing in this module decides *whether* an order should be placed — no
idempotency, risk, or permission check happens here. This client only knows
how to talk to Binance; `hermes_v2.trading.order_service.OrderService` is the
only caller of `create_order`/`cancel_order` anywhere in the codebase.

Credentials (`BINANCE_API_KEY`, `BINANCE_API_SECRET`) are read from the
environment once per client, exactly like every other credential in this
codebase (see `auth/oauth.py`). Neither value, nor the HMAC signature
derived from the secret, is ever included in a log line or an exception
message — only the request path and Binance's own (non-sensitive) error
`code`/`msg` are.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.binance.com"
_DEFAULT_TIMEOUT_SECONDS = 10
_RECV_WINDOW_MS = 5000


class BinanceError(RuntimeError):
    """Base class for every error this client raises.

    Every subclass's message is built exclusively from this module's own
    strings, an HTTP status code, and Binance's own `code`/`msg` fields —
    never from request headers, query parameters, or the raw response body,
    so no credential or signature can end up in one by accident.
    """


class BinanceConfigurationError(BinanceError):
    """Raised when required Binance credentials/config are missing."""


class BinanceAuthenticationError(BinanceError):
    """Raised when Binance rejects the request's credentials or signature."""


class BinanceRateLimitError(BinanceError):
    """Raised when Binance throttles or bans the caller (HTTP 429/418).

    Distinct from `BinanceRequestError` so callers (in particular
    `OrderService`) can tell "Binance is rate-limiting us" apart from a
    generic failure and decide not to retry a write immediately.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class BinanceRequestError(BinanceError):
    """Raised for any other network, HTTP, or response-parsing failure."""


def _require_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BinanceConfigurationError(f"{name} must be configured")
    return value


def _configured_api_key() -> str:
    return _require_environment_value("BINANCE_API_KEY")


def _configured_api_secret() -> str:
    return _require_environment_value("BINANCE_API_SECRET")


def _configured_base_url() -> str:
    return os.environ.get("BINANCE_BASE_URL", _DEFAULT_BASE_URL)


def _extract_binance_error(response: requests.Response) -> str:
    """Best-effort extraction of Binance's own error code/message.

    Falls back to just the HTTP status if the body isn't the expected
    `{"code": ..., "msg": ...}` shape. Never includes the raw response body,
    headers, or anything from the request — only Binance's own short,
    operator-facing message, which describes the failure, not the secret.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "msg" in payload:
        code = payload.get("code", "?")
        return f"HTTP {response.status_code}, Binance code={code} msg={payload['msg']}"
    return f"HTTP {response.status_code}"


def _extract_symbol_filters(symbol_info: dict[str, Any]) -> dict[str, Any]:
    """Curate `exchangeInfo`'s per-symbol filter list down to what order
    validation actually needs: quantity step size, price tick size, and the
    minimum order notional. Binance has renamed/split these filter types
    across API versions (`MIN_NOTIONAL` vs `NOTIONAL`), so both are checked.
    """
    filters_by_type = {f.get("filterType"): f for f in symbol_info.get("filters", [])}

    lot_size = filters_by_type.get("LOT_SIZE", {})
    price_filter = filters_by_type.get("PRICE_FILTER", {})
    notional_filter = filters_by_type.get("MIN_NOTIONAL") or filters_by_type.get(
        "NOTIONAL", {}
    )

    return {
        "min_qty": lot_size.get("minQty"),
        "max_qty": lot_size.get("maxQty"),
        "step_size": lot_size.get("stepSize"),
        "min_price": price_filter.get("minPrice"),
        "max_price": price_filter.get("maxPrice"),
        "tick_size": price_filter.get("tickSize"),
        "min_notional": notional_filter.get("minNotional"),
    }


class BinanceClient:
    """Minimal Binance REST client — reads plus the minimum write surface.

    Exposes exactly ten capabilities: the five read-only methods from Phase
    1 (`ping`, `get_account_info`, `get_balances`, `get_market_data`,
    `get_open_orders`) plus five added in Phase 2 (`create_order`,
    `cancel_order`, `get_order`, `get_trades`, `get_exchange_info`). Nothing
    that withdraws, transfers, or manages deposit addresses exists here or
    ever should.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else _configured_api_key()
        self._api_secret = (
            api_secret if api_secret is not None else _configured_api_secret()
        )
        self._base_url = (base_url or _configured_base_url()).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._session = session or requests.Session()

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {**params, "signature": signature}

    def _prepare_request(
        self, params: dict[str, Any] | None, signed: bool
    ) -> tuple[dict[str, Any], dict[str, str]]:
        request_params: dict[str, Any] = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            request_params["timestamp"] = int(time.time() * 1000)
            request_params["recvWindow"] = _RECV_WINDOW_MS
            request_params = self._sign(request_params)
            headers = {"X-MBX-APIKEY": self._api_key}
        return request_params, headers

    def _handle_response(
        self, response: requests.Response, method: str, path: str
    ) -> Any:
        if response.status_code in (401, 403):
            logger.warning(
                "Binance authentication failed: method=%s path=%s status=%s",
                method,
                path,
                response.status_code,
            )
            raise BinanceAuthenticationError(_extract_binance_error(response))

        if response.status_code in (418, 429):
            retry_after_header = response.headers.get("Retry-After")
            retry_after_seconds = (
                float(retry_after_header) if retry_after_header else None
            )
            logger.warning(
                "Binance rate limit hit: method=%s path=%s status=%s",
                method,
                path,
                response.status_code,
            )
            raise BinanceRateLimitError(
                _extract_binance_error(response),
                retry_after_seconds=retry_after_seconds,
            )

        if not response.ok:
            logger.warning(
                "Binance request failed: method=%s path=%s status=%s",
                method,
                path,
                response.status_code,
            )
            raise BinanceRequestError(_extract_binance_error(response))

        try:
            return response.json()
        except ValueError as exc:
            logger.warning(
                "Binance returned a malformed response: method=%s path=%s",
                method,
                path,
            )
            raise BinanceRequestError(
                f"Binance returned a malformed response for {method} {path}"
            ) from exc

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        request_params, headers = self._prepare_request(params, signed)
        url = f"{self._base_url}{path}"
        try:
            response = self._session.get(
                url,
                params=request_params,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except requests.Timeout as exc:
            logger.warning("Binance request timed out: path=%s", path)
            raise BinanceRequestError(f"Binance request timed out: {path}") from exc
        except requests.RequestException as exc:
            logger.warning(
                "Binance request failed: path=%s error=%s", path, type(exc).__name__
            )
            raise BinanceRequestError(f"Binance request failed: {path}") from exc

        return self._handle_response(response, "GET", path)

    def _post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        request_params, headers = self._prepare_request(params, signed)
        url = f"{self._base_url}{path}"
        try:
            response = self._session.post(
                url,
                params=request_params,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except requests.Timeout as exc:
            logger.warning("Binance request timed out: path=%s", path)
            raise BinanceRequestError(f"Binance request timed out: {path}") from exc
        except requests.RequestException as exc:
            logger.warning(
                "Binance request failed: path=%s error=%s", path, type(exc).__name__
            )
            raise BinanceRequestError(f"Binance request failed: {path}") from exc

        return self._handle_response(response, "POST", path)

    def _delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        request_params, headers = self._prepare_request(params, signed)
        url = f"{self._base_url}{path}"
        try:
            response = self._session.delete(
                url,
                params=request_params,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except requests.Timeout as exc:
            logger.warning("Binance request timed out: path=%s", path)
            raise BinanceRequestError(f"Binance request timed out: {path}") from exc
        except requests.RequestException as exc:
            logger.warning(
                "Binance request failed: path=%s error=%s", path, type(exc).__name__
            )
            raise BinanceRequestError(f"Binance request failed: {path}") from exc

        return self._handle_response(response, "DELETE", path)

    def ping(self) -> bool:
        """Connectivity check against Binance's public ping endpoint.

        No credentials sent.
        """
        self._get("/api/v3/ping")
        return True

    def get_account_info(self) -> dict[str, Any]:
        """Minimal authenticated account info, used only to confirm auth works.

        Deliberately returns only a handful of flags — never the full
        account payload (which also includes balances, commission rates,
        and permission lists) — callers that need balances use
        `get_balances()` instead.
        """
        payload = self._get("/api/v3/account", signed=True)
        return {
            "account_type": payload.get("accountType"),
            "can_trade": payload.get("canTrade"),
            "can_withdraw": payload.get("canWithdraw"),
            "can_deposit": payload.get("canDeposit"),
        }

    def get_balances(self) -> list[dict[str, Any]]:
        """Non-zero account balances only — asset, free, locked, nothing else."""
        payload = self._get("/api/v3/account", signed=True)
        balances = payload.get("balances", [])
        return [
            {
                "asset": balance["asset"],
                "free": balance["free"],
                "locked": balance["locked"],
            }
            for balance in balances
            if float(balance.get("free", 0)) > 0 or float(balance.get("locked", 0)) > 0
        ]

    def get_market_data(self, symbol: str) -> dict[str, Any]:
        """Public 24h ticker data for `symbol`. No credentials required or sent."""
        payload = self._get("/api/v3/ticker/24hr", params={"symbol": symbol})
        return {
            "symbol": payload.get("symbol"),
            "last_price": payload.get("lastPrice"),
            "price_change_percent": payload.get("priceChangePercent"),
            "high_price": payload.get("highPrice"),
            "low_price": payload.get("lowPrice"),
            "volume": payload.get("volume"),
        }

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Currently open orders."""
        params = {"symbol": symbol} if symbol else {}
        payload = self._get("/api/v3/openOrders", params=params, signed=True)
        return [
            {
                "symbol": order.get("symbol"),
                "order_id": order.get("orderId"),
                "side": order.get("side"),
                "type": order.get("type"),
                "price": order.get("price"),
                "orig_qty": order.get("origQty"),
                "executed_qty": order.get("executedQty"),
                "status": order.get("status"),
                "time": order.get("time"),
            }
            for order in payload
        ]

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a new order. Signed, write, real money.

        `side` must be `"BUY"`/`"SELL"`, `order_type` `"MARKET"`/`"LIMIT"`.
        `price` is required for `LIMIT` orders (Binance requires
        `timeInForce=GTC` alongside it) and must be omitted for `MARKET`.
        `client_order_id`, when given, becomes Binance's `newClientOrderId`
        — the caller (`OrderService`) is responsible for making this
        deterministic/idempotent; this method does not generate one itself.

        Never called directly by an API route — only `OrderService` calls
        this, after validation, risk checks, and idempotency have already
        run.
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }
        if price is not None:
            params["price"] = price
            params["timeInForce"] = "GTC"
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id

        payload = self._post("/api/v3/order", params=params, signed=True)
        return {
            "symbol": payload.get("symbol"),
            "order_id": payload.get("orderId"),
            "client_order_id": payload.get("clientOrderId"),
            "status": payload.get("status"),
            "side": payload.get("side"),
            "type": payload.get("type"),
            "price": payload.get("price"),
            "orig_qty": payload.get("origQty"),
            "executed_qty": payload.get("executedQty"),
            "cummulative_quote_qty": payload.get("cummulativeQuoteQty"),
            "transact_time": payload.get("transactTime"),
        }

    def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an existing order. Signed, write.

        Exactly one of `order_id` (Binance's own) or `client_order_id`
        (`origClientOrderId`) must identify the order.
        """
        if order_id is None and client_order_id is None:
            raise ValueError("cancel_order requires order_id or client_order_id")

        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id

        payload = self._delete("/api/v3/order", params=params, signed=True)
        return {
            "symbol": payload.get("symbol"),
            "order_id": payload.get("orderId"),
            "client_order_id": payload.get("clientOrderId"),
            "status": payload.get("status"),
            "orig_qty": payload.get("origQty"),
            "executed_qty": payload.get("executedQty"),
        }

    def get_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Look up one order's current status. Signed, read.

        Used both for `GET /orders/{id}` reconciliation and, critically, to
        confirm whether an order that appeared to time out on submission
        actually reached Binance (see `OrderService`'s idempotency handling)
        — never assume a failed HTTP call means no order was created.
        """
        if order_id is None and client_order_id is None:
            raise ValueError("get_order requires order_id or client_order_id")

        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id

        payload = self._get("/api/v3/order", params=params, signed=True)
        return {
            "symbol": payload.get("symbol"),
            "order_id": payload.get("orderId"),
            "client_order_id": payload.get("clientOrderId"),
            "status": payload.get("status"),
            "side": payload.get("side"),
            "type": payload.get("type"),
            "price": payload.get("price"),
            "orig_qty": payload.get("origQty"),
            "executed_qty": payload.get("executedQty"),
            "cummulative_quote_qty": payload.get("cummulativeQuoteQty"),
            "time": payload.get("time"),
            "update_time": payload.get("updateTime"),
            "is_working": payload.get("isWorking"),
        }

    def get_trades(self, symbol: str) -> list[dict[str, Any]]:
        """This account's executed trades for `symbol`. Signed, read.

        Used to compute a position's weighted-average entry price — Binance
        Spot has no position endpoint, only a trade ledger.
        """
        payload = self._get("/api/v3/myTrades", params={"symbol": symbol}, signed=True)
        return [
            {
                "id": trade.get("id"),
                "order_id": trade.get("orderId"),
                "price": trade.get("price"),
                "qty": trade.get("qty"),
                "quote_qty": trade.get("quoteQty"),
                "commission": trade.get("commission"),
                "commission_asset": trade.get("commissionAsset"),
                "time": trade.get("time"),
                "is_buyer": trade.get("isBuyer"),
                "is_maker": trade.get("isMaker"),
            }
            for trade in payload
        ]

    def get_exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        """Public trading rules for `symbol` — precision and order filters.

        Unsigned, no credentials sent. Used by `OrderValidator` to check
        quantity step size, price tick size, and minimum notional before an
        order is ever sent to `create_order`.
        """
        params = {"symbol": symbol} if symbol else {}
        payload = self._get("/api/v3/exchangeInfo", params=params)
        symbols = payload.get("symbols", [])
        if not symbols:
            raise BinanceRequestError(
                f"Binance returned no exchange info for symbol={symbol!r}"
            )
        symbol_info = symbols[0]
        return {
            "symbol": symbol_info.get("symbol"),
            "status": symbol_info.get("status"),
            "base_asset": symbol_info.get("baseAsset"),
            "quote_asset": symbol_info.get("quoteAsset"),
            "base_asset_precision": symbol_info.get("baseAssetPrecision"),
            "quote_asset_precision": symbol_info.get("quoteAssetPrecision"),
            "filters": _extract_symbol_filters(symbol_info),
        }
