"""Tests for `run_snapshot_tick` — the scheduler's per-tick contract is
that it never raises, regardless of what Binance or the database do.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_v2.trading import portfolio_snapshot_scheduler as scheduler_module
from hermes_v2.trading.portfolio_snapshot_scheduler import run_snapshot_tick


class _FakeSnapshot:
    def __init__(self) -> None:
        self.snapshot_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _FakeSession:
    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *args) -> None:
        return None

    def commit(self) -> None:
        pass


def _session_factory() -> _FakeSession:
    return _FakeSession()


def test_tick_returns_false_when_binance_is_not_configured(monkeypatch) -> None:
    def _raise_not_configured() -> None:
        raise scheduler_module.BinanceConfigurationError("no credentials")

    monkeypatch.setattr(scheduler_module, "BinanceClient", _raise_not_configured)

    result = run_snapshot_tick(_session_factory, interval_minutes=15)

    assert result is False


def test_tick_catches_a_binance_error_and_never_raises(monkeypatch) -> None:
    class _ExplodingClient:
        def __init__(self) -> None:
            raise RuntimeError("Binance is down")

    monkeypatch.setattr(scheduler_module, "BinanceClient", _ExplodingClient)

    result = run_snapshot_tick(_session_factory, interval_minutes=15)

    assert result is False


def test_tick_catches_a_db_error_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module, "BinanceClient", lambda: object())

    def _raise_db_error(*args, **kwargs):
        raise RuntimeError("DB connection lost")

    monkeypatch.setattr(scheduler_module, "take_portfolio_snapshot", _raise_db_error)

    result = run_snapshot_tick(_session_factory, interval_minutes=15)

    assert result is False


def test_tick_returns_true_when_a_snapshot_is_created(monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module, "BinanceClient", lambda: object())
    monkeypatch.setattr(
        scheduler_module, "take_portfolio_snapshot", lambda *a, **k: _FakeSnapshot()
    )

    result = run_snapshot_tick(_session_factory, interval_minutes=15)

    assert result is True


def test_tick_returns_false_when_the_bucket_already_has_a_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module, "BinanceClient", lambda: object())
    monkeypatch.setattr(
        scheduler_module, "take_portfolio_snapshot", lambda *a, **k: None
    )

    result = run_snapshot_tick(_session_factory, interval_minutes=15)

    assert result is False


def test_scheduler_stop_before_start_does_not_raise() -> None:
    from hermes_v2.trading.portfolio_snapshot_scheduler import (
        PortfolioSnapshotScheduler,
    )

    pytest.importorskip("hermes_v2.database.connection")
    # Constructing the scheduler builds an engine (cheap, no connection
    # attempted until used) but never starts the thread; stop() on a
    # never-started scheduler must be a safe no-op, matching how
    # HermesRuntime may stop() during a startup failure.
    import os

    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required")
    scheduler = PortfolioSnapshotScheduler(interval_minutes=15)
    scheduler.stop()  # must not raise
