.PHONY: install test lint format check docker-build docker-run run bootstrap-admin

install:
	pip install -e ".[dev]"

run:
	python -m hermes_v2.runtime

cli:
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
	docker run --rm \
		--name hermes-runtime-test \
		-p 8000:8000 \
		hermes-v2:local
		
docker-clean:
	docker rm -f hermes-runtime-test 2>/dev/null || true

bootstrap-admin:
	set -a; . ./.env.dev; set +a; python -m hermes_v2.cli bootstrap-admin