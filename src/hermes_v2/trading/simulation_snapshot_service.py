"""Per-bot Simulation snapshots — the one metric (drawdown) that
genuinely needs a time series rather than a live computation from
current state (portfolio value, return %, P&L, trade count, and win rate
are all directly computable from `SimulationAccount` + `SimulationOrder`
history alone; see `simulation_portfolio_service.py`).

Reuses `portfolio_snapshot_service.bucket_timestamp` — the exact same
epoch-aligned, restart-safe bucketing the real account-wide snapshot
already uses — and the same `INSERT ... ON CONFLICT DO NOTHING ...
RETURNING` idempotency pattern (see that module's own docstring for why
`RETURNING` is used instead of the driver's `rowcount`, which was found
to be unreliable for `ON CONFLICT DO NOTHING` on psycopg3).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from hermes_v2.integrations.binance import BinanceClient, BinanceError
from hermes_v2.trading.models import Bot, SimulationAccount, SimulationSnapshot
from hermes_v2.trading.portfolio_snapshot_service import bucket_timestamp
from hermes_v2.trading.simulation_portfolio_service import (
    compute_position_value,
    compute_total_value,
)

logger = logging.getLogger(__name__)


def take_simulation_snapshot(
    session: Session,
    client: BinanceClient,
    bot: Bot,
    *,
    interval_minutes: int,
    now: datetime | None = None,
) -> SimulationSnapshot | None:
    """One snapshot row for `bot`'s simulation account, or `None` if this
    interval's bucket already has one, the bot has no simulation account
    (a LIVE bot), or the current market price couldn't be fetched — never
    a fabricated value at a stale/zero price."""
    account = session.scalar(
        select(SimulationAccount).where(SimulationAccount.bot_id == bot.id)
    )
    if account is None:
        return None

    position = bot.position
    current_quantity = Decimal(position.current_quantity) if position else Decimal("0")

    market_price = Decimal("0")
    if current_quantity > 0:
        try:
            market_data = client.get_market_data(position.instrument)
            market_price = Decimal(str(market_data["last_price"]))
        except BinanceError:
            logger.warning(
                "Simulation snapshot skipped for bot %s: market price unavailable.",
                bot.id,
            )
            return None

    cash_balance = Decimal(account.cash_balance_quote)
    position_value = compute_position_value(current_quantity, market_price)
    total_value = compute_total_value(cash_balance, current_quantity, market_price)
    exposure_pct = (
        (position_value / total_value * 100) if total_value > 0 else Decimal("0")
    )

    snapshot_at = bucket_timestamp(now or datetime.now(UTC), interval_minutes)

    stmt = (
        pg_insert(SimulationSnapshot.__table__)
        .values(
            id=uuid.uuid4(),
            bot_id=bot.id,
            snapshot_at=snapshot_at,
            cash_balance_quote=cash_balance,
            position_value_quote=position_value,
            total_value_quote=total_value,
            exposure_pct=exposure_pct,
        )
        .on_conflict_do_nothing(index_elements=["bot_id", "snapshot_at"])
        .returning(SimulationSnapshot.__table__.c.id)
    )
    inserted_id = session.execute(stmt).scalar()
    session.flush()

    if inserted_id is None:
        return None
    return session.scalar(
        select(SimulationSnapshot).where(
            SimulationSnapshot.bot_id == bot.id,
            SimulationSnapshot.snapshot_at == snapshot_at,
        )
    )


__all__ = ["take_simulation_snapshot"]
