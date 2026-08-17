"""PortfolioSnapshotScheduler — the first in-process recurring task in
this codebase. Deliberately not Celery/APScheduler/a host-level timer:
`hermes_v2.runtime.HermesRuntime` already runs as a single long-running
process with a daemon thread (uvicorn) coordinated by a
`threading.Event`; this is a second daemon thread of the same shape,
coordinated by the same kind of stop event, needing no new deployment
infrastructure — it starts and survives redeploys exactly because the
main process does.

Runs regardless of `TRADING_ENABLED`: taking a snapshot is a read-only
`GET /portfolio`-equivalent call, unrelated to the kill switch that
guards order placement.

Each tick does two independent things — the real account-wide snapshot
(`run_snapshot_tick`, unchanged) and a per-bot Simulation snapshot pass
(`run_simulation_snapshot_tick`, new) — in the *same* thread/interval
rather than a second scheduler thread, but each wrapped in its own
try/except so a failure in one never affects the other, and one bot's
failure in the Simulation pass never stops the rest of that pass either.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceClient, BinanceConfigurationError
from hermes_v2.trading.models import Bot, BotExecutionMode
from hermes_v2.trading.portfolio_snapshot_service import take_portfolio_snapshot
from hermes_v2.trading.simulation_snapshot_service import take_simulation_snapshot

logger = logging.getLogger(__name__)

_DEFAULT_QUOTE_ASSET = "USDT"


def run_snapshot_tick(
    session_factory: sessionmaker,
    *,
    quote_asset: str = _DEFAULT_QUOTE_ASSET,
    interval_minutes: int,
    now: datetime | None = None,
) -> bool:
    """One snapshot attempt. Returns whether a new row was created (for
    tests/logging) — but its real contract is that it **never raises**:
    a transient Binance/DB failure must not permanently stop the
    scheduler thread, since nothing else would ever restart it."""
    try:
        client = BinanceClient()
        with session_factory() as session:
            snapshot = take_portfolio_snapshot(
                session,
                client,
                quote_asset=quote_asset,
                interval_minutes=interval_minutes,
                now=now,
            )
            session.commit()
    except BinanceConfigurationError:
        logger.warning("Portfolio snapshot skipped: Binance is not configured.")
        return False
    except Exception:
        logger.exception("Portfolio snapshot tick failed; will retry next interval.")
        return False

    if snapshot is None:
        logger.debug("Portfolio snapshot skipped: this interval already has one.")
        return False
    logger.info("Portfolio snapshot recorded at %s.", snapshot.snapshot_at.isoformat())
    return True


def run_simulation_snapshot_tick(
    session_factory: sessionmaker,
    *,
    interval_minutes: int,
    now: datetime | None = None,
) -> int:
    """Snapshots every SIMULATION bot's virtual portfolio in this tick.
    Returns how many snapshot rows were created (for tests/logging).
    Never raises: a Binance/DB failure for one bot is caught and logged
    without stopping the rest of the pass, and a failure to run the pass
    at all never escapes to the caller — the same "one bad tick must
    never permanently stop the scheduler thread" contract as
    `run_snapshot_tick`."""
    try:
        client = BinanceClient()
    except BinanceConfigurationError:
        logger.warning("Simulation snapshots skipped: Binance is not configured.")
        return 0

    taken = 0
    try:
        with session_factory() as session:
            bots = session.scalars(
                select(Bot).where(Bot.execution_mode == BotExecutionMode.SIMULATION)
            ).all()
            for bot in bots:
                try:
                    snapshot = take_simulation_snapshot(
                        session, client, bot, interval_minutes=interval_minutes, now=now
                    )
                    session.commit()
                    if snapshot is not None:
                        taken += 1
                except Exception:
                    logger.exception(
                        "Simulation snapshot failed for bot %s; will retry next "
                        "interval.",
                        bot.id,
                    )
                    session.rollback()
    except Exception:
        logger.exception("Simulation snapshot tick failed to run at all.")

    return taken


class PortfolioSnapshotScheduler:
    def __init__(
        self,
        interval_minutes: int,
        *,
        stop_event: threading.Event | None = None,
        quote_asset: str = _DEFAULT_QUOTE_ASSET,
    ) -> None:
        self._interval_minutes = interval_minutes
        self._quote_asset = quote_asset
        self._stop_event = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        # One long-lived engine for the scheduler's lifetime (not
        # recreated per tick, unlike the deliberately short-lived
        # per-request engines route handlers build) — a fresh Session is
        # still opened and closed per tick, matching the codebase's
        # existing per-unit-of-work convention.
        self._session_factory = sessionmaker(
            bind=create_engine_from_environment(),
            autoflush=False,
            expire_on_commit=False,
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        interval_seconds = self._interval_minutes * 60
        logger.info(
            "Portfolio snapshot scheduler started (every %s minutes).",
            self._interval_minutes,
        )
        while not self._stop_event.is_set():
            run_snapshot_tick(
                self._session_factory,
                quote_asset=self._quote_asset,
                interval_minutes=self._interval_minutes,
            )
            run_simulation_snapshot_tick(
                self._session_factory,
                interval_minutes=self._interval_minutes,
            )
            self._stop_event.wait(interval_seconds)


__all__ = [
    "PortfolioSnapshotScheduler",
    "run_simulation_snapshot_tick",
    "run_snapshot_tick",
]
