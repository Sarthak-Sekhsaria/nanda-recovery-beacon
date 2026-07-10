
.DEFAULT_GOAL := help
SHELL := /bin/bash

PY  := backend/.venv/Scripts/python.exe
ifeq (,$(wildcard backend/.venv/Scripts/python.exe))
PY  := backend/.venv/bin/python
endif

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Setup --------------------------------------------------------------------
.PHONY: install
install: ## Create the backend venv and install dependencies
	python -m venv backend/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r backend/requirements-dev.txt

.PHONY: install-frontend
install-frontend: ## Install dashboard dependencies
	cd frontend && npm install

# --- Database -----------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply all database migrations
	cd backend && ../$(PY) -m alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add thing"
	cd backend && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd backend && ../$(PY) -m alembic downgrade -1

.PHONY: seed
seed: ## Insert realistic sample workflows
	cd backend && ../$(PY) -m app.cli seed --reset

.PHONY: create-key
create-key: ## Mint an API key: make create-key agent=planner-1
	cd backend && ../$(PY) -m app.cli create-key --agent-id "$(agent)"

.PHONY: create-admin-key
create-admin-key: ## Mint an admin API key: make create-admin-key agent=admin
	cd backend && ../$(PY) -m app.cli create-key --agent-id "$(agent)" --admin

# --- Run ----------------------------------------------------------------------
.PHONY: dev
dev: ## Run the API with autoreload on :8000
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --port 8000

.PHONY: start
start: ## Run the API the way production does
	cd backend && ../$(PY) -m alembic upgrade head && ../$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8000}

.PHONY: worker
worker: ## Run the standalone failure-detection worker
	cd backend && ../$(PY) -m app.reaper --loop

.PHONY: reap
reap: ## Run one failure-detection sweep and print the result
	cd backend && ../$(PY) -m app.reaper

.PHONY: dashboard
dashboard: ## Run the dashboard on :3000
	cd frontend && npm run dev

.PHONY: up
up: ## Start the whole stack with docker compose
	docker compose up --build

.PHONY: down
down: ## Stop the docker compose stack
	docker compose down -v

# --- Quality ------------------------------------------------------------------
.PHONY: test
test: ## Run the backend test suite (needs TEST_DATABASE_URL)
	cd backend && ../$(PY) -m pytest -q

.PHONY: test-concurrency
test-concurrency: ## Run only the concurrency and race tests
	cd backend && ../$(PY) -m pytest -q -m concurrency

.PHONY: coverage
coverage: ## Run tests with a coverage report
	cd backend && ../$(PY) -m pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Lint the backend
	cd backend && ../$(PY) -m ruff check app seed.py tests

.PHONY: format
format: ## Auto-fix lint issues
	cd backend && ../$(PY) -m ruff check --fix app seed.py tests

.PHONY: typecheck
typecheck: ## Type-check the backend
	cd backend && ../$(PY) -m mypy app

.PHONY: check
check: lint typecheck test ## Everything CI runs

.PHONY: verify-deployment
verify-deployment: ## Smoke-test a live deployment: make verify-deployment url=https://...
	@bash scripts/verify_deployment.sh "$(url)"
