#!/usr/bin/env bash
# Runs a command with real PostgreSQL + RabbitMQ available for integration
# tests (ADR-0012). Order of preference:
#   1. AGENTTWIN_TEST_DATABASE_URL / AGENTTWIN_TEST_AMQP_URL already exported
#   2. docker compose -f docker-compose.test.yml
#   3. native binaries via scripts/test-infra-local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${AGENTTWIN_TEST_DATABASE_URL:-}" ] || [ -z "${AGENTTWIN_TEST_AMQP_URL:-}" ]; then
  if docker info >/dev/null 2>&1; then
    docker compose -f docker-compose.test.yml up -d --wait >/dev/null
    export AGENTTWIN_TEST_DATABASE_URL="postgres://agenttwin:agenttwin-test@127.0.0.1:55433/agenttwin?sslmode=disable"
    export AGENTTWIN_TEST_AMQP_URL="amqp://agenttwin:agenttwin-test@127.0.0.1:5673/"
  else
    eval "$(./scripts/test-infra-local.sh start)"
  fi
fi
export AGENTTWIN_REQUIRE_INTEGRATION=1
exec "$@"
