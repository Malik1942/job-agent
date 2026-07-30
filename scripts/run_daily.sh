#!/usr/bin/env bash
# Daily runner for jobagent, safe for cron and launchd.
# Edit PROJECT_DIR to the absolute path of your jobagent folder, then:
#   chmod +x scripts/run_daily.sh
#
# It activates the venv, loads secrets from scripts/.env (git-ignored) if
# present, runs the pipeline, and appends output to output/daily.log.

set -euo pipefail

PROJECT_DIR="${JOBAGENT_DIR:-$HOME/jobagent}"
cd "$PROJECT_DIR"

# Activate the virtualenv if it exists.
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

mkdir -p output
echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >> output/daily.log
python scripts/run_with_env.py python -m jobagent.cli run --config config.yaml >> output/daily.log 2>&1
echo "" >> output/daily.log
