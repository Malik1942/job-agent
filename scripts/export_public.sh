#!/usr/bin/env bash
# Build the publishable repo: tracked files only, personal docs dropped,
# fresh single-commit history. Usage: scripts/export_public.sh DEST_DIR
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?usage: export_public.sh DEST_DIR}"

[ -e "$DEST" ] && { echo "refusing to overwrite existing $DEST"; exit 1; }

cd "$ROOT"
scripts/check_no_personal.sh   # gate 1: tracked files clean of personal tokens

mkdir -p "$DEST"
git ls-files -z | rsync -a --files-from=- --from0 . "$DEST/"

# Personal artifacts never ship:
rm -f  "$DEST/docs/sponsorship_companies.md"
rm -rf "$DEST/docs/superpowers"
rmdir "$DEST/docs" 2>/dev/null || true

"$ROOT/scripts/check_no_personal.sh" "$DEST"   # gate 2: verify the export itself

cd "$DEST"
git init -q
git config core.hooksPath scripts/git-hooks
git add -A
git commit -q -m "jobagent: initial public release"
echo "✅ public repo ready at $DEST (fresh history, single commit)"
echo "   push it: cd $DEST && git remote add origin <url> && git push -u origin main"
