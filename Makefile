# AgentTwin developer entrypoints. Every CI stage maps to a target here so the
# whole pipeline runs locally without GitHub.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE        ?= docker compose
GO             ?= go
UV             ?= uv
PNPM           ?= pnpm
GO_PACKAGES    := ./packages/... ./services/control-plane/... ./services/trace-service/... ./services/graph-service/... ./services/runtime-gateway/...
PY_PROJECTS    := packages/core-py packages/sdk-python services/evaluation-service services/simulation-service demo/support-refund-agent

.PHONY: help
help: ## Show targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n",$$1,$$2}'

# ---------------------------------------------------------------- local stack
.PHONY: env
env:
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

.PHONY: dev
dev: env ## Build and start the complete local stack, seed the demo workspace
	$(COMPOSE) up --build -d
	./scripts/wait-healthy.sh
	$(MAKE) seed
	@echo ""
	@echo "AgentTwin is running:"
	@echo "  Web UI         http://localhost:$${WEB_HOST_PORT:-3000}   (demo login: owner@demo.agenttwin.dev)"
	@echo "  Control plane  http://localhost:$${CONTROL_PLANE_HOST_PORT:-8080}/api/v1"
	@echo "  Grafana        http://localhost:$${GRAFANA_HOST_PORT:-3001}"
	@echo "  RabbitMQ UI    http://localhost:$${RABBITMQ_UI_HOST_PORT:-15672}"

.PHONY: down
down: ## Stop the local stack (keeps data)
	$(COMPOSE) down

.PHONY: reset
reset: ## Destroy local data and start fresh
	$(COMPOSE) down -v --remove-orphans
	$(MAKE) dev

.PHONY: seed
seed: env ## Load the demo workspace (idempotent)
	$(COMPOSE) run --rm --no-deps seed

.PHONY: logs
logs: ## Tail service logs
	$(COMPOSE) logs -f --tail=100

.PHONY: doctor
doctor: env ## Verify Docker, ports, env, DB, RabbitMQ, OTel, migrations, services
	./scripts/doctor.sh

# ---------------------------------------------------------------- quality
.PHONY: fmt
fmt: ## Format all code
	gofmt -w packages services
	cd $(CURDIR) && $(UV) run ruff format .
	cd apps/web && $(PNPM) exec prettier --write . >/dev/null 2>&1 || true

.PHONY: lint
lint: lint-go lint-py lint-web ## Run all linters and type checkers

.PHONY: lint-go
lint-go:
	@out=$$(gofmt -l packages services); if [ -n "$$out" ]; then echo "gofmt needed:"; echo "$$out"; exit 1; fi
	$(GO) vet $(GO_PACKAGES)
	golangci-lint run $(GO_PACKAGES)

.PHONY: lint-py
lint-py:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

.PHONY: lint-web
lint-web:
	$(PNPM) -r --if-present run lint
	$(PNPM) -r --if-present run typecheck

# ---------------------------------------------------------------- tests
.PHONY: test
test: test-unit test-integration test-web ## Run unit, integration and frontend tests

.PHONY: test-unit
test-unit: ## Unit, property and fuzz-seed tests (no infrastructure)
	env -u AGENTTWIN_TEST_DATABASE_URL -u AGENTTWIN_TEST_AMQP_URL $(GO) test -count=1 $(GO_PACKAGES)
	env -u AGENTTWIN_TEST_DATABASE_URL -u AGENTTWIN_TEST_AMQP_URL $(UV) run pytest -q -m "not integration"

.PHONY: test-integration
test-integration: ## Integration tests against real PostgreSQL + RabbitMQ
	./scripts/with-test-infra.sh bash -c '$(GO) test -count=1 -p 4 $(GO_PACKAGES) && $(UV) run pytest -q'

.PHONY: test-web
test-web: ## Frontend + TS SDK unit tests
	$(PNPM) -r --if-present run test

.PHONY: fuzz
fuzz: ## Run Go fuzz targets for 20s each
	./scripts/fuzz.sh 20s

.PHONY: e2e
e2e: ## Playwright end-to-end tests against the running stack (make dev first)
	cd apps/web && $(PNPM) exec playwright test

.PHONY: load
load: ## k6 load tests against the running stack
	./scripts/load-test.sh

.PHONY: contracts-check
contracts-check: ## Detect breaking changes in event schemas
	$(UV) run python scripts/contracts_check.py

.PHONY: build
build: ## Build all Go binaries and container images
	$(GO) build ./...
	$(COMPOSE) build

.PHONY: cli
cli: ## Build the agenttwin CLI into ./bin
	$(GO) build -o bin/agenttwin ./packages/cli/cmd/agenttwin
