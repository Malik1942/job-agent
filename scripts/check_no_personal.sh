#!/usr/bin/env bash
# Leak-check gate: no personal tokens in tracked/exported files.
# Usage: scripts/check_no_personal.sh [DIR]
#   No DIR: scan the repo's TRACKED files (personal docs excluded — they are
#   dropped at export). With DIR: scan every file under DIR.
#
# Patterns come from scripts/.denylist (untracked/local — it contains the
# personal tokens themselves, so it must never ship) plus generic patterns
# below. Without a denylist file only the generic patterns run, so the check
# still protects a public clone.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DENYLIST="$ROOT/scripts/.denylist"

PATTERNS=()
if [ -f "$DENYLIST" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && PATTERNS+=("$line")
  done < "$DENYLIST"
fi
# Generic patterns that are personal regardless of the denylist:
PATTERNS+=('[A-Za-z0-9._%+-]+@gmail\.com' '\+1[- ]?[0-9]{3}[- ][0-9]{3}[- ]?[0-9]{4}')

fail=0
scan_files() {
  # $1: newline-separated file list. Runs in the main shell so fail= sticks.
  local files="$1"
  [ -z "$files" ] && return 0
  for pat in "${PATTERNS[@]}"; do
    hits=$(printf '%s\n' "$files" | tr '\n' '\0' \
      | xargs -0 grep -nIiE "$pat" 2>/dev/null \
      | grep -v 'example\.com' | grep -v '555 010' | grep -v '206 555' || true)
    if [ -n "$hits" ]; then
      echo "LEAK ($pat):"
      echo "$hits"
      fail=1
    fi
  done
}

if [ $# -ge 1 ]; then
  cd "$1"
  scan_files "$(find . -type f ! -path './.git/*')"
else
  cd "$ROOT"
  scan_files "$(git ls-files \
    | grep -v '^docs/sponsorship_companies.md$' \
    | grep -v '^docs/superpowers/')"
fi

if [ "$fail" -ne 0 ]; then
  echo "❌ personal data found — fix before exporting/committing"
  exit 1
fi
echo "✅ no personal tokens found"
