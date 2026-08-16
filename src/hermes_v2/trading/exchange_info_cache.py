"""In-process TTL cache for Binance's `exchangeInfo` per symbol.

Same idiom as `OAuthStateStore` (`auth/oauth.py`): thread-safe, in-process,
single-worker-scoped — this deployment runs uvicorn without `--workers`
(see `runtime.py`), so a single process already matches production, exactly
the same constraint `OAuthStateStore` already accepts. Before a
multi-worker/multi-replica deployment, this would need a shared store.

Exchange filters (lot size, tick size, min notional) change rarely — caching
them avoids an extra Binance round trip on every single order validation.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from hermes_v2.integrations.binance import BinanceClient

_DEFAULT_TTL_SECONDS = 3600


class ExchangeInfoCache:
    """Thread-safe, in-process cache of `BinanceClient.get_exchange_info()`."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, client: BinanceClient, symbol: str) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(normalized_symbol)
            if entry is not None and entry[0] > now:
                return entry[1]

        info = client.get_exchange_info(normalized_symbol)
        with self._lock:
            self._entries[normalized_symbol] = (now + self.ttl_seconds, info)
        return info

    def reset(self) -> None:
        """Clear all cached entries. Test-only — production never calls this."""
        with self._lock:
            self._entries.clear()


EXCHANGE_INFO_CACHE = ExchangeInfoCache()

__all__ = ["EXCHANGE_INFO_CACHE", "ExchangeInfoCache"]
