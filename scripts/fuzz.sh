#!/usr/bin/env bash
# Runs every Go fuzz target for the given duration (default 10s).
set -euo pipefail
cd "$(dirname "$0")/.."
DUR="${1:-10s}"
grep -rl --include='*_test.go' '^func Fuzz' packages services | while read -r f; do
  dir=$(dirname "$f")
  for fn in $(grep -oE '^func (Fuzz[A-Za-z0-9_]+)' "$f" | awk '{print $2}'); do
    echo "== $dir $fn ($DUR)"
    go test "./$dir" -run '^$' -fuzz "^${fn}\$" -fuzztime "$DUR"
  done
done
