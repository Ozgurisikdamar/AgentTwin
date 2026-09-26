#!/usr/bin/env bash
# `make db-upgrade`: brings a PostgreSQL data volume created by an older
# AgentTwin image up to date with the current one. Safe to run any time: it
# only acts where something is out of date and says what it did.
#
#   * Collation: a database created on a different glibc (the image moved
#     from Debian 12 to 13, glibc 2.36 -> 2.41) is reindexed, because text
#     indexes built with the old sort order can miss rows, and its recorded
#     collation version is then refreshed. PostgreSQL warns about this on
#     every connection until it is done.
#   * pgvector: the extension objects are updated to the version the image
#     ships (the library is already new; the SQL definitions are not).
#
# REINDEX takes locks on each table while it runs: on a large database, run
# it in a maintenance window (docs/runbooks/postgres-upgrade.md).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="${COMPOSE:-docker compose}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
user="${POSTGRES_USER:-agenttwin}"

# The collation warning is printed on every connection until it is fixed;
# it is what this script fixes, so it is dropped. Any other message shows.
psql_in() {
  local db=$1
  shift
  $COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -X -q -U "$user" -d "$db" "$@" \
    2> >(grep -vE 'collation version mismatch|created using collation version|REFRESH COLLATION VERSION, or build' >&2)
}

databases=$(psql_in postgres -tAc \
  "SELECT datname FROM pg_database WHERE datallowconn ORDER BY datname")
changed=0
for db in $databases; do
  stale=$(psql_in "$db" -tAc \
    "SELECT datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
       FROM pg_database WHERE datname = current_database()")
  if [ "$stale" = "t" ]; then
    echo "$db: collation version changed - reindexing, then refreshing the recorded version"
    psql_in "$db" -c "REINDEX DATABASE \"$db\""
    psql_in "$db" -c "ALTER DATABASE \"$db\" REFRESH COLLATION VERSION"
    changed=$((changed + 1))
  fi
  vector=$(psql_in "$db" -tAc \
    "SELECT installed_version || ' ' || default_version FROM pg_available_extensions
      WHERE name = 'vector' AND installed_version IS DISTINCT FROM default_version
        AND installed_version IS NOT NULL")
  if [ -n "$vector" ]; then
    echo "$db: pgvector ${vector% *} -> ${vector#* }"
    psql_in "$db" -c "ALTER EXTENSION vector UPDATE"
    changed=$((changed + 1))
  fi
done
if [ "$changed" -eq 0 ]; then
  echo "PostgreSQL data is up to date with the image."
fi
