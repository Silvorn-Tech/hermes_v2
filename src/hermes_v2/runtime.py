"""Hermes v2 long-running application runtime."""

from __future__ import annotations
import os
import logging
import signal
import threading

import uvicorn
from fastapi import FastAPI

from hermes_v2.api.app import app as api_app
from hermes_v2.trading.portfolio_snapshot_scheduler import PortfolioSnapshotScheduler

logger = logging.getLogger(__name__)

_DEFAULT_SNAPSHOT_INTERVAL_MINUTES = 15


class HermesRuntime:
    """Long-running runtime for the Hermes application."""

    def __init__(self, application: FastAPI | None = None) -> None:
        self._stop_event = threading.Event()
        self._application = application or api_app
        self._server: uvicorn.Server | None = None
        self._host = os.getenv(
            "HERMES_HOST",
            "0.0.0.0",  # nosec B104 - required for Docker container networking
        )
        self._snapshot_scheduler: PortfolioSnapshotScheduler | None = None

    def start(self) -> None:
        logger.info("Hermes runtime starting")

        self._install_signal_handlers()
        self._initialize()

        config = uvicorn.Config(
            app=self._application,
            host=self._host,
            port=8000,
            log_level="info",
        )
        self._server = uvicorn.Server(config)

        server_thread = threading.Thread(target=self._server.run, daemon=True)
        server_thread.start()

        # Read-only (GET /portfolio-equivalent), so it runs regardless of
        # TRADING_ENABLED — that flag guards order placement, not
        # recording portfolio state. Shares this runtime's own
        # _stop_event so one shutdown signal stops both threads.
        interval_minutes = int(
            os.getenv(
                "HERMES_PORTFOLIO_SNAPSHOT_INTERVAL_MINUTES",
                str(_DEFAULT_SNAPSHOT_INTERVAL_MINUTES),
            )
        )
        self._snapshot_scheduler = PortfolioSnapshotScheduler(
            interval_minutes, stop_event=self._stop_event
        )
        self._snapshot_scheduler.start()

        logger.info("Hermes runtime started")

        self._stop_event.wait()

        logger.info("Hermes runtime stopping")

        if self._server is not None:
            self._server.should_exit = True

        server_thread.join(timeout=5)
        if self._snapshot_scheduler is not None:
            self._snapshot_scheduler.stop()

    def _initialize(self) -> None:
        """Initialize runtime dependencies."""

        # Future:
        # - database
        # - configuration
        # - Telegram integration
        # - market data
        # - strategy engine
        # - risk engine
        pass

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        logger.info("Shutdown signal received: %s", signum)
        self._stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    HermesRuntime().start()


if __name__ == "__main__":
    main()
