#!/usr/bin/env bash
# jobagent one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Malik1942/job-agent/main/install.sh | bash
#
# What it does (and nothing else):
#   1. Finds Python 3.10+ (tells you how to get it if missing)
#   2. Clones the repo to ~/job-agent (or pulls if already there)
#   3. Creates a virtualenv and installs jobagent + the browser filler
#   4. Downloads Chromium for Playwright (skip: JOBAGENT_SKIP_BROWSER=1)
#   5. Launches the setup wizard in your browser
#
# Options via env vars:
#   JOBAGENT_DIR=~/somewhere     install location (default ~/job-agent)
#   JOBAGENT_SKIP_BROWSER=1      skip the Chromium download (scan-only use)
#   JOBAGENT_NO_WIZARD=1         install only; don't launch the setup wizard
set -euo pipefail

REPO="${JOBAGENT_REPO:-https://github.com/Malik1942/job-agent.git}"
DIR="${JOBAGENT_DIR:-$HOME/job-agent}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Python 3.10+ ------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 \
     && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"
    break
  fi
done
if [ -z "$PY" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    fail "Python 3.10+ not found. Install it with:  brew install python@3.12  (or from python.org), then re-run this installer."
  fi
  fail "Python 3.10+ not found. Install it with your package manager (e.g. apt install python3.12 python3.12-venv), then re-run this installer."
fi
say "Using Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

command -v git >/dev/null 2>&1 || fail "git not found. Install git, then re-run this installer."

# --- 2. Clone or update ---------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating existing checkout at $DIR"
  git -C "$DIR" pull --ff-only || say "Could not fast-forward (local changes?) — continuing with what's there."
else
  [ -e "$DIR" ] && fail "$DIR exists but is not a git checkout. Move it or set JOBAGENT_DIR to another path."
  say "Cloning to $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

# --- 3. Virtualenv + package ---------------------------------------------
if [ ! -x .venv/bin/python ]; then
  say "Creating virtualenv"
  "$PY" -m venv .venv
fi
say "Installing jobagent (+ browser filler)"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[browser]"

# --- 4. Chromium for the form filler --------------------------------------
if [ "${JOBAGENT_SKIP_BROWSER:-0}" = "1" ]; then
  say "Skipping Chromium download (JOBAGENT_SKIP_BROWSER=1) — scanning works; form filling needs it later: .venv/bin/playwright install chromium"
else
  say "Downloading Chromium for the form filler (one-time, ~150 MB)"
  .venv/bin/playwright install chromium
fi

# --- 5. Done: hand off to the wizard --------------------------------------
say "Installed. Command: $DIR/.venv/bin/jobagent"
echo
echo "  Add it to your PATH if you like:"
echo "    echo 'export PATH=\"$DIR/.venv/bin:\$PATH\"' >> ~/.zshrc"
echo
if [ "${JOBAGENT_NO_WIZARD:-0}" = "1" ]; then
  echo "  Next: cd $DIR && .venv/bin/jobagent setup --config config.yaml"
  exit 0
fi
say "Launching the setup wizard (Ctrl-C when you're done)"
exec .venv/bin/jobagent setup --config "$DIR/config.yaml"
