.PHONY: install test lint format check docker-build docker-run run bootstrap-admin

install:
	pip install -e ".[dev]"

run:
	python -m hermes_v2.cli

test:
	pytest

lint:
	ruff check .

format:
	ruff format .

check:
	pytest
	ruff check .
	ruff format --check .

docker-build:
	docker build -t hermes-v2:local .

docker-run:
	docker run --rm hermes-v2:local

bootstrap-admin:
	set -a; . ./.env.dev; set +a; python -m hermes_v2.cli bootstrap-admin
