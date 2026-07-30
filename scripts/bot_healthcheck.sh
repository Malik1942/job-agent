#!/bin/zsh
# Health-check for the Slack approval bot.
#
# launchd's KeepAlive only restarts the bot when the PROCESS dies. The
# failure we actually hit (2026-07-20) is a wedge: the process stays alive
# but its socket-mode connection to Slack is gone, stuck in a
# BrokenPipeError reconnect loop after the Mac slept — so Slack taps and
# slash commands silently go nowhere for days. This script kickstarts the
# bot whenever the process is missing or has no ESTABLISHED TCP connection.
#
# Installed via scripts/install_launchd.sh (renders the plist templates;
# runs every 5 min). Logs only when it acts, to output/bot-healthcheck.log.
#
# Test overrides:
#   HC_PID=<pid>   check this pid instead of the real bot
#   HC_DRY_RUN=1   log the verdict but do not kickstart

LABEL="com.jobagent.approval-bot"
JOBAGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${HC_LOG:-$JOBAGENT_DIR/output/bot-healthcheck.log}"
MIN_UPTIME_SECS=90   # a fresh bot needs a moment to connect; don't judge it yet

log() { print "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" }

restart() {
  if [[ -n "$HC_DRY_RUN" ]]; then
    log "DRY_RUN: would kickstart -- $1"
  else
    log "kickstart -- $1"
    launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>> "$LOG"
  fi
}

pid=${HC_PID:-$(pgrep -f "jobagent.cli approval-bot" | head -1)}
if [[ -z "$pid" ]]; then
  restart "no approval-bot process found"
  exit 0
fi

# Uptime in seconds from ps etime (formats: MM:SS, HH:MM:SS, D-HH:MM:SS).
etime=$(ps -o etime= -p "$pid" | tr -d ' ')
[[ -z "$etime" ]] && exit 0   # pid vanished between pgrep and ps; next run decides
typeset -i days=0 secs=0
rest=$etime
if [[ $rest == *-* ]]; then days=${rest%%-*}; rest=${rest#*-}; fi
parts=(${(s.:.)rest})
if (( ${#parts} == 3 )); then
  secs=$(( parts[1] * 3600 + parts[2] * 60 + parts[3] ))
else
  secs=$(( parts[1] * 60 + parts[2] ))
fi
secs=$(( secs + days * 86400 ))
(( secs < MIN_UPTIME_SECS )) && exit 0

if ! lsof -p "$pid" -a -i TCP -s TCP:ESTABLISHED -n 2>/dev/null | grep -q TCP; then
  restart "pid $pid up ${secs}s with no established TCP connection to Slack"
fi
