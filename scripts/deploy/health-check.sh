#!/usr/bin/env bash
#
# health-check.sh - Read-only health check of the running application. Makes
# no filesystem mutation and no production mutation of any kind. Safe to run
# at any time, including right now, against live production.
#
# Usage:
#   health-check.sh [--mode local|public|both] [--local-url <url>] [--public-url <url>] [--health-path </api/health>]
#
# --local-url defaults to http://127.0.0.1:8000 (where ptai.service actually
# listens) and exists mainly so this script's local-mode HTTP/JSON handling
# can be exercised against a synthetic/mock endpoint in tests without ever
# pointing at production's real port.
#
# Exit codes (Nagios-style, distinct from the rest of this directory because
# there are three meaningful states, not two):
#   0 - HEALTHY
#   1 - DEGRADED (reachable but something is off)
#   2 - FAILED (not reachable / not running)
set -Eeuo pipefail

log() { echo "[health-check] $*" >&2; }

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "[health-check] ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 2
fi

MODE="both"
LOCAL_URL="http://127.0.0.1:8000"
PUBLIC_URL="${PTAI_PUBLIC_URL:-}"
HEALTH_PATH="/api/health"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --local-url) LOCAL_URL="${2:-}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:-}"; shift 2 ;;
    --health-path) HEALTH_PATH="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--mode local|public|both] [--local-url <url>] [--public-url <url>] [--health-path </api/health>]"
      exit 0
      ;;
    *) log "ERROR: Unknown argument: $1"; exit 2 ;;
  esac
done

case "$MODE" in local|public|both) ;; *) log "ERROR: --mode must be local, public, or both"; exit 2 ;; esac

STATUS="HEALTHY"   # only ever escalates below as issues are found - a
                    # severity ratchet, never a plain overwrite, so a FAILED
                    # finding can never be masked by a later DEGRADED one
                    # (or vice versa) regardless of check order.
declare -a ISSUES=()

note_degraded() { ISSUES+=("$1"); log "DEGRADED: $1"; [ "$STATUS" = "FAILED" ] || STATUS="DEGRADED"; }
note_failed()   { ISSUES+=("$1"); log "FAILED: $1"; STATUS="FAILED"; }

check_service() {
  local unit="$1"
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not available, skipping unit check for $unit"
    return
  fi
  local state
  state=$(systemctl is-active "$unit" 2>&1 || true)
  if [ "$state" = "active" ]; then
    log "OK: $unit is active"
  else
    note_failed "$unit is not active (state: $state)"
  fi
}

if [ "$MODE" = "local" ] || [ "$MODE" = "both" ]; then
  check_service "ptai.service"
  check_service "ptai-livekit-agent.service"

  if command -v curl >/dev/null 2>&1; then
    LOCAL_RESPONSE=$(curl -fsS --max-time 5 "${LOCAL_URL%/}${HEALTH_PATH}" 2>&1) && LOCAL_OK=true || LOCAL_OK=false
    if [ "$LOCAL_OK" = "true" ]; then
      if command -v jq >/dev/null 2>&1 && echo "$LOCAL_RESPONSE" | jq -e '.status == "ok"' >/dev/null 2>&1; then
        log "OK: local ${HEALTH_PATH} reports status=ok"
      else
        note_degraded "local ${HEALTH_PATH} responded but did not report status=ok: $LOCAL_RESPONSE"
      fi
    else
      note_failed "local ${HEALTH_PATH} unreachable: $LOCAL_RESPONSE"
    fi
  else
    log "curl not available, skipping local HTTP check"
  fi
fi

if [ "$MODE" = "public" ] || [ "$MODE" = "both" ]; then
  if [ -z "$PUBLIC_URL" ]; then
    log "No --public-url given and PTAI_PUBLIC_URL not set -- skipping public check"
  elif command -v curl >/dev/null 2>&1; then
    # curl's -w already prints "000" on connection/transport failure even
    # though it also exits non-zero, so the fallback only needs to cover the
    # (rare) case where it printed nothing at all.
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${PUBLIC_URL%/}${HEALTH_PATH}") || true
    [ -n "$HTTP_CODE" ] || HTTP_CODE="000"
    if [ "$HTTP_CODE" = "200" ]; then
      log "OK: public ${PUBLIC_URL%/}${HEALTH_PATH} returned 200"
    else
      note_failed "public ${PUBLIC_URL%/}${HEALTH_PATH} returned $HTTP_CODE"
    fi
  else
    log "curl not available, skipping public HTTP check"
  fi
fi

echo "STATUS=$STATUS"
for issue in "${ISSUES[@]}"; do
  echo "ISSUE: $issue"
done

case "$STATUS" in
  HEALTHY) exit 0 ;;
  DEGRADED) exit 1 ;;
  FAILED) exit 2 ;;
esac
