# Aegis - developer and operations tasks.
#
# `make help` lists everything. ENV selects the environment file used by the
# Docker targets and defaults to development.

.DEFAULT_GOAL := help
SHELL := /bin/bash

ENV ?= development
COMPOSE ?= docker compose
PYTHON ?= .venv/bin/python
UV := $(shell command -v uv 2>/dev/null)

# Every Docker target loads .env.$(ENV); fail early and clearly if it is absent.
ENV_FILE := .env.$(ENV)

.PHONY: help
help: ## Show this help
	@echo "Aegis - AI incident response platform"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "ENV defaults to 'development'. Example: make up ENV=staging"

# =====================================================================
# Setup
# =====================================================================

.PHONY: install
install: ## Create a virtualenv and install all dependencies
ifdef UV
	uv venv
	uv pip install -e ".[dev,test]"
else
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev,test]"
endif
	@echo "Done. Activate with: source .venv/bin/activate"

.PHONY: env
env: ## Create .env.$(ENV) from the template if it does not exist
	@if [ -f $(ENV_FILE) ]; then \
		echo "$(ENV_FILE) already exists; leaving it alone."; \
	else \
		cp .env.example $(ENV_FILE); \
		echo "Created $(ENV_FILE). Set OPENAI_API_KEY and JWT_SECRET_KEY before starting."; \
	fi

.PHONY: secret
secret: ## Print a fresh JWT signing secret
	@openssl rand -hex 32

# =====================================================================
# Running locally
# =====================================================================

.PHONY: dev
dev: ## Run the API with auto-reload
	APP_ENV=development $(PYTHON) -m uvicorn aegis.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: serve
serve: ## Run the API without reload, production settings
	APP_ENV=$(ENV) $(PYTHON) -m uvicorn aegis.main:app --host 0.0.0.0 --port 8000 --workers 4

.PHONY: check
check: ## Verify configuration and dependency reachability
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli check

# =====================================================================
# Database
# =====================================================================

.PHONY: migrate
migrate: ## Apply all pending migrations
	APP_ENV=$(ENV) .venv/bin/alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back the most recent migration
	APP_ENV=$(ENV) .venv/bin/alembic downgrade -1

.PHONY: migration
migration: ## Autogenerate a migration: make migration M="add widget table"
	@if [ -z "$(M)" ]; then echo "Usage: make migration M=\"description\""; exit 1; fi
	APP_ENV=$(ENV) .venv/bin/alembic revision --autogenerate -m "$(M)"

.PHONY: migrate-sql
migrate-sql: ## Print the SQL for pending migrations without applying them
	APP_ENV=$(ENV) .venv/bin/alembic upgrade head --sql

# =====================================================================
# Knowledge base
# =====================================================================

.PHONY: ingest
ingest: ## Ingest documents: make ingest PATH_ARG=./data/runbooks
	@if [ -z "$(PATH_ARG)" ]; then \
		echo "Usage: make ingest PATH_ARG=./data/runbooks"; exit 1; \
	fi
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli ingest $(PATH_ARG)

.PHONY: ingest-samples
ingest-samples: ## Ingest the bundled sample runbooks
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli ingest ./data/runbooks

.PHONY: stats
stats: ## Show knowledge base size and coverage
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli stats

.PHONY: search
search: ## Search the knowledge base: make search Q="checkout 503"
	@if [ -z "$(Q)" ]; then echo "Usage: make search Q=\"your query\""; exit 1; fi
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli search "$(Q)"

.PHONY: admin
admin: ## Create an admin user: make admin EMAIL=you@example.com
	@if [ -z "$(EMAIL)" ]; then echo "Usage: make admin EMAIL=you@example.com"; exit 1; fi
	APP_ENV=$(ENV) $(PYTHON) -m aegis.cli create-user $(EMAIL) --admin

# =====================================================================
# Quality
# =====================================================================

.PHONY: test
test: ## Run the test suite
	$(PYTHON) -m pytest

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	$(PYTHON) -m pytest --cov=aegis --cov-report=term-missing --cov-report=html

.PHONY: test-unit
test-unit: ## Run unit tests only (no database required)
	$(PYTHON) -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## Run integration tests (requires PostgreSQL)
	$(PYTHON) -m pytest tests/integration -q

.PHONY: lint
lint: ## Check formatting and lint rules
	.venv/bin/ruff check aegis tests
	.venv/bin/ruff format --check aegis tests

.PHONY: format
format: ## Apply formatting and safe lint fixes
	.venv/bin/ruff check --fix aegis tests
	.venv/bin/ruff format aegis tests

.PHONY: typecheck
typecheck: ## Run static type checking
	.venv/bin/mypy aegis

.PHONY: verify
verify: lint typecheck test ## Run everything CI runs

# =====================================================================
# Evaluation
# =====================================================================

.PHONY: eval
eval: ## Run the evaluation harness interactively
	APP_ENV=$(ENV) $(PYTHON) -m evals.main --interactive

.PHONY: eval-quick
eval-quick: ## Run the evaluation harness with defaults
	APP_ENV=$(ENV) $(PYTHON) -m evals.main --quick

# =====================================================================
# Docker
# =====================================================================

.PHONY: require-env
require-env:
	@if [ ! -f $(ENV_FILE) ]; then \
		echo "Missing $(ENV_FILE). Run: make env ENV=$(ENV)"; exit 1; \
	fi

.PHONY: up
up: require-env ## Start the full stack
	APP_ENV=$(ENV) $(COMPOSE) --env-file $(ENV_FILE) up -d --build

.PHONY: up-min
up-min: require-env ## Start only the database and API
	APP_ENV=$(ENV) $(COMPOSE) --env-file $(ENV_FILE) up -d --build db api

.PHONY: down
down: ## Stop the stack, preserving volumes
	APP_ENV=$(ENV) $(COMPOSE) down

.PHONY: clean-volumes
clean-volumes: ## Stop the stack and DELETE all data
	@read -p "This deletes the database and all indexed documents. Continue? [y/N] " ok; \
	[ "$$ok" = "y" ] && APP_ENV=$(ENV) $(COMPOSE) down -v || echo "Cancelled."

.PHONY: logs
logs: ## Tail application logs
	APP_ENV=$(ENV) $(COMPOSE) logs -f api

.PHONY: logs-all
logs-all: ## Tail logs from every service
	APP_ENV=$(ENV) $(COMPOSE) logs -f

.PHONY: shell
shell: ## Open a shell in the API container
	APP_ENV=$(ENV) $(COMPOSE) exec api /bin/bash

.PHONY: psql
psql: ## Open a psql session against the database
	APP_ENV=$(ENV) $(COMPOSE) exec db psql -U $${POSTGRES_USER:-aegis} -d $${POSTGRES_DB:-aegis}

.PHONY: build
build: ## Build the production image
	docker build --target runtime --build-arg APP_ENV=$(ENV) -t aegis:$(ENV) .

# =====================================================================
# Housekeeping
# =====================================================================

.PHONY: clean
clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	@echo "Cleaned."
