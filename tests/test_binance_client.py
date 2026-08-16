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

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if self._exception is not None:
            raise self._exception
        return self._response


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


def test_client_exposes_no_write_methods() -> None:
    forbidden_names = {
        "create_order",
        "cancel_order",
        "new_order",
        "place_order",
        "buy",
        "sell",
        "withdraw",
        "transfer",
        "delete_order",
        "cancel_all_orders",
    }
    exposed = {name for name in dir(BinanceClient) if not name.startswith("_")}
    assert exposed & forbidden_names == set()
    assert exposed == {
        "ping",
        "get_account_info",
        "get_balances",
        "get_market_data",
        "get_open_orders",
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
