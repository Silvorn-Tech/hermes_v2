"""Hermes v2 command-line interface."""

import argparse
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from hermes_v2.auth.bootstrap import (
    BootstrapConfigurationError,
    BootstrapStateError,
    bootstrap_super_admin,
)
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.database.connection import create_engine_from_environment
from hermes_v2.integrations.binance import (
    BinanceClient,
    BinanceConfigurationError,
    BinanceError,
)

_BINANCE_DIAGNOSTIC_SYMBOL = "BTCUSDT"


def bootstrap_admin() -> None:
    """Bootstrap the configured protected Super Admin."""
    engine = create_engine_from_environment()
    try:
        with Session(engine) as session:
            with session.begin():
                seed_authorization_data(session)
                bootstrap_super_admin(session)
    finally:
        engine.dispose()

    print("Super Admin bootstrap completed.")


def binance_check() -> int:
    """Run a read-only diagnostic against Binance and report per-check status.

    Never prints an API key, secret, header, token, or account identifier —
    only a fixed OK/FAIL label per check, plus this module's own non-secret
    error messages on failure. Calls only BinanceClient's five read-only
    methods; nothing here can create, cancel, or modify anything on Binance.

    Exits 0 only if every check passes, including the final permissions
    check: an API key with withdrawals enabled fails this diagnostic even if
    every other call succeeds, since that key is unsafe to use regardless of
    what Hermes itself calls.
    """
    failures: list[str] = []

    try:
        client = BinanceClient()
    except BinanceConfigurationError as error:
        print(f"Binance connection: FAIL ({error})")
        for label in (
            "Account",
            "Balances",
            "Market data",
            "Open orders",
            "Permissions",
        ):
            print(f"{label}: SKIPPED")
        return 1

    def _run(label: str, action: Callable[[], Any]) -> Any:
        try:
            result = action()
            print(f"{label}: OK")
            return result
        except BinanceError as error:
            print(f"{label}: FAIL ({error})")
            failures.append(label)
            return None

    _run("Binance connection", client.ping)
    _run("Account", client.get_account_info)
    _run("Balances", client.get_balances)
    _run("Market data", lambda: client.get_market_data(_BINANCE_DIAGNOSTIC_SYMBOL))
    _run("Open orders", client.get_open_orders)

    # Deliberately not `account.get("can_withdraw")` from the "Account"
    # check above -- that field reflects the account's overall withdrawal
    # eligibility (KYC/verification status), not this specific key's
    # "Enable Withdrawals" checkbox, so it's `true` for any verified
    # account regardless of which key was used. get_api_key_permissions()
    # reads Binance's dedicated key-permissions endpoint instead.
    try:
        permissions = client.get_api_key_permissions()
    except BinanceError as error:
        print(f"Permissions: FAIL ({error})")
        failures.append("Permissions")
    else:
        if permissions.get("can_withdraw") is False:
            print("Permissions: READ_ONLY")
        else:
            print("Permissions: UNSAFE (withdrawals are enabled on this API key)")
            failures.append("Permissions")

    return 1 if failures else 0


def main() -> int:
    """Run the Hermes v2 command-line interface."""
    parser = argparse.ArgumentParser(prog="hermes")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser(
        "bootstrap-admin", help="Bootstrap the configured Super Admin"
    )
    subcommands.add_parser(
        "binance-check",
        help="Read-only diagnostic: verify Binance connectivity and credentials",
    )
    arguments = parser.parse_args()

    if arguments.command == "bootstrap-admin":
        try:
            bootstrap_admin()
        except (
            BootstrapConfigurationError,
            BootstrapStateError,
            RuntimeError,
        ) as error:
            print(f"Bootstrap failed: {error}")
            return 1
        return 0

    if arguments.command == "binance-check":
        return binance_check()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
