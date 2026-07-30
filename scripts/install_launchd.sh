#!/usr/bin/env bash
# Render the launchd templates for THIS checkout and load them.
# Usage: scripts/install_launchd.sh [--dry-run]
set -euo pipefail

JOBAGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
DRY="${1:-}"

for tpl in "$JOBAGENT_DIR"/scripts/templates/*.plist.template; do
  name="$(basename "$tpl" .template)"
  out="$DEST/$name"
  rendered="$(sed "s|__JOBAGENT_DIR__|$JOBAGENT_DIR|g" "$tpl")"
  if [ "$DRY" = "--dry-run" ]; then
    echo "would write $out"
    continue
  fi
  mkdir -p "$DEST"
  printf '%s\n' "$rendered" > "$out"
  launchctl unload "$out" 2>/dev/null || true
  launchctl load "$out"
  echo "installed $name"
done
if [ "$DRY" = "--dry-run" ]; then
  echo "(dry run: nothing written)"
fi
