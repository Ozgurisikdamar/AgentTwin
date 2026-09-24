#!/usr/bin/env bash
# Starts PostgreSQL (with pgvector) and RabbitMQ from native binaries for
# integration tests when Docker is not available (ADR-0012).
# Usage: scripts/test-infra-local.sh start|stop|env
set -euo pipefail

DATA_DIR="${AGENTTWIN_TEST_INFRA_DIR:-/tmp/agenttwin-test-infra}"
PG_PORT="${AGENTTWIN_TEST_PG_PORT:-55432}"
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
PG_USER=agenttwin
PG_PASS=agenttwin-test

start_pg() {
  mkdir -p "$DATA_DIR"
  chown postgres:postgres "$DATA_DIR" 2>/dev/null || true
  if [ ! -f "$DATA_DIR/pg/PG_VERSION" ]; then
    su postgres -c "$PG_BIN/initdb -D $DATA_DIR/pg -U postgres --auth=trust >/dev/null"
  fi
  if ! su postgres -c "$PG_BIN/pg_ctl -D $DATA_DIR/pg status" >/dev/null 2>&1; then
    su postgres -c "$PG_BIN/pg_ctl -D $DATA_DIR/pg -o '-p $PG_PORT -k /tmp -c max_connections=300' -l $DATA_DIR/pg.log start -w" >/dev/null
  fi
  psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1 || \
    psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -c "CREATE ROLE $PG_USER LOGIN SUPERUSER PASSWORD '$PG_PASS'" >/dev/null
  psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='agenttwin'" | grep -q 1 || \
    psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -c "CREATE DATABASE agenttwin OWNER $PG_USER" >/dev/null
}

start_mq() {
  if ! rabbitmqctl status >/dev/null 2>&1; then
    rabbitmq-server -detached >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do rabbitmqctl status >/dev/null 2>&1 && break; sleep 1; done
  fi
  rabbitmqctl list_users 2>/dev/null | grep -q '^agenttwin' || {
    rabbitmqctl add_user agenttwin agenttwin-test >/dev/null
    rabbitmqctl set_permissions -p / agenttwin '.*' '.*' '.*' >/dev/null
  }
}

case "${1:-start}" in
  start) start_pg; start_mq; "$0" env ;;
  stop)
    su postgres -c "$PG_BIN/pg_ctl -D $DATA_DIR/pg stop -m fast" >/dev/null 2>&1 || true
    rabbitmqctl stop >/dev/null 2>&1 || true ;;
  env)
    echo "export AGENTTWIN_TEST_DATABASE_URL=postgres://$PG_USER:$PG_PASS@127.0.0.1:$PG_PORT/agenttwin?sslmode=disable"
    echo "export AGENTTWIN_TEST_AMQP_URL=amqp://agenttwin:agenttwin-test@127.0.0.1:5672/" ;;
  *) echo "usage: $0 start|stop|env" >&2; exit 2 ;;
esac
