#!/usr/bin/env bash
#
# livekit-activity-gate.sh - Classifies whether the LiveKit agent worker is
# currently IDLE, ACTIVE, or AMBIGUOUS based on a FRESH read of its journal
# (never cached/stale data - this script always re-queries journalctl live).
# ACTIVE or AMBIGUOUS must block any automatic restart of
# ptai-livekit-agent.service; only IDLE is safe to restart over.
#
# Looks for the most recent "livekit_agent_job_connected" event and the most
# recent "livekit_agent_job_shutdown" event in the unit's journal within the
# lookback window. If the last connect has no later matching shutdown, a
# session is presumed still active. If the journal within the window is
# empty (e.g. service just started, journal rotated), the result is
# AMBIGUOUS rather than guessed as IDLE - fail safe, never assume idle.
#
# Runs on the target EC2 instance (via SSM Run Command or locally on the
# box). This script only classifies - it never restarts anything itself;
# the caller decides what to do with the classification.
#
# Usage:
#   livekit-activity-gate.sh [--since '<journalctl --since value>'] [--unit <name>]
#
# Output: exactly one of IDLE / ACTIVE / AMBIGUOUS on stdout.
# Exit codes: always 0 - this script classifies, it does not fail by itself.
set -Eeuo pipefail

SINCE="24 hours ago"
UNIT="ptai-livekit-agent.service"

while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="${2:-}"; shift 2 ;;
    --unit) UNIT="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--since '<journalctl --since value>'] [--unit <name>]"
      exit 0
      ;;
    *) shift ;;
  esac
done

log() { echo "[livekit-activity-gate] $*" >&2; }

if ! command -v journalctl >/dev/null 2>&1; then
  log "journalctl not available - cannot determine activity, failing safe"
  echo "AMBIGUOUS"
  exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
  log "jq not available - cannot parse journal, failing safe"
  echo "AMBIGUOUS"
  exit 0
fi

# Always a fresh, live query - never a cached/previously-captured result.
JOURNAL=$(journalctl -u "$UNIT" --since "$SINCE" --output=json 2>/dev/null) || true

if [ -z "$JOURNAL" ]; then
  log "no journal entries for $UNIT since '$SINCE' - cannot confirm idle, failing safe"
  echo "AMBIGUOUS"
  exit 0
fi

LAST_CONNECT_TS=$(echo "$JOURNAL" | jq -r 'select(.MESSAGE // "" | contains("livekit_agent_job_connected")) | .__REALTIME_TIMESTAMP' 2>/dev/null | sort -n | tail -1)
LAST_SHUTDOWN_TS=$(echo "$JOURNAL" | jq -r 'select(.MESSAGE // "" | contains("livekit_agent_job_shutdown")) | .__REALTIME_TIMESTAMP' 2>/dev/null | sort -n | tail -1)

if [ -z "$LAST_CONNECT_TS" ]; then
  log "no job-connected events found in window - IDLE"
  echo "IDLE"
  exit 0
fi

if [ -z "$LAST_SHUTDOWN_TS" ]; then
  log "job-connected event found with no matching shutdown - ACTIVE"
  echo "ACTIVE"
  exit 0
fi

if [ "$LAST_CONNECT_TS" -gt "$LAST_SHUTDOWN_TS" ]; then
  log "most recent connect ($LAST_CONNECT_TS) is newer than most recent shutdown ($LAST_SHUTDOWN_TS) - ACTIVE"
  echo "ACTIVE"
else
  log "most recent shutdown ($LAST_SHUTDOWN_TS) is newer than most recent connect ($LAST_CONNECT_TS) - IDLE"
  echo "IDLE"
fi
