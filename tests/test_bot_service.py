"""Tests for BotService — the Bot domain's lifecycle chokepoint. Mirrors
test_order_service.py's shape (hand-written fake BinanceClient, real
Postgres session for idempotency's real SAVEPOINT). See
hermes_v2.trading.bot_service's module docstring for the outcome table
these tests exercise.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from hermes_v2.auth.models import User
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import BinanceRequestError
from hermes_v2.trading.bot_service import (
    BotNotFoundError,
    BotService,
    InvalidBotTransitionError,
)
from hermes_v2.trading.exchange_info_cache import ExchangeInfoCache
from hermes_v2.trading.models import AuditLogEntry, Bot, BotExecutionMode, BotPosition, Order

pytestmark = pytest.mark.database

_GOOD_EXCHANGE_INFO = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "filters": {
        "min_qty": "0.0001",
        "max_qty": "100",
        "step_size": "0.0001",
        "min_price": "0.01",
        "max_price": "1000000",
        "tick_size": "0.01",
        "min_notional": "10",
    },
}


class _FakeBinanceClient:
    def __init__(self) -> None:
        self.market_data: dict[str, dict] = {"BTCUSDT": {"last_price": "50000"}}
        self.exchange_info: dict[str, dict] = {"BTCUSDT": _GOOD_EXCHANGE_INFO}
        self.balances: list[dict] = [{"asset": "USDT", "free": "100000", "locked": "0"}]
        self.trades: dict[str, list[dict]] = {}
        self.create_order_result: dict | Exception = {
            "symbol": "BTCUSDT",
            "order_id": 555,
            "client_order_id": "hm-x",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "price": "0",
            "orig_qty": "0.015",
            "executed_qty": "0.015",
            "cummulative_quote_qty": "750.00",
            "transact_time": 1700000000000,
        }
        self.get_order_result: dict | Exception | None = None
        self.create_order_calls: list[dict] = []
        self.get_order_calls: list[dict] = []

    def get_market_data(self, symbol: str) -> dict:
        return self.market_data[symbol]

    def get_exchange_info(self, symbol: str) -> dict:
        return self.exchange_info[symbol]

    def get_balances(self) -> list[dict]:
        return self.balances

    def get_trades(self, symbol: str) -> list[dict]:
        return self.trades.get(symbol, [])

    def create_order(self, **kwargs) -> dict:
        self.create_order_calls.append(kwargs)
        if isinstance(self.create_order_result, Exception):
            raise self.create_order_result
        return self.create_order_result

    def get_order(self, **kwargs) -> dict:
        self.get_order_calls.append(kwargs)
        if self.get_order_result is None:
            raise BinanceRequestError("no get_order result configured")
        if isinstance(self.get_order_result, Exception):
            raise self.get_order_result
        return self.get_order_result


@pytest.fixture()
def session() -> Session:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine_from_environment()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE bot_positions, bots, audit_log, idempotency_keys, "
                "order_events, orders, role_permissions, user_roles, identities, "
                "sessions, permissions, roles, users CASCADE"
            )
        )

    session_factory = sessionmaker(engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


@pytest.fixture(autouse=True)
def _configure_risk_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("HERMES_RISK_MAX_ORDER_NOTIONAL_USD", "10000")
    monkeypatch.setenv("HERMES_RISK_MAX_SYMBOL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_TOTAL_EXPOSURE_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_DAILY_LOSS_PCT", "100")
    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT")


def _make_user(session: Session) -> User:
    user = User(email="trader@example.com")
    session.add(user)
    session.flush()
    return user


def _make_service(session: Session, client: _FakeBinanceClient) -> BotService:
    return BotService(session, client, exchange_info_cache=ExchangeInfoCache())


def _create_bot(
    service: BotService,
    user_id,
    *,
    key: str = "create-1",
    risk_profile: str = "SENTINEL",
    asset_class: str = "CRYPTO",
    instrument: str = "BTCUSDT",
    target_quantity: Decimal = Decimal("0.015"),
) -> dict:
    return service.create_bot(
        user_id=user_id,
        name="Test Bot",
        risk_profile=risk_profile,
        asset_class=asset_class,
        execution_venue="BINANCE",
        instrument=instrument,
        target_quantity=target_quantity,
        idempotency_key=key,
    )


def _make_live(session: Session, bot_id: str) -> None:
    """Every bot is created SIMULATION now (see BotExecutionMode) --
    this file specifically exercises OrderService's real-Binance-calling
    Pause/Resume integration (mirroring test_order_service.py's shape,
    per this file's own module docstring), which is still real, working
    code kept ready for a future LIVE-activation phase even though
    nothing in the current API can reach it. Flips a freshly created
    test bot to LIVE directly, bypassing that (deliberately absent)
    activation path, so these tests keep covering it."""
    bot = session.get(Bot, bot_id)
    bot.execution_mode = BotExecutionMode.LIVE
    session.flush()


# --- creation -------------------------------------------------------------------


def test_create_crypto_bot(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    result = _create_bot(service, user.id, asset_class="CRYPTO", instrument="BTCUSDT")
    session.commit()

    assert result["status"] == "PAUSED"
    assert result["bot"]["asset_class"] == "CRYPTO"
    assert Decimal(result["bot"]["current_quantity"]) == Decimal("0")
    assert Decimal(result["bot"]["target_quantity"]) == Decimal("0.015")

    position = session.scalars(select(BotPosition)).one()
    assert position.current_quantity == Decimal("0")
    assert position.target_quantity == Decimal("0.015")


def test_create_stock_bot(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    result = _create_bot(
        service,
        user.id,
        risk_profile="EQUILIBRIUM",
        asset_class="EQUITY",
        instrument="AAPL",
        target_quantity=Decimal("10"),
    )
    session.commit()

    assert result["bot"]["asset_class"] == "EQUITY"
    assert result["bot"]["execution_venue"] == "BINANCE"
    assert result["bot"]["risk_profile"] == "EQUILIBRIUM"


def test_create_bot_rejects_invalid_asset_class(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(ValueError):
        _create_bot(service, user.id, asset_class="COMMODITIES")

    # No stuck idempotency reservation left behind by the rejected attempt.
    assert session.scalars(select(BotPosition)).all() == []


def test_create_bot_rejects_invalid_risk_profile(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(ValueError):
        _create_bot(service, user.id, risk_profile="AGGRESSIVE")


def test_create_bot_starts_paused_with_zero_exposure_never_auto_trades(
    session: Session,
) -> None:
    """No math models/autonomous trading exist yet — creation must never
    place an order."""
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)

    result = _create_bot(service, user.id)
    session.commit()

    assert result["bot"]["status"] == "PAUSED"
    assert client.create_order_calls == []


# --- lifecycle transitions -------------------------------------------------------


def _activate(service: BotService, session: Session, user_id, bot_id: str) -> None:
    """Test helper: get a freshly-created bot into ACTIVE via a real resume."""
    result = service.resume(user_id, bot_id, "activate-1")
    session.commit()
    assert result["status"] == "ACTIVE", result


def test_active_to_paused_via_pause(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])

    result = service.pause(user.id, bot["id"], "pause-1")
    session.commit()

    assert result["status"] == "PAUSED"
    assert Decimal(result["bot"]["current_quantity"]) == Decimal("0")
    assert Decimal(result["bot"]["target_quantity"]) == Decimal("0.015")


def test_paused_to_active_via_resume(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())
    bot = _create_bot(service, user.id)["bot"]

    result = service.resume(user.id, bot["id"], "resume-1")
    session.commit()

    assert result["status"] == "ACTIVE"
    assert Decimal(result["bot"]["current_quantity"]) == Decimal("0.015")


def test_active_to_stopped_does_not_close_position(session: Session) -> None:
    """STOP != PAUSE: any open position stays open."""
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])
    calls_before_stop = len(client.create_order_calls)

    result = service.stop(user.id, bot["id"], "stop-1")
    session.commit()

    assert result["status"] == "STOPPED"
    assert Decimal(result["bot"]["current_quantity"]) == Decimal("0.015")  # untouched
    assert len(client.create_order_calls) == calls_before_stop  # no new order


def test_paused_to_stopped(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())
    bot = _create_bot(service, user.id)["bot"]

    result = service.stop(user.id, bot["id"], "stop-1")
    session.commit()

    assert result["status"] == "STOPPED"


def test_pause_on_a_paused_bot_is_an_invalid_transition(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())
    bot = _create_bot(service, user.id)["bot"]  # starts PAUSED

    with pytest.raises(InvalidBotTransitionError):
        service.pause(user.id, bot["id"], "pause-1")
    session.commit()


def test_resume_on_an_active_bot_is_an_invalid_transition(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])

    with pytest.raises(InvalidBotTransitionError):
        service.resume(user.id, bot["id"], "resume-2")
    session.commit()


def test_stop_on_a_stopped_bot_is_an_invalid_transition(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())
    bot = _create_bot(service, user.id)["bot"]
    service.stop(user.id, bot["id"], "stop-1")
    session.commit()

    with pytest.raises(InvalidBotTransitionError):
        service.stop(user.id, bot["id"], "stop-2")


def test_error_only_exits_via_stop(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])
    _activate(service, session, user.id, bot["id"])

    # Force an ambiguous failure on pause: create_order's HTTP call fails,
    # and the ensuing reconciliation read also fails. OrderService returns
    # a status="FAILED" dict for this (it doesn't raise) -> ERROR, per the
    # module docstring's outcome table.
    client.create_order_result = BinanceRequestError("boom")
    result = service.pause(user.id, bot["id"], "pause-err")
    session.commit()
    assert result["status"] == "ERROR"

    current = service.get_bot(user.id, bot["id"])
    assert current["status"] == "ERROR"

    with pytest.raises(InvalidBotTransitionError):
        service.pause(user.id, bot["id"], "pause-err-2")
    with pytest.raises(InvalidBotTransitionError):
        service.resume(user.id, bot["id"], "resume-err")

    result = service.stop(user.id, bot["id"], "stop-from-error")
    session.commit()
    assert result["status"] == "STOPPED"


def test_bot_not_found_raises(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    service = _make_service(session, _FakeBinanceClient())

    with pytest.raises(BotNotFoundError):
        service.pause(user.id, "00000000-0000-0000-0000-000000000000", "key-1")


# --- pause specifics ---------------------------------------------------------------


def test_pause_closes_position_via_explicit_quantity_never_close_position(
    session: Session,
) -> None:
    """The core safety property: pause must sell the bot's own tracked
    quantity via create_order, never the account-wide close_position."""
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id, target_quantity=Decimal("0.015"))["bot"]
    _make_live(session, bot["id"])
    _activate(service, session, user.id, bot["id"])
    client.create_order_calls.clear()

    service.pause(user.id, bot["id"], "pause-1")
    session.commit()

    assert len(client.create_order_calls) == 1
    call = client.create_order_calls[0]
    assert call["side"] == "SELL"
    # The bot's own tracked amount, not "everything held" -- compared as a
    # Decimal since Postgres NUMERIC(28,10) pads the string on re-read.
    assert Decimal(call["quantity"]) == Decimal("0.015")


def test_pause_kill_switch_off_stays_active_not_error(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """TradingDisabledError is raised, not returned as REJECTED -- this
    regression-tests that it's special-cased instead of falling into the
    generic 'any exception -> ERROR' branch."""
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])
    client.create_order_calls.clear()  # drop the resume's own BUY call

    monkeypatch.setenv("TRADING_ENABLED", "false")
    result = service.pause(user.id, bot["id"], "pause-killswitch")
    session.commit()

    assert result["status"] == "REJECTED"
    assert result["bot"]["status"] == "ACTIVE"
    assert client.create_order_calls == []


def test_pause_blocked_by_risk_engine_stays_active(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])

    # Symbol no longer allowed -- RiskEngine rejects before Binance is touched.
    monkeypatch.setenv("HERMES_RISK_ALLOWED_SYMBOLS", "ETHUSDT")
    client.create_order_calls.clear()

    result = service.pause(user.id, bot["id"], "pause-risk")
    session.commit()

    assert result["status"] == "REJECTED"
    assert result["bot"]["status"] == "ACTIVE"
    assert client.create_order_calls == []


def test_pause_failure_leaves_bot_in_a_safe_state_never_paused(
    session: Session,
) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])
    _activate(service, session, user.id, bot["id"])

    client.create_order_result = BinanceRequestError("network blip")
    result = service.pause(user.id, bot["id"], "pause-fail")
    session.commit()
    assert result["status"] == "ERROR"

    current = service.get_bot(user.id, bot["id"])
    assert current["status"] != "PAUSED"
    assert current["status"] == "ERROR"


def test_duplicate_pause_request_is_idempotent(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])
    _activate(service, session, user.id, bot["id"])
    client.create_order_calls.clear()

    first = service.pause(user.id, bot["id"], "pause-dup")
    session.commit()
    second = service.pause(user.id, bot["id"], "pause-dup")
    session.commit()

    assert first == second
    assert len(client.create_order_calls) == 1  # not sold twice


# --- resume specifics --------------------------------------------------------------


def test_resume_restores_saved_target_quantity(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id, target_quantity=Decimal("0.02"))["bot"]
    _make_live(session, bot["id"])

    service.resume(user.id, bot["id"], "resume-1")
    session.commit()

    call = client.create_order_calls[-1]
    assert call["side"] == "BUY"
    assert Decimal(call["quantity"]) == Decimal("0.02")


def test_resume_uses_current_market_price_never_a_stored_historical_price(
    session: Session,
) -> None:
    """Resume's request never includes a price at all -- create_order is
    called with price=None (MARKET), so it always executes at whatever
    Binance quotes right now."""
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])

    service.resume(user.id, bot["id"], "resume-1")
    session.commit()

    call = client.create_order_calls[-1]
    assert call["price"] is None
    assert call["order_type"] == "MARKET"


def test_resume_goes_through_risk_validation(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]

    monkeypatch.setenv("HERMES_RISK_MAX_OPEN_POSITIONS", "0")
    result = service.resume(user.id, bot["id"], "resume-risk")
    session.commit()

    assert result["status"] == "REJECTED"
    assert result["bot"]["status"] == "PAUSED"


def test_resume_insufficient_balance_is_rejected_not_a_crash(
    session: Session,
) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    client.balances = [{"asset": "USDT", "free": "0", "locked": "0"}]
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])

    result = service.resume(user.id, bot["id"], "resume-1")
    session.commit()

    assert result["status"] == "REJECTED"
    assert result["bot"]["status"] == "PAUSED"


def test_duplicate_resume_request_is_idempotent(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])

    first = service.resume(user.id, bot["id"], "resume-dup")
    session.commit()
    second = service.resume(user.id, bot["id"], "resume-dup")
    session.commit()

    assert first == second
    assert len(client.create_order_calls) == 1  # not bought twice


def test_resume_failure_leaves_bot_in_a_safe_state_never_active(
    session: Session,
) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])

    client.create_order_result = BinanceRequestError("network blip")
    result = service.resume(user.id, bot["id"], "resume-fail")
    session.commit()
    assert result["status"] == "ERROR"

    current = service.get_bot(user.id, bot["id"])
    assert current["status"] != "ACTIVE"
    assert current["status"] == "ERROR"


# --- order/bot linkage + audit ------------------------------------------------------


def test_pause_links_the_closing_order_to_the_bot(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _make_live(session, bot["id"])
    _activate(service, session, user.id, bot["id"])

    result = service.pause(user.id, bot["id"], "pause-1")
    session.commit()

    order = session.scalars(select(Order).where(Order.side == "SELL")).one()
    assert str(order.bot_id) == bot["id"]
    position = session.scalars(select(BotPosition)).one()
    assert position.last_close_order_id == order.id
    assert result["bot"]["status"] == "PAUSED"


def test_pause_and_resume_write_audit_entries(session: Session) -> None:
    user = _make_user(session)
    session.commit()
    client = _FakeBinanceClient()
    service = _make_service(session, client)
    bot = _create_bot(service, user.id)["bot"]
    _activate(service, session, user.id, bot["id"])
    service.pause(user.id, bot["id"], "pause-1")
    session.commit()

    actions = {row.action for row in session.scalars(select(AuditLogEntry)).all()}
    assert "bot.create" in actions
    assert "bot.resume" in actions
    assert "bot.pause" in actions
