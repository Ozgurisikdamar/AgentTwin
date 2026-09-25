#!/usr/bin/env bash
# `make doctor`: checks what the local stack needs (Docker, env, ports,
# PostgreSQL, RabbitMQ, the OpenTelemetry collector, migrations, services)
# and prints ok / warn / FAIL with a hint for each item. Exit code 1 when any
# check failed. Safe to run while the stack is down: it then reports what is
# missing instead of starting anything.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="${COMPOSE:-docker compose}"
fails=0
warns=0
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() {
  printf '  \033[33mwarn\033[0m  %s\n' "$*"
  warns=$((warns + 1))
}
fail() {
  printf '  \033[31mFAIL\033[0m  %s\n' "$*"
  fails=$((fails + 1))
}
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }
# Local probes must not go through an outbound HTTP proxy.
http() { curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 "$@" 2>/dev/null || true; }

# ------------------------------------------------------------------ tools
section "Tools"
if ! command -v docker >/dev/null 2>&1; then
  fail "docker is not installed (https://docs.docker.com/get-docker/)"
  printf '\n%d failed\n' "$fails"
  exit 1
fi
if docker info >/dev/null 2>&1; then
  ok "docker engine $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
else
  fail "docker engine is not reachable (is the daemon running? does your user have access to the socket?)"
fi
compose_version=$($COMPOSE version --short 2>/dev/null || true)
if [ -z "$compose_version" ]; then
  fail "docker compose v2 is not available"
else
  major=${compose_version%%.*}
  minor=${compose_version#*.}
  minor=${minor%%.*}
  if [ "${major#v}" -gt 2 ] || { [ "${major#v}" -eq 2 ] && [ "$minor" -ge 20 ]; }; then
    ok "docker compose $compose_version"
  else
    fail "docker compose $compose_version is too old (2.20+ required)"
  fi
fi
command -v curl >/dev/null 2>&1 && ok "curl" || fail "curl is required by the checks below"

# ------------------------------------------------------------------ env
section "Environment (.env)"
if [ ! -f .env ]; then
  fail ".env is missing - run 'make env' (copies .env.example)"
else
  ok ".env present"
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  required=(POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB RABBITMQ_USER RABBITMQ_PASSWORD
    AGENTTWIN_INTERNAL_TOKEN_SECRET AGENTTWIN_SESSION_SECRET AGENTTWIN_API_KEY_PEPPER
    AGENTTWIN_DEMO_API_KEY DEMO_AGENT_TOKEN DEMO_TOOLS_ADMIN_TOKEN GRAFANA_ADMIN_PASSWORD)
  missing=()
  placeholders=()
  for v in "${required[@]}"; do
    val="${!v:-}"
    if [ -z "$val" ]; then missing+=("$v"); elif [[ "$val" == *change-me* ]]; then placeholders+=("$v"); fi
  done
  if [ ${#missing[@]} -gt 0 ]; then fail "unset variables: ${missing[*]}"; else ok "required variables set"; fi
  for v in AGENTTWIN_INTERNAL_TOKEN_SECRET AGENTTWIN_SESSION_SECRET AGENTTWIN_API_KEY_PEPPER; do
    val="${!v:-}"
    if [ -n "$val" ] && [ ${#val} -lt 32 ]; then fail "$v must be at least 32 characters"; fi
  done
  if [ "${APP_ENV:-development}" = "production" ]; then
    [ ${#placeholders[@]} -gt 0 ] && fail "placeholder secrets with APP_ENV=production: ${placeholders[*]}"
    [ "${AUTH_MODE:-dev}" = "dev" ] && fail "AUTH_MODE=dev is not allowed with APP_ENV=production (use oidc)"
  elif [ ${#placeholders[@]} -gt 0 ]; then
    warn "${#placeholders[@]} development placeholder secrets (fine locally; services refuse them in production)"
  fi
  if [ "${AUTH_MODE:-dev}" = "oidc" ] && { [ -z "${OIDC_ISSUER_URL:-}" ] || [ -z "${OIDC_CLIENT_ID:-}" ]; }; then
    fail "AUTH_MODE=oidc needs OIDC_ISSUER_URL and OIDC_CLIENT_ID"
  fi
fi

# ------------------------------------------------------------------ ports
section "Ports"
running_services=$($COMPOSE ps --status running --services 2>/dev/null || true)
is_running() { grep -qx "$1" <<<"$running_services"; }
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }
check_port() { # service container_port env_var default
  local svc=$1 cport=$2 var=$3 def=$4 port
  port="${!var:-$def}"
  if is_running "$svc"; then
    local bound
    bound=$($COMPOSE port "$svc" "$cport" 2>/dev/null | tail -1)
    if [ "${bound##*:}" = "$port" ]; then ok "$port $svc"; else fail "$svc publishes ${bound:-nothing}, expected $port ($var)"; fi
  elif port_in_use "$port"; then
    fail "$port ($var) is used by another process - stop it or set $var in .env"
  else
    ok "$port free for $svc"
  fi
}
check_port web 3000 WEB_HOST_PORT 3000
check_port control-plane 8080 CONTROL_PLANE_HOST_PORT 8080
check_port postgres 5432 POSTGRES_HOST_PORT 55432
check_port rabbitmq 5672 RABBITMQ_HOST_PORT 5672
check_port rabbitmq 15672 RABBITMQ_UI_HOST_PORT 15672
check_port otel-collector 4317 OTEL_GRPC_HOST_PORT 4317
check_port otel-collector 4318 OTEL_HTTP_HOST_PORT 4318
check_port prometheus 9090 PROMETHEUS_HOST_PORT 9090
check_port grafana 3000 GRAFANA_HOST_PORT 3001
check_port demo-agent 8090 DEMO_AGENT_HOST_PORT 8090
check_port demo-tools 8091 DEMO_TOOLS_HOST_PORT 8091

if [ -z "$running_services" ]; then
  printf '\nThe stack is not running - start it with "make dev" and run "make doctor" again.\n'
  printf '\n%d failed, %d warnings\n' "$fails" "$warns"
  [ "$fails" -eq 0 ]
  exit
fi

# ------------------------------------------------------------------ PostgreSQL
section "PostgreSQL"
if is_running postgres && $COMPOSE exec -T postgres pg_isready -q -U "${POSTGRES_USER:-agenttwin}" -d "${POSTGRES_DB:-agenttwin}"; then
  ok "accepting connections"
  vec=$($COMPOSE exec -T postgres psql -U "${POSTGRES_USER:-agenttwin}" -d "${POSTGRES_DB:-agenttwin}" -tAc \
    "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'" 2>/dev/null | tr -d '[:space:]')
  [ -n "$vec" ] && ok "pgvector $vec available" || fail "pgvector extension is not available (use the pgvector/pgvector image)"
  schemas=$($COMPOSE exec -T postgres psql -U "${POSTGRES_USER:-agenttwin}" -d "${POSTGRES_DB:-agenttwin}" -tAc \
    "SELECT string_agg(nspname, ',' ORDER BY nspname) FROM pg_namespace WHERE nspname IN ('control','evaluation','simulation','trace')" 2>/dev/null | tr -d '[:space:]')
  [ "$schemas" = "control,evaluation,simulation,trace" ] && ok "service schemas: control, evaluation, simulation, trace" ||
    fail "service schemas missing (found: ${schemas:-none})"
else
  fail "postgres is not ready ($COMPOSE logs postgres)"
fi

# ------------------------------------------------------------------ RabbitMQ
section "RabbitMQ"
if is_running rabbitmq && $COMPOSE exec -T rabbitmq rabbitmq-diagnostics -q check_running >/dev/null 2>&1; then
  ok "broker running"
  exchanges=$($COMPOSE exec -T rabbitmq rabbitmqctl -q list_exchanges name 2>/dev/null)
  grep -qx "agenttwin.events" <<<"$exchanges" && ok "exchange agenttwin.events declared" ||
    fail "exchange agenttwin.events missing (services declare it on start)"
  queues=$($COMPOSE exec -T rabbitmq rabbitmqctl -q list_queues name messages consumers 2>/dev/null)
  dlq_depth=$(awk '$1 ~ /\.dlq$/ {s += $2} END {print s + 0}' <<<"$queues")
  [ "$dlq_depth" -eq 0 ] && ok "dead-letter queues empty" || warn "$dlq_depth messages in dead-letter queues (inspect in the RabbitMQ UI)"
  # Durable queues keep events for consumers that are not running (yet); they
  # are processed when the service starts. Report them instead of hiding them.
  idle=$(awk '$1 !~ /\.(retry|dlq)$/ && $2 > 0 && $3 == 0 {printf "%s%s=%s", sep, $1, $2; sep=", "}' <<<"$queues")
  [ -z "$idle" ] && ok "no events waiting without a consumer" || warn "events waiting for a consumer that is not running: $idle"
else
  fail "rabbitmq is not running ($COMPOSE logs rabbitmq)"
fi

# ------------------------------------------------------------------ OTel
section "OpenTelemetry collector"
code=$(http -X POST -H 'Content-Type: application/json' --data '{}' "http://127.0.0.1:${OTEL_HTTP_HOST_PORT:-4318}/v1/traces")
[ "$code" = "200" ] && ok "OTLP/HTTP receiver accepts traces" || fail "OTLP/HTTP receiver returned '$code' (expected 200)"
export_errors=$($COMPOSE logs --no-color --since 15m otel-collector 2>/dev/null | grep -ciE 'exporting failed|dropping data' || true)
[ "${export_errors:-0}" -eq 0 ] && ok "no export failures in the last 15 minutes" ||
  warn "$export_errors export failures in the last 15 minutes ($COMPOSE logs otel-collector)"

# ------------------------------------------------------------------ migrations
section "Migrations"
# service -> its binary inside the image (Go services: /app/service)
for entry in control-plane:/app/service trace-service:/app/service simulation-service:simulation-service \
  evaluation-service:evaluation-service; do
  svc=${entry%%:*}
  bin=${entry#*:}
  if ! is_running "$svc"; then
    fail "$svc is not running"
    continue
  fi
  out=$($COMPOSE exec -T "$svc" "$bin" migrate status 2>&1 | grep '"migration status"' | tail -1)
  if grep -Eq '"pending": ?0[,}]' <<<"$out"; then
    ok "$svc: all $(sed -E 's/.*"known": ?([0-9]+).*/\1/' <<<"$out") migrations applied"
  else
    fail "$svc: pending migrations or status failed: ${out:-no output}"
  fi
done

# ------------------------------------------------------------------ services
section "Services"
while read -r svc st hl; do
  [ -n "${svc:-}" ] || continue
  if [ "$st" != "running" ]; then fail "$svc is $st"; elif [ -n "${hl:-}" ] && [ "$hl" != "healthy" ]; then fail "$svc is $hl"; fi
done < <($COMPOSE ps -a --format '{{.Service}} {{.State}} {{.Health}}')
probe() { # name url expected
  local code
  code=$(http "$2")
  [ "$code" = "$3" ] && ok "$1 ($2)" || fail "$1 returned '${code:-no response}' ($2)"
}
probe "control plane ready" "http://127.0.0.1:${CONTROL_PLANE_HOST_PORT:-8080}/health/ready" 200
if $COMPOSE exec -T trace-service /app/service healthcheck >/dev/null 2>&1; then ok "trace service ready"; else fail "trace service is not ready"; fi
for entry in simulation-service:simulation-service simulation-worker:simulation-service \
  evaluation-service:evaluation-service evaluation-worker:evaluation-service; do
  svc=${entry%%:*}
  bin=${entry#*:}
  if $COMPOSE exec -T "$svc" "$bin" healthcheck >/dev/null 2>&1; then ok "$svc ready"; else fail "$svc is not ready ($COMPOSE logs $svc)"; fi
done
probe "web" "http://127.0.0.1:${WEB_HOST_PORT:-3000}/healthz" 200
probe "demo agent" "http://127.0.0.1:${DEMO_AGENT_HOST_PORT:-8090}/healthz" 200
probe "demo tools" "http://127.0.0.1:${DEMO_TOOLS_HOST_PORT:-8091}/healthz" 200
probe "prometheus" "http://127.0.0.1:${PROMETHEUS_HOST_PORT:-9090}/-/ready" 200
probe "grafana" "http://127.0.0.1:${GRAFANA_HOST_PORT:-3001}/api/health" 200

# ------------------------------------------------------------------ demo data
section "Demo workspace"
if [ -n "${AGENTTWIN_DEMO_API_KEY:-}" ]; then
  traces=$(curl --noproxy '*' -s --max-time 5 -H "X-AgentTwin-Api-Key: $AGENTTWIN_DEMO_API_KEY" \
    "http://127.0.0.1:${CONTROL_PLANE_HOST_PORT:-8080}/api/v1/traces?limit=1" 2>/dev/null)
  if grep -q '"trace_id"' <<<"$traces"; then ok "demo project has traces"; else warn "no demo traces yet - run 'make seed'"; fi
  scenarios=$(curl --noproxy '*' -s --max-time 5 -H "X-AgentTwin-Api-Key: $AGENTTWIN_DEMO_API_KEY" \
    "http://127.0.0.1:${CONTROL_PLANE_HOST_PORT:-8080}/api/v1/scenarios?limit=200" 2>/dev/null)
  count=$(grep -o '"id": \?"' <<<"$scenarios" | wc -l | tr -d ' ')
  if [ "${count:-0}" -gt 0 ]; then ok "demo project has $count scenarios"; else warn "no demo scenarios yet - run 'make seed'"; fi
  runs=$(curl --noproxy '*' -s --max-time 5 -H "X-AgentTwin-Api-Key: $AGENTTWIN_DEMO_API_KEY" \
    "http://127.0.0.1:${CONTROL_PLANE_HOST_PORT:-8080}/api/v1/eval-runs?status=COMPLETED&limit=1" 2>/dev/null)
  if grep -q '"status": \?"COMPLETED"' <<<"$runs"; then ok "demo project has a completed evaluation run"; else warn "no completed evaluation run yet - run 'make seed'"; fi
fi

printf '\n%d failed, %d warnings\n' "$fails" "$warns"
[ "$fails" -eq 0 ]
