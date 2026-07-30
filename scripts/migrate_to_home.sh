#!/bin/zsh
# Migrate jobagent from ~/Documents/jobagent to ~/jobagent.
#
# Why: macOS TCC protects ~/Documents, so launchd jobs there can fail with
# "Operation not permitted" unless every binary in the chain has Full Disk
# Access. ~/jobagent is outside TCC's protected folders, removing the whole
# problem class.
#
# What it does, in order:
#   1. Stops the three launchd jobs (daily, approval-bot, approval-web)
#   2. Moves the project directory
#   3. Fixes every absolute path the move breaks:
#        - venv script shebangs + activate scripts
#        - the pip editable-install finder mapping (reinstalls -e .)
#        - the three launchd plists (rewritten + reinstalled)
#   4. Reloads the launchd jobs and verifies the bot comes back up
#
# Run it from your HOME directory (not from inside the project):
#   cd ~ && zsh ~/Documents/jobagent/scripts/migrate_to_home.sh
#
# Afterwards, start new terminal/Claude sessions in ~/jobagent.

set -euo pipefail

OLD="$HOME/Documents/jobagent"
NEW="$HOME/jobagent"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.jobagent.daily com.jobagent.approval-bot com.jobagent.approval-web)
UID_N=$(id -u)

# ---- preflight ---------------------------------------------------------------
if [[ "$PWD" == "$OLD"* ]]; then
  echo "ERROR: you are inside $OLD. cd ~ first, then re-run." >&2
  exit 1
fi
[[ -d "$OLD" ]] || { echo "ERROR: $OLD does not exist (already moved?)" >&2; exit 1; }
[[ -e "$NEW" ]] && { echo "ERROR: $NEW already exists; remove or rename it first." >&2; exit 1; }

echo "==> 1/6 Stopping launchd jobs"
for label in "${LABELS[@]}"; do
  launchctl bootout "gui/$UID_N/$label" 2>/dev/null \
    && echo "    stopped $label" \
    || echo "    $label was not loaded (ok)"
done
sleep 2

echo "==> 2/6 Moving $OLD -> $NEW"
mv "$OLD" "$NEW"

echo "==> 3/6 Fixing venv paths"
# Shebangs + activate scripts: text files only, replace the old prefix.
for f in "$NEW"/.venv/bin/*; do
  [[ -f "$f" ]] || continue
  if LC_ALL=C grep -qs "Documents/jobagent" "$f" 2>/dev/null; then
    LC_ALL=C sed -i '' "s|$HOME/Documents/jobagent|$NEW|g" "$f" 2>/dev/null \
      || true  # binary files (e.g. compiled entry points) are skipped
  fi
done
# Editable install embeds an absolute module path; reinstall regenerates it.
(cd "$NEW" && .venv/bin/python -m pip install -e . --no-deps -q)
echo "    venv fixed; import check:"
(cd "$NEW" && .venv/bin/python -c "import jobagent; print('      import jobagent OK')")

echo "==> 4/6 Rewriting + installing launchd plists"
for label in "${LABELS[@]}"; do
  src="$NEW/scripts/$label.plist"
  [[ -f "$src" ]] || { echo "    WARNING: $src missing, skipped"; continue; }
  sed "s|$HOME/Documents/jobagent|$NEW|g" "$src" > "$AGENTS/$label.plist"
  # keep the repo copy in sync so future installs use the new path
  sed -i '' "s|$HOME/Documents/jobagent|$NEW|g" "$src"
  echo "    installed $AGENTS/$label.plist"
done

echo "==> 5/6 Loading launchd jobs"
for label in "${LABELS[@]}"; do
  launchctl bootstrap "gui/$UID_N" "$AGENTS/$label.plist" \
    && echo "    loaded $label" \
    || echo "    WARNING: could not load $label"
done
sleep 4

echo "==> 6/6 Verifying"
launchctl list | grep jobagent || true
if tail -2 "$NEW/output/approval-bot.launchd.log" 2>/dev/null | grep -q "Bolt app is running"; then
  echo "    approval-bot reconnected to Slack ✓"
else
  echo "    WARNING: check $NEW/output/approval-bot.launchd.log"
fi
echo ""
echo "Kickstarting a daily run now to prove the TCC problem is gone"
launchctl kickstart "gui/$UID_N/com.jobagent.daily" || true
sleep 8
if [[ -s "$NEW/output/daily.launchd.err.log" ]] \
   && tail -1 "$NEW/output/daily.launchd.err.log" | grep -q "Operation not permitted"; then
  echo "    WARNING: still seeing 'Operation not permitted' — unexpected at $NEW"
else
  echo "    no launchd permission errors ✓ (run continues in background;"
  echo "    watch: tail -f $NEW/output/daily.log)"
fi
echo ""
echo "Done. Project now lives at $NEW"
echo "Start future terminal / Claude Code sessions there."
