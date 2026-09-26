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
# The Prometheus of docker-compose.yml, for promtool (make alerts-check).
PROMETHEUS_IMAGE := $(shell sed -n 's/^ *image: \(prom\/prometheus:.*\)$$/\1/p' docker-compose.yml)
DEMO_INPUT     ?= Hi! One item in {order} arrived broken. Can I get a refund of $$40?
# The version Demo Co runs in production (1.3.x are release candidates).
DEMO_VERSION   ?= 1.2.4
DEMO_CONTAINED_INPUT ?= Hi! One item in {order} arrived broken. Can I get a refund of $$150?

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
seed: env ## Load the demo workspace: agent, tool twin, scenarios, simulations, an evaluation run, gated releases, verified traffic
	$(COMPOSE) run --rm seed

.PHONY: demo
demo: env ## Run one refund conversation through the demo agent and print its trace link
	$(COMPOSE) exec -T demo-agent support-refund-agent run '$(DEMO_INPUT)' --version $(DEMO_VERSION) --new-order 140 --agent-url http://127.0.0.1:8090

.PHONY: demo-contained
demo-contained: env ## Run an over-limit refund through the runtime gateway; it waits for a person to approve it in the UI
	$(COMPOSE) exec -T demo-agent support-refund-agent run '$(DEMO_CONTAINED_INPUT)' --version $(DEMO_VERSION) --new-order 180 --contained --agent-url http://127.0.0.1:8090

.PHONY: logs
logs: ## Tail service logs
	$(COMPOSE) logs -f --tail=100

.PHONY: doctor
doctor: env ## Verify Docker, ports, env, DB, RabbitMQ, OTel, migrations, services
	./scripts/doctor.sh

.PHONY: db-upgrade
db-upgrade: env ## Bring a data volume from an older PostgreSQL image up to date (collation, pgvector)
	./scripts/postgres-upgrade.sh

.PHONY: alerts-check
alerts-check: ## Validate the Prometheus config and alert rules, and run their promtool tests
	docker run --rm --entrypoint promtool -v "$(CURDIR)/infra/prometheus:/p:ro" -w /p $(PROMETHEUS_IMAGE) check config prometheus.yml
	docker run --rm --entrypoint promtool -v "$(CURDIR)/infra/prometheus:/p:ro" -w /p $(PROMETHEUS_IMAGE) test rules alerts.test.yml

.PHONY: dlq
dlq: env ## Dead-letter queues: depth, and per message its event and why it was parked
	$(LOAD_ENV); $(UV) run python scripts/dlq.py list

.PHONY: dlq-replay
dlq-replay: env ## Send QUEUE's dead letters back to it (LIMIT=n, DRY_RUN=1); fix the cause first
	@test -n "$(QUEUE)" || { echo "usage: make dlq-replay QUEUE=<queue> [LIMIT=n] [DRY_RUN=1]; 'make dlq' lists them" >&2; exit 2; }
	$(LOAD_ENV); $(UV) run python scripts/dlq.py replay $(QUEUE) --limit $(or $(LIMIT),0) $(if $(DRY_RUN),--dry-run)

.PHONY: dlq-drop
dlq-drop: env ## Archive QUEUE's dead letters to dist/dlq/ (JSON Lines), then remove them (LIMIT=n)
	@test -n "$(QUEUE)" || { echo "usage: make dlq-drop QUEUE=<queue> [LIMIT=n]; 'make dlq' lists them" >&2; exit 2; }
	$(LOAD_ENV); $(UV) run python scripts/dlq.py drop $(QUEUE) --limit $(or $(LIMIT),0)

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

.PHONY: test-security
test-security: ## Security tests of spec §63, item by item (real PostgreSQL + RabbitMQ)
	./scripts/with-test-infra.sh $(UV) run python scripts/spec_tests.py security

.PHONY: test-security-live
test-security-live: env ## test-security plus the browser checks against the running stack (make dev first)
	$(LOAD_ENV); ./scripts/with-test-infra.sh $(UV) run python scripts/spec_tests.py security --live

# ---------------------------------------------------------------- supply chain
# docs/security/supply-chain.md. Reports in dist/scan, SBOMs in dist/sbom.
.PHONY: secret-scan
secret-scan: ## Committed secrets anywhere in the git history (gitleaks)
	$(UV) run python scripts/supply_chain.py secrets

.PHONY: vuln-scan
vuln-scan: ## Known vulnerabilities in the Go, Python and JS dependencies (govulncheck, pip-audit, pnpm audit)
	$(UV) run python scripts/supply_chain.py deps

.PHONY: image-scan
image-scan: env ## Every image of the stack against the image policy (trivy; make build first)
	$(LOAD_ENV); $(UV) run python scripts/supply_chain.py images

.PHONY: sbom
sbom: env ## CycloneDX SBOMs of our images and of the source tree (make build first)
	$(LOAD_ENV); $(UV) run python scripts/supply_chain.py sbom

.PHONY: supply-chain
supply-chain: env ## All four supply-chain checks (make build first)
	$(LOAD_ENV); $(UV) run python scripts/supply_chain.py all

.PHONY: test-sdk-ts-live
test-sdk-ts-live: env ## TS SDK end to end: a run through the collector, read back from the API, outcome reported (make dev first)
	$(LOAD_ENV); AGENTTWIN_LIVE=1 \
	  AGENTTWIN_API_URL=http://127.0.0.1:$${CONTROL_PLANE_HOST_PORT:-8080} \
	  AGENTTWIN_OTLP_ENDPOINT=http://127.0.0.1:$${OTEL_HTTP_HOST_PORT:-4318} \
	  AGENTTWIN_API_KEY=$$AGENTTWIN_DEMO_API_KEY \
	  $(PNPM) --filter @agenttwin/sdk exec vitest run test/live.test.ts

.PHONY: test-chaos
test-chaos: ## Chaos tests of spec §64, item by item (outages injected in front of real PostgreSQL + RabbitMQ)
	./scripts/with-test-infra.sh $(UV) run python scripts/spec_tests.py chaos

.PHONY: chaos-drill
chaos-drill: env ## Break the running stack (RabbitMQ, PostgreSQL, the simulation worker) and check it fails safe and recovers (make dev first)
	$(LOAD_ENV); $(UV) run python scripts/chaos_drill.py

.PHONY: load-test
load-test: env ## Load test of spec §65 (k6) against the running stack, with end-to-end checks (make dev first)
	$(LOAD_ENV); $(UV) run python scripts/load_test.py $(LOAD_ARGS)

.PHONY: fuzz
fuzz: ## Run Go fuzz targets for 20s each
	./scripts/fuzz.sh 20s

.PHONY: e2e
e2e: env ## Playwright end-to-end tests against the running stack (make dev first)
	$(LOAD_ENV); cd apps/web && $(PNPM) exec playwright test

.PHONY: golden-path
golden-path: env ## The spec's golden path (§137) as one end-to-end story against the running stack
	$(LOAD_ENV); cd apps/web && $(PNPM) exec playwright test e2e/golden-path.spec.ts

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

.PHONY: cli
cli: ## Build the agenttwin CLI into bin/agenttwin
	$(GO) build -trimpath -ldflags "-X github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo.Commit=$$(git rev-parse --short HEAD 2>/dev/null)" \
		-o bin/agenttwin ./packages/cli/cmd/agenttwin
