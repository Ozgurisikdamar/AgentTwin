# ADR-0012 — Integration tests against real PostgreSQL/RabbitMQ via provided DSNs

* Status: accepted · Date: 2026-09-24

## Context
The specification prefers Testcontainers "where practical". `testcontainers-go` and
`testcontainers-python` add a large dependency tree and need a Docker socket inside the
test process, which is not available in every developer/CI sandbox.

## Decision
Integration tests use real PostgreSQL (with pgvector) and RabbitMQ provided through
`AGENTTWIN_TEST_DATABASE_URL` / `AGENTTWIN_TEST_AMQP_URL`. `make test-integration`
starts them with `docker compose -f docker-compose.test.yml up -d` when Docker is
available; `scripts/test-infra-local.sh` starts native binaries otherwise. Tests create an
isolated database per test package and drop it afterwards. Integration tests **fail**
(not skip) when `AGENTTWIN_REQUIRE_INTEGRATION=1` and the infrastructure is missing, so
CI cannot silently skip them.

## Consequences
Same real-dependency coverage as Testcontainers, fewer dependencies, works without a
Docker socket.
