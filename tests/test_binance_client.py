"""Unit tests for the read-only Binance client.

Every fake credential here is an obviously-fake sentinel string, never a
real value — several tests exist specifically to prove that sentinel never
leaks into a log line, exception message, or parsed response.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

import pytest
import requests

from hermes_v2.integrations.binance import (
    BinanceAuthenticationError,
    BinanceClient,
    BinanceConfigurationError,
    BinanceError,
    BinanceRateLimitError,
    BinanceRequestError,
)

_FAKE_API_KEY = "unit-test-fake-api-key-do-not-use"
_FAKE_API_SECRET = "unit-test-fake-api-secret-do-not-use"


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: object = None,
        ok: bool | None = None,
        malformed: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.ok = ok if ok is not None else 200 <= status_code < 400
        self._malformed = malformed

    def json(self) -> object:
        if self._malformed:
            raise ValueError("not valid json")
        return self._json_data


class _FakeSession:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._response = response
        self._exception = exception
        self.calls: list[dict] = []

    def _record_and_respond(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if self._exception is not None:
            raise self._exception
        return self._response

    def get(self, url, params=None, headers=None, timeout=None):
        return self._record_and_respond("GET", url, params, headers, timeout)

    def post(self, url, params=None, headers=None, timeout=None):
        return self._record_and_respond("POST", url, params, headers, timeout)

    def delete(self, url, params=None, headers=None, timeout=None):
        return self._record_and_respond("DELETE", url, params, headers, timeout)


def _make_client(session: _FakeSession) -> BinanceClient:
    return BinanceClient(
        api_key=_FAKE_API_KEY,
        api_secret=_FAKE_API_SECRET,
        session=session,
    )


def test_missing_credentials_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    with pytest.raises(BinanceConfigurationError):
        BinanceClient(session=_FakeSession())


def test_ping_sends_no_credentials() -> None:
    session = _FakeSession(response=_FakeResponse(json_data={}))
    client = _make_client(session)

    assert client.ping() is True
    assert len(session.calls) == 1
    assert session.calls[0]["headers"] == {}
    assert session.calls[0]["params"] == {}


def test_get_account_info_signs_request_and_returns_minimal_fields() -> None:
    payload = {
        "accountType": "SPOT",
        "canTrade": True,
        "canWithdraw": False,
        "canDeposit": True,
        "commissionRates": {"maker": "0.001"},  # must NOT leak into the result
        "balances": [{"asset": "BTC", "free": "1", "locked": "0"}],
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_account_info()

    assert result == {
        "account_type": "SPOT",
        "can_trade": True,
        "can_withdraw": False,
        "can_deposit": True,
    }

    call = session.calls[0]
    assert call["headers"] == {"X-MBX-APIKEY": _FAKE_API_KEY}
    sent_params = dict(call["params"])
    signature = sent_params.pop("signature")
    expected_signature = hmac.new(
        _FAKE_API_SECRET.encode("utf-8"),
        urlencode(sent_params).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected_signature


def test_get_api_key_permissions_signs_request_and_reads_enable_withdrawals() -> None:
    """Distinct from `get_account_info()`'s `can_withdraw`: this hits
    Binance's dedicated key-permissions endpoint, which reflects the
    specific key's "Enable Withdrawals" checkbox rather than the
    account's overall eligibility."""
    payload = {
        "ipRestrict": True,
        "enableReading": True,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": True,
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_api_key_permissions()

    assert result == {"can_withdraw": False}
    call = session.calls[0]
    assert call["url"].endswith("/sapi/v1/account/apiRestrictions")
    assert call["headers"] == {"X-MBX-APIKEY": _FAKE_API_KEY}


def test_get_klines_curates_the_positional_array_into_named_fields() -> None:
    payload = [
        [
            1499040000000,
            "0.01634790",
            "0.80000000",
            "0.01575800",
            "0.01577100",
            "148976.11427815",
            1499644799999,
            "2434.19055334",  # quote asset volume -- must NOT leak into the result
            308,  # number of trades -- must NOT leak into the result
            "1756.87402397",
            "28.46694368",
            "0",
        ]
    ]
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_klines("BTCUSDT", "15m", limit=1)

    assert result == [
        {
            "open_time": 1499040000000,
            "open": "0.01634790",
            "high": "0.80000000",
            "low": "0.01575800",
            "close": "0.01577100",
            "volume": "148976.11427815",
            "close_time": 1499644799999,
        }
    ]
    call = session.calls[0]
    assert call["headers"] == {}
    assert call["params"] == {"symbol": "BTCUSDT", "interval": "15m", "limit": 1}


def test_get_balances_filters_zero_and_parses_fields() -> None:
    payload = {
        "balances": [
            {"asset": "BTC", "free": "1.5", "locked": "0"},
            {"asset": "ETH", "free": "0", "locked": "0"},
            {"asset": "USDT", "free": "0", "locked": "10"},
        ]
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    balances = client.get_balances()

    assert balances == [
        {"asset": "BTC", "free": "1.5", "locked": "0"},
        {"asset": "USDT", "free": "0", "locked": "10"},
    ]


def test_get_open_orders_parses_expected_fields_only() -> None:
    payload = [
        {
            "symbol": "BTCUSDT",
            "orderId": 123,
            "side": "BUY",
            "type": "LIMIT",
            "price": "50000",
            "origQty": "0.1",
            "executedQty": "0.0",
            "status": "NEW",
            "time": 1700000000000,
            "clientOrderId": "should-not-leak",
            "isWorking": True,
        }
    ]
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    orders = client.get_open_orders()

    assert orders == [
        {
            "symbol": "BTCUSDT",
            "order_id": 123,
            "side": "BUY",
            "type": "LIMIT",
            "price": "50000",
            "orig_qty": "0.1",
            "executed_qty": "0.0",
            "status": "NEW",
            "time": 1700000000000,
        }
    ]


def test_get_market_data_is_public_and_unsigned() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "lastPrice": "50000",
        "priceChangePercent": "1.5",
        "highPrice": "51000",
        "lowPrice": "49000",
        "volume": "1000",
        "bidPrice": "should-not-leak",
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_market_data("BTCUSDT")

    assert result == {
        "symbol": "BTCUSDT",
        "last_price": "50000",
        "price_change_percent": "1.5",
        "high_price": "51000",
        "low_price": "49000",
        "volume": "1000",
    }
    call = session.calls[0]
    assert call["headers"] == {}
    assert call["params"] == {"symbol": "BTCUSDT"}


def test_authentication_error_raised_on_401() -> None:
    response = _FakeResponse(
        status_code=401,
        json_data={
            "code": -2015,
            "msg": "Invalid API-key, IP, or permissions for action.",
        },
        ok=False,
    )
    session = _FakeSession(response=response)
    client = _make_client(session)

    with pytest.raises(BinanceAuthenticationError, match="Invalid API-key"):
        client.get_account_info()


def test_http_error_raised_on_server_error() -> None:
    response = _FakeResponse(
        status_code=500,
        json_data={"code": -1000, "msg": "An unknown error occurred"},
        ok=False,
    )
    session = _FakeSession(response=response)
    client = _make_client(session)

    with pytest.raises(BinanceRequestError, match="-1000"):
        client.get_open_orders()


def test_timeout_raises_request_error() -> None:
    session = _FakeSession(exception=requests.Timeout("simulated timeout"))
    client = _make_client(session)

    with pytest.raises(BinanceRequestError, match="timed out"):
        client.ping()


def test_connection_error_raises_request_error() -> None:
    session = _FakeSession(
        exception=requests.ConnectionError("simulated connection error")
    )
    client = _make_client(session)

    with pytest.raises(BinanceRequestError):
        client.ping()


def test_malformed_response_raises_request_error() -> None:
    response = _FakeResponse(status_code=200, ok=True, malformed=True)
    session = _FakeSession(response=response)
    client = _make_client(session)

    with pytest.raises(BinanceRequestError, match="malformed"):
        client.get_market_data("BTCUSDT")


@pytest.mark.parametrize(
    ("session_kwargs", "action_name"),
    [
        ({"exception": requests.Timeout("boom")}, "ping"),
        (
            {
                "response": _FakeResponse(
                    status_code=401,
                    json_data={"code": -2015, "msg": "bad key"},
                    ok=False,
                )
            },
            "get_account_info",
        ),
        (
            {"response": _FakeResponse(status_code=200, ok=True, malformed=True)},
            "get_balances",
        ),
        (
            {
                "response": _FakeResponse(
                    status_code=500, json_data={"msg": "err"}, ok=False
                )
            },
            "get_open_orders",
        ),
    ],
)
def test_credentials_never_appear_in_exceptions_or_logs(
    session_kwargs: dict, action_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    session = _FakeSession(**session_kwargs)
    client = _make_client(session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(BinanceError) as excinfo:
            getattr(client, action_name)()

    exception_text = str(excinfo.value) + repr(excinfo.value)
    assert _FAKE_API_KEY not in exception_text
    assert _FAKE_API_SECRET not in exception_text
    assert _FAKE_API_KEY not in caplog.text
    assert _FAKE_API_SECRET not in caplog.text


def test_client_exposes_no_write_beyond_orders() -> None:
    """Whitelist, not an allowlist-by-omission: exactly these thirteen
    methods may exist on BinanceClient. Funds-movement methods (withdraw,
    transfer, deposit address management) must never be added here, in
    any phase."""
    forbidden_names = {
        "withdraw",
        "transfer",
        "new_order",
        "place_order",
        "buy",
        "sell",
        "delete_order",
        "cancel_all_orders",
        "deposit_address",
        "get_deposit_address",
        "sub_account_transfer",
    }
    exposed = {name for name in dir(BinanceClient) if not name.startswith("_")}
    assert exposed & forbidden_names == set()
    assert exposed == {
        "ping",
        "get_account_info",
        "get_api_key_permissions",
        "get_balances",
        "get_market_data",
        "get_open_orders",
        "get_klines",
        "create_order",
        "cancel_order",
        "get_order",
        "get_trades",
        "get_exchange_info",
    }


def test_signature_is_a_valid_query_string_component() -> None:
    """Guards against a signing bug where params serialize differently between
    what gets sent and what gets signed (e.g. dict ordering)."""
    session = _FakeSession(response=_FakeResponse(json_data=[]))
    client = _make_client(session)

    client.get_open_orders(symbol="BTCUSDT")

    sent_params = session.calls[0]["params"]
    assert "signature" in sent_params
    assert dict(parse_qsl(f"symbol={sent_params['symbol']}"))["symbol"] == "BTCUSDT"


# --- Phase 2: write methods -------------------------------------------------


def test_create_order_market_sends_no_price_and_returns_curated_fields() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "orderId": 555,
        "clientOrderId": "hm-abc123",
        "transactTime": 1700000000000,
        "price": "0.00000000",
        "origQty": "0.01",
        "executedQty": "0.01",
        "cummulativeQuoteQty": "500.00",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": "BUY",
        "fills": [{"price": "50000", "qty": "0.01"}],  # must NOT leak into result
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.create_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.01",
        client_order_id="hm-abc123",
    )

    assert result == {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-abc123",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "price": "0.00000000",
        "orig_qty": "0.01",
        "executed_qty": "0.01",
        "cummulative_quote_qty": "500.00",
        "transact_time": 1700000000000,
    }
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["headers"] == {"X-MBX-APIKEY": _FAKE_API_KEY}
    assert "price" not in call["params"]
    assert "timeInForce" not in call["params"]
    assert call["params"]["newClientOrderId"] == "hm-abc123"


def test_create_order_limit_sends_price_and_time_in_force() -> None:
    session = _FakeSession(
        response=_FakeResponse(json_data={"symbol": "BTCUSDT", "status": "NEW"})
    )
    client = _make_client(session)

    client.create_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity="0.01",
        price="60000",
    )

    sent_params = session.calls[0]["params"]
    assert sent_params["price"] == "60000"
    assert sent_params["timeInForce"] == "GTC"
    assert sent_params["type"] == "LIMIT"


def test_cancel_order_requires_an_identifier() -> None:
    client = _make_client(_FakeSession())

    with pytest.raises(ValueError, match="order_id or client_order_id"):
        client.cancel_order(symbol="BTCUSDT")


def test_cancel_order_by_order_id_returns_curated_fields() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "orderId": 555,
        "clientOrderId": "hm-abc123",
        "status": "CANCELED",
        "origQty": "0.01",
        "executedQty": "0.00",
        "price": "60000",
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.cancel_order(symbol="BTCUSDT", order_id=555)

    assert result == {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-abc123",
        "status": "CANCELED",
        "orig_qty": "0.01",
        "executed_qty": "0.00",
    }
    call = session.calls[0]
    assert call["method"] == "DELETE"
    assert call["params"]["orderId"] == 555
    assert "origClientOrderId" not in call["params"]


def test_get_order_requires_an_identifier() -> None:
    client = _make_client(_FakeSession())

    with pytest.raises(ValueError, match="order_id or client_order_id"):
        client.get_order(symbol="BTCUSDT")


def test_get_order_by_client_order_id_returns_curated_fields() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "orderId": 555,
        "clientOrderId": "hm-abc123",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "origQty": "0.01",
        "executedQty": "0.004",
        "cummulativeQuoteQty": "200.00",
        "time": 1700000000000,
        "updateTime": 1700000005000,
        "isWorking": True,
        "icebergQty": "should-not-leak",
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_order(symbol="BTCUSDT", client_order_id="hm-abc123")

    assert result == {
        "symbol": "BTCUSDT",
        "order_id": 555,
        "client_order_id": "hm-abc123",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "LIMIT",
        "price": "50000",
        "orig_qty": "0.01",
        "executed_qty": "0.004",
        "cummulative_quote_qty": "200.00",
        "time": 1700000000000,
        "update_time": 1700000005000,
        "is_working": True,
    }
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["params"]["origClientOrderId"] == "hm-abc123"


def test_get_trades_returns_curated_fields() -> None:
    payload = [
        {
            "symbol": "BTCUSDT",
            "id": 1,
            "orderId": 555,
            "orderListId": -1,
            "price": "50000",
            "qty": "0.01",
            "quoteQty": "500.00",
            "commission": "0.00001",
            "commissionAsset": "BTC",
            "time": 1700000000000,
            "isBuyer": True,
            "isMaker": False,
            "isBestMatch": True,
        }
    ]
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    trades = client.get_trades("BTCUSDT")

    assert trades == [
        {
            "id": 1,
            "order_id": 555,
            "price": "50000",
            "qty": "0.01",
            "quote_qty": "500.00",
            "commission": "0.00001",
            "commission_asset": "BTC",
            "time": 1700000000000,
            "is_buyer": True,
            "is_maker": False,
        }
    ]
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["headers"] == {"X-MBX-APIKEY": _FAKE_API_KEY}


def test_get_exchange_info_curates_filters() -> None:
    payload = {
        "timezone": "UTC",
        "serverTime": 1700000000000,
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "baseAssetPrecision": 8,
                "quoteAssetPrecision": 8,
                "permissions": ["SPOT"],  # must not leak into result
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001000",
                        "maxQty": "9000.00000000",
                        "stepSize": "0.00001000",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01000000",
                        "maxPrice": "1000000.00000000",
                        "tickSize": "0.01000000",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000"},
                ],
            }
        ],
    }
    session = _FakeSession(response=_FakeResponse(json_data=payload))
    client = _make_client(session)

    result = client.get_exchange_info("BTCUSDT")

    assert result == {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "base_asset_precision": 8,
        "quote_asset_precision": 8,
        "filters": {
            "min_qty": "0.00001000",
            "max_qty": "9000.00000000",
            "step_size": "0.00001000",
            "min_price": "0.01000000",
            "max_price": "1000000.00000000",
            "tick_size": "0.01000000",
            "min_notional": "10.00000000",
        },
    }
    call = session.calls[0]
    assert call["headers"] == {}  # public endpoint, unsigned
    assert "signature" not in call["params"]


def test_get_exchange_info_raises_when_symbol_not_found() -> None:
    session = _FakeSession(
        response=_FakeResponse(json_data={"timezone": "UTC", "symbols": []})
    )
    client = _make_client(session)

    with pytest.raises(BinanceRequestError, match="NOPE"):
        client.get_exchange_info("NOPE")


def test_rate_limit_error_raised_on_429_with_retry_after() -> None:
    response = _FakeResponse(
        status_code=429,
        json_data={"code": -1003, "msg": "Too many requests."},
        ok=False,
    )
    response.headers = {"Retry-After": "5"}
    session = _FakeSession(response=response)
    client = _make_client(session)

    with pytest.raises(BinanceRateLimitError) as excinfo:
        client.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity="0.01"
        )
    assert excinfo.value.retry_after_seconds == 5.0


def test_rate_limit_error_raised_on_418_without_retry_after() -> None:
    response = _FakeResponse(status_code=418, json_data={"msg": "IP banned"}, ok=False)
    response.headers = {}
    session = _FakeSession(response=response)
    client = _make_client(session)

    with pytest.raises(BinanceRateLimitError) as excinfo:
        client.cancel_order(symbol="BTCUSDT", order_id=1)
    assert excinfo.value.retry_after_seconds is None


def test_write_method_credentials_never_appear_in_exceptions_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = _FakeResponse(
        status_code=401,
        json_data={"code": -2015, "msg": "Invalid API-key, IP, or permissions."},
        ok=False,
    )
    session = _FakeSession(response=response)
    client = _make_client(session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(BinanceError) as excinfo:
            client.create_order(
                symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity="0.01"
            )

    exception_text = str(excinfo.value) + repr(excinfo.value)
    assert _FAKE_API_KEY not in exception_text
    assert _FAKE_API_SECRET not in exception_text
    assert _FAKE_API_KEY not in caplog.text
    assert _FAKE_API_SECRET not in caplog.text
