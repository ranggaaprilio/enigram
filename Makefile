# Tasks for both apps (apps/api, apps/web); run from the repository root. `make` lists them.
.DEFAULT_GOAL := help
.PHONY: help install lint format test test-all api worker web-dev web-build db up down

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

install: ## Python workspace, web dependencies and git hooks
	uv sync
	npm --prefix apps/web install
	uv run pre-commit install

lint: ## ruff and the web type check
	uv run ruff check . && uv run ruff format --check .
	npm --prefix apps/web run typecheck

format: ## ruff fixes and formatting
	uv run ruff check --fix . && uv run ruff format .

test: ## fast tests: Python unit tests and web tests
	uv run pytest -m "not live and not docling"
	npm --prefix apps/web test

test-all: ## every Python test (Docling; live APIs and Postgres when configured)
	uv run pytest

api: ## HTTP API on 127.0.0.1:8080 (start Postgres first: make db)
	uv run enigram api

worker: ## index worker (one per workspace)
	uv run enigram worker

web-dev: ## UI on http://localhost:5173, proxying /api to the API
	npm --prefix apps/web run dev

web-build: ## type-check and build the UI into apps/web/dist
	npm --prefix apps/web run build

db: ## only Postgres, for the CLI and API on the host
	docker compose up -d postgres

up: ## Postgres, API, worker and web UI in Docker
	docker compose up -d --build

down: ## stop the Docker services
	docker compose down
