#!/usr/bin/env bash
# Waits until every service of the local stack is up: healthy when it has a
# health check, running otherwise. On timeout (or when a container exits) it
# prints the offending services with their recent logs and exits 1.
#
#   scripts/wait-healthy.sh            # default timeout 300s
#   WAIT_TIMEOUT=600 scripts/wait-healthy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="${COMPOSE:-docker compose}"
TIMEOUT="${WAIT_TIMEOUT:-300}"
deadline=$(($(date +%s) + TIMEOUT))

# Services the current configuration starts (profile-only services such as
# `seed` are excluded unless their profile is active).
mapfile -t expected < <($COMPOSE config --services)

status_of() {
  # "<service> <state> <health>" for every container of the project.
  $COMPOSE ps -a --format '{{.Service}} {{.State}} {{.Health}}'
}

report() {
  echo "" >&2
  $COMPOSE ps -a >&2 || true
  for svc in "$@"; do
    echo "" >&2
    echo "--- last log lines of $svc ---" >&2
    $COMPOSE logs --no-color --tail=30 "$svc" >&2 || true
  done
}

printf 'waiting for %d services' "${#expected[@]}"
while :; do
  declare -A state=() health=()
  while read -r svc st hl; do
    [ -n "${svc:-}" ] || continue
    state[$svc]=$st
    health[$svc]=${hl:-}
  done < <(status_of)

  waiting=() dead=()
  for svc in "${expected[@]}"; do
    st=${state[$svc]:-missing}
    hl=${health[$svc]:-}
    case "$st" in
      running)
        if [ -n "$hl" ] && [ "$hl" != "healthy" ]; then waiting+=("$svc($hl)"); fi ;;
      exited | dead) dead+=("$svc") ;;
      *) waiting+=("$svc($st)") ;;
    esac
  done

  if [ ${#dead[@]} -gt 0 ]; then
    echo "" >&2
    echo "error: container exited: ${dead[*]}" >&2
    report "${dead[@]}"
    exit 1
  fi
  if [ ${#waiting[@]} -eq 0 ]; then
    echo " - all healthy"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "" >&2
    echo "error: not healthy after ${TIMEOUT}s: ${waiting[*]}" >&2
    names=()
    for w in "${waiting[@]}"; do names+=("${w%%(*}"); done
    report "${names[@]}"
    exit 1
  fi
  printf '.'
  sleep 3
  unset state health
done
