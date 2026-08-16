"""Read-only Binance REST client.

Phase 1 (`feature/binance-read-only-v1`) scope, deliberately: connectivity,
account info, balances, public market data, and open orders. There is no
`create_order`, `cancel_order`, `buy`, `sell`, `withdraw`, or `transfer`
method anywhere in this module, and none should ever be added here without a
new, explicitly-scoped phase — this file is the isolation boundary between
Hermes and Binance, so every write capability Binance's API exposes stays
unreachable from Hermes by construction, not by convention.

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


class BinanceClient:
    """Minimal, read-only Binance REST client.

    Exposes exactly five capabilities — `ping`, `get_account_info`,
    `get_balances`, `get_market_data`, `get_open_orders` — and nothing that
    creates, cancels, or modifies anything on Binance.
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

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        request_params: dict[str, Any] = dict(params or {})
        headers: dict[str, str] = {}

        if signed:
            request_params["timestamp"] = int(time.time() * 1000)
            request_params["recvWindow"] = _RECV_WINDOW_MS
            request_params = self._sign(request_params)
            headers = {"X-MBX-APIKEY": self._api_key}

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

        if response.status_code in (401, 403):
            logger.warning(
                "Binance authentication failed: path=%s status=%s",
                path,
                response.status_code,
            )
            raise BinanceAuthenticationError(_extract_binance_error(response))

        if not response.ok:
            logger.warning(
                "Binance request failed: path=%s status=%s", path, response.status_code
            )
            raise BinanceRequestError(_extract_binance_error(response))

        try:
            return response.json()
        except ValueError as exc:
            logger.warning("Binance returned a malformed response: path=%s", path)
            raise BinanceRequestError(
                f"Binance returned a malformed response for {path}"
            ) from exc

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
        """Currently open orders.

        Read-only — there is no way to act on what this returns.
        """
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
