#!/bin/sh
set -eu

alembic upgrade head
python -m hermes_v2.cli bootstrap-admin

exec python -m hermes_v2.runtime
