"""Hermes v2 command-line interface."""

import argparse

from sqlalchemy.orm import Session

from hermes_v2.auth.bootstrap import (
    BootstrapConfigurationError,
    BootstrapStateError,
    bootstrap_super_admin,
)
from hermes_v2.auth.seed import seed_authorization_data
from hermes_v2.database.connection import create_engine_from_environment


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


def main() -> int:
    """Run the Hermes v2 command-line interface."""
    parser = argparse.ArgumentParser(prog="hermes")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser(
        "bootstrap-admin", help="Bootstrap the configured Super Admin"
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
