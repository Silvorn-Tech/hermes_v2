#!/bin/sh
# Run on ROMEO (or any host holding a real Hermes v2 .env) to verify the
# secrets file is not group/world-readable. Exits non-zero if it is, or if
# the file is missing.
set -eu

ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE does not exist" >&2
    exit 1
fi

MODE=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || stat -f "%OLp" "$ENV_FILE")

case "$MODE" in
    600|400)
        echo "OK: $ENV_FILE is $MODE"
        ;;
    *)
        echo "FAIL: $ENV_FILE is $MODE (expected 600) — run: chmod 600 $ENV_FILE" >&2
        exit 1
        ;;
esac
