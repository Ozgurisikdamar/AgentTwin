# AgentTwin developer entrypoints. Every CI stage maps to a target here so the
# whole pipeline runs locally without GitHub.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE        ?= docker compose
GO             ?= go
UV             ?= uv
PNPM           ?= pnpm
GO_PACKAGES    := ./packages/... ./services/...
# Loads .env into a recipe's shell (for targets that talk to the running stack).
LOAD_ENV       := set -a; . ./.env; set +a
DEMO_INPUT     ?= Hi! One item in {order} arrived broken. Can I get a refund of $$40?
# The version Demo Co runs in production (1.3.x are release candidates).
DEMO_VERSION   ?= 1.2.4

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
	@$(LOAD_ENV); echo ""; \
	echo "AgentTwin is running:"; \
	echo "  Web UI         http://localhost:$${WEB_HOST_PORT:-3000}   (demo login: owner@demo.agenttwin.dev)"; \
	echo "  Control plane  http://localhost:$${CONTROL_PLANE_HOST_PORT:-8080}/api/v1"; \
	echo "  Grafana        http://localhost:$${GRAFANA_HOST_PORT:-3001}"; \
	echo "  RabbitMQ UI    http://localhost:$${RABBITMQ_UI_HOST_PORT:-15672}"; \
	echo "  One more run:  make demo"

.PHONY: down
down: ## Stop the local stack (keeps data)
	$(COMPOSE) down

.PHONY: reset
reset: ## Destroy local data and start fresh
	$(COMPOSE) down -v --remove-orphans
	$(MAKE) dev

.PHONY: seed
seed: env ## Load the demo workspace: agent, tool twin, scenarios, simulations, an evaluation run, verified traffic
	$(COMPOSE) run --rm seed

.PHONY: demo
demo: env ## Run one refund conversation through the demo agent and print its trace link
	$(COMPOSE) exec -T demo-agent support-refund-agent run '$(DEMO_INPUT)' --version $(DEMO_VERSION) --new-order 140 --agent-url http://127.0.0.1:8090

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
	$(UV) run ruff format .
	$(PNPM) -r --if-present run format

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
	$(PNPM) -r --if-present run format:check
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
e2e: env ## Playwright end-to-end tests against the running stack (make dev first)
	$(LOAD_ENV); cd apps/web && $(PNPM) exec playwright test

.PHONY: contracts-check
contracts-check: ## Detect breaking changes in event schemas and API documents; check generated API types
	$(UV) run python scripts/contracts_check.py
	$(PNPM) --filter @agenttwin/web run check:api

.PHONY: gen-api
gen-api: ## Regenerate the web's API types from the OpenAPI documents
	$(PNPM) --filter @agenttwin/web run gen:api

.PHONY: build
build: ## Build all Go binaries and container images
	$(GO) build ./...
	$(COMPOSE) build
