"""Tests for the in-process exchangeInfo TTL cache."""

from __future__ import annotations

from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_exchange_info(self, symbol: str) -> dict:
        self.calls.append(symbol)
        return {"symbol": symbol, "status": "TRADING"}


def test_second_call_within_ttl_does_not_hit_the_client() -> None:
    cache = ExchangeInfoCache(ttl_seconds=3600)
    client = _FakeClient()

    first = cache.get(client, "BTCUSDT")
    second = cache.get(client, "BTCUSDT")

    assert first == second
    assert client.calls == ["BTCUSDT"]


def test_expired_entry_is_refetched() -> None:
    cache = ExchangeInfoCache(ttl_seconds=-1)  # already expired the instant it's set
    client = _FakeClient()

    cache.get(client, "BTCUSDT")
    cache.get(client, "BTCUSDT")

    assert client.calls == ["BTCUSDT", "BTCUSDT"]


def test_different_symbols_are_cached_independently() -> None:
    cache = ExchangeInfoCache(ttl_seconds=3600)
    client = _FakeClient()

    cache.get(client, "BTCUSDT")
    cache.get(client, "ETHUSDT")

    assert client.calls == ["BTCUSDT", "ETHUSDT"]


def test_symbol_lookup_is_case_insensitive() -> None:
    cache = ExchangeInfoCache(ttl_seconds=3600)
    client = _FakeClient()

    cache.get(client, "btcusdt")
    cache.get(client, "BTCUSDT")

    assert client.calls == ["BTCUSDT"]


def test_reset_clears_cached_entries() -> None:
    cache = ExchangeInfoCache(ttl_seconds=3600)
    client = _FakeClient()

    cache.get(client, "BTCUSDT")
    cache.reset()
    cache.get(client, "BTCUSDT")

    assert client.calls == ["BTCUSDT", "BTCUSDT"]
