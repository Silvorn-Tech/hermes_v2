.PHONY: install test lint format check docker-build docker-run run migrate bootstrap-admin binance-check

install:
	pip install -e ".[dev]"

run: bootstrap-admin
	set -a; . ./.env.dev; set +a; python -m hermes_v2.runtime

cli:
	python -m hermes_v2.cli

test:
	pytest

lint:
	ruff check .

format:
	ruff format .

check:
	set -a; . ./.env.dev; set +a; pytest
	set -a; . ./.env.dev; set +a; ruff check .
	set -a; . ./.env.dev; set +a; ruff format --check .
	set -a; . ./.env.dev; set +a; bandit -r src/

docker-build:
	docker build -t hermes-v2:local .

docker-run:
	docker run --rm \
		--name hermes-runtime-test \
		--env-file ./.env.dev \
		--network host \
		hermes-v2:local

docker-clean:
	docker rm -f hermes-runtime-test 2>/dev/null || true

migrate:
	set -a; . ./.env.dev; set +a; alembic upgrade head

bootstrap-admin:
	set -a; . ./.env.dev; set +a; python -m hermes_v2.cli bootstrap-admin

binance-check:
	set -a; . ./.env.dev; set +a; python -m hermes_v2.cli binance-check

security:
	bandit -r src/