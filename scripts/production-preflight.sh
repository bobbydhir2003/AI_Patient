#!/usr/bin/env bash
#
# production-preflight.sh
#
# READ-ONLY pre-deployment health/sanity check for the PT AI Patient
# production host. Designed to run ON the target server - every path/command
# below assumes it is executing directly on that machine (systemctl, local
# curl, local git checkout), not over a remote connection.
#
# Two ways to use it today (before it is ever deployed to the server):
#   ssh <host> bash -s < scripts/production-preflight.sh
# or, once actually present on the server in a future phase:
#   ./scripts/production-preflight.sh
#
# This script NEVER modifies anything: no service restarts/reloads, no git
# operations beyond read-only `status`/`rev-parse`, no package installs, no
# migrations, no file writes of any kind. It never reads or prints a secret
# value (passwords, DATABASE_URL, API keys) - only structural, non-sensitive
# facts (service state, SHA, disk/memory numbers, Python version strings,
# HTTP status).
#
# Output: one line per check - "[PASS ]", "[WARN ]", or "[BLOCK]" - plus a
# summary. A BLOCK means a deployment prerequisite is unhealthy and a future
# CD workflow should refuse to proceed; a WARN is informational/known-risk
# and should not by itself stop a deployment.
#
# Exit code:
#   0   no BLOCKing failures (WARNings may still be present - read the output)
#   1   at least one BLOCKing failure - do not deploy

set -uo pipefail

APP_DIR="${PTAI_APP_DIR:-/home/ubuntu/AI_Patient}"
BACKEND_DIR="$APP_DIR/backend"
FRONTEND_DIR="${PTAI_FRONTEND_DIR:-/var/www/ptai}"
HEALTH_URL="${PTAI_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
# CI/Dockerfile-pinned version (backend/requirements.txt has precise pins
# verified only against this line) - a mismatch is a known, tracked WARNing,
# not by itself a reason to block (production is currently healthy on
# different versions - see docs/production-deployment-architecture.md).
EXPECTED_PYTHON="3.12"

BLOCKING_FAILURES=0
WARNINGS=0

pass()  { printf '[PASS ] %s: %s\n' "$1" "$2"; }
warn()  { printf '[WARN ] %s: %s\n' "$1" "$2"; WARNINGS=$((WARNINGS + 1)); }
block() { printf '[BLOCK] %s: %s\n' "$1" "$2"; BLOCKING_FAILURES=$((BLOCKING_FAILURES + 1)); }

check_service() {
  # $1=unit name  $2=report label  $3=severity on inactive ("block"|"warn")
  local unit="$1" label="$2" severity="${3:-block}"
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    pass "$label" "active"
  elif [ "$severity" = "block" ]; then
    block "$label" "not active"
  else
    warn "$label" "not active"
  fi
}

echo "=== PT AI Patient production preflight - $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo

# --- 1: application directory -----------------------------------------------
if [ -d "$APP_DIR" ]; then
  pass "app_dir_exists" "$APP_DIR"
else
  block "app_dir_exists" "$APP_DIR not found"
fi

# --- 2/3/4: git repository, SHA, branch -------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  pass "git_repo_exists" "found"
  GIT_SHA=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null) || GIT_SHA=""
  GIT_BRANCH=$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null) || GIT_BRANCH=""
  if [ -n "$GIT_SHA" ]; then pass "git_sha" "$GIT_SHA"; else block "git_sha" "could not determine current SHA"; fi
  if [ -n "$GIT_BRANCH" ]; then pass "git_branch" "$GIT_BRANCH"; else warn "git_branch" "could not determine current branch"; fi
else
  block "git_repo_exists" "$APP_DIR/.git not found"
fi

# --- 5: dirty working tree (tracked-file modifications) ---------------------
if [ -d "$APP_DIR/.git" ]; then
  DIRTY=$(git -C "$APP_DIR" status --porcelain=v1 2>/dev/null | grep -v '^??' || true)
  if [ -z "$DIRTY" ]; then
    pass "git_tree_clean" "no modified tracked files"
  else
    warn "git_tree_clean" "modified tracked file(s): $(echo "$DIRTY" | awk '{print $2}' | tr '\n' ' ')"
  fi
fi

# --- 6: untracked files ------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  UNTRACKED=$(git -C "$APP_DIR" status --porcelain=v1 2>/dev/null | grep '^??' | awk '{print $2}' | tr '\n' ' ')
  if [ -z "$UNTRACKED" ]; then
    pass "git_no_untracked" "none"
  else
    warn "git_no_untracked" "untracked path(s): $UNTRACKED"
  fi
fi

# --- 7: disk free space -------------------------------------------------------
DISK_LINE=$(df -Pk "$APP_DIR" 2>/dev/null | tail -1)
DISK_AVAIL_KB=$(echo "$DISK_LINE" | awk '{print $4}')
DISK_USE_PCT=$(echo "$DISK_LINE" | awk '{print $5}' | tr -d '%')
if [ -z "${DISK_AVAIL_KB:-}" ]; then
  warn "disk_space" "could not determine disk usage for $APP_DIR"
elif [ "$DISK_AVAIL_KB" -lt 1048576 ]; then
  block "disk_space" "only ${DISK_AVAIL_KB}KB free (${DISK_USE_PCT}% used)"
elif [ "$DISK_USE_PCT" -ge 85 ]; then
  warn "disk_space" "${DISK_USE_PCT}% used - getting tight"
else
  pass "disk_space" "${DISK_USE_PCT}% used, ${DISK_AVAIL_KB}KB free"
fi

# --- 8: memory baseline -------------------------------------------------------
MEM_AVAIL_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')
if [ -z "${MEM_AVAIL_MB:-}" ]; then
  warn "memory_baseline" "could not determine available memory"
elif [ "$MEM_AVAIL_MB" -lt 200 ]; then
  block "memory_baseline" "only ${MEM_AVAIL_MB}MB available"
else
  pass "memory_baseline" "${MEM_AVAIL_MB}MB available"
fi

# --- 9/10/11/12/24: core services --------------------------------------------
check_service ptai "ptai_service_active" block
check_service ptai-livekit-agent "livekit_service_active" block
check_service nginx "nginx_service_active" block
check_service redis-server "redis_service_active" block
check_service postgresql "postgresql_service_active" block

# --- 13: redis-cli ping -------------------------------------------------------
if command -v redis-cli >/dev/null 2>&1; then
  if [ "$(redis-cli ping 2>/dev/null)" = "PONG" ]; then
    pass "redis_ping" "PONG"
  else
    block "redis_ping" "did not respond PONG"
  fi
else
  warn "redis_ping" "redis-cli not found on PATH"
fi

# --- 14/15: python venvs exist ------------------------------------------------
if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  pass "fastapi_venv_exists" "found"
else
  block "fastapi_venv_exists" "$BACKEND_DIR/.venv/bin/python not found"
fi
if [ -x "$BACKEND_DIR/.venv-livekit/bin/python" ]; then
  pass "livekit_venv_exists" "found"
else
  block "livekit_venv_exists" "$BACKEND_DIR/.venv-livekit/bin/python not found"
fi

# --- 16/17: python versions (informational + known-mismatch warning) --------
if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  FASTAPI_PY=$("$BACKEND_DIR/.venv/bin/python" --version 2>&1 | awk '{print $2}')
  case "$FASTAPI_PY" in
    "$EXPECTED_PYTHON".*) pass "fastapi_python_version" "$FASTAPI_PY (matches CI)" ;;
    *) warn "fastapi_python_version" "$FASTAPI_PY (CI/Dockerfile pin to $EXPECTED_PYTHON - known, tracked mismatch)" ;;
  esac
fi
if [ -x "$BACKEND_DIR/.venv-livekit/bin/python" ]; then
  LIVEKIT_PY=$("$BACKEND_DIR/.venv-livekit/bin/python" --version 2>&1 | awk '{print $2}')
  case "$LIVEKIT_PY" in
    "$EXPECTED_PYTHON".*) pass "livekit_python_version" "$LIVEKIT_PY (matches CI)" ;;
    *) warn "livekit_python_version" "$LIVEKIT_PY (CI/Dockerfile pin to $EXPECTED_PYTHON - known, tracked mismatch)" ;;
  esac
fi

# --- 18: frontend directory ---------------------------------------------------
if [ -d "$FRONTEND_DIR" ]; then
  pass "frontend_dir_exists" "$FRONTEND_DIR"
else
  block "frontend_dir_exists" "$FRONTEND_DIR not found"
fi

# --- 19/20/21: local health endpoint ------------------------------------------
HEALTH_JSON=$(curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null) || HEALTH_JSON=""
if [ -z "$HEALTH_JSON" ]; then
  block "health_endpoint_responds" "no response from $HEALTH_URL"
else
  pass "health_endpoint_responds" "responded"
  if echo "$HEALTH_JSON" | grep -q '"database":"connected"'; then
    pass "health_database" "connected"
  else
    block "health_database" "not reported as connected"
  fi
  if echo "$HEALTH_JSON" | grep -q '"redis":"connected"'; then
    pass "health_redis" "connected"
  else
    block "health_redis" "not reported as connected"
  fi
fi

# --- 22: nginx config validation (read-only test, never reloads) ------------
if command -v nginx >/dev/null 2>&1; then
  if sudo -n nginx -t >/dev/null 2>&1 || nginx -t >/dev/null 2>&1; then
    pass "nginx_config_valid" "syntax OK"
  else
    warn "nginx_config_valid" "could not validate (insufficient privilege in this context, or real syntax error - check manually)"
  fi
else
  warn "nginx_config_valid" "nginx binary not found on PATH"
fi

# --- 23: backend port listening -----------------------------------------------
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q '127\.0\.0\.1:8000'; then
    pass "backend_port_listening" "127.0.0.1:8000"
  else
    block "backend_port_listening" "nothing listening on 127.0.0.1:8000"
  fi
else
  warn "backend_port_listening" "ss not available to check"
fi

# --- 25: alembic current revision (read-only query, never upgrades) ---------
if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  ALEMBIC_CURRENT=$(cd "$BACKEND_DIR" && "$BACKEND_DIR/.venv/bin/python" -m alembic current 2>/dev/null | tail -1)
  ALEMBIC_HEADS=$(cd "$BACKEND_DIR" && "$BACKEND_DIR/.venv/bin/python" -m alembic heads 2>/dev/null | tail -1)
  if [ -z "$ALEMBIC_CURRENT" ]; then
    warn "alembic_revision" "could not determine current revision"
  elif [ "$ALEMBIC_CURRENT" = "$ALEMBIC_HEADS" ]; then
    pass "alembic_revision" "$ALEMBIC_CURRENT (matches heads)"
  else
    warn "alembic_revision" "current=[$ALEMBIC_CURRENT] heads=[$ALEMBIC_HEADS] - schema behind code, migration needed at/before deploy"
  fi
else
  warn "alembic_revision" "backend venv not found - skipped"
fi

echo
echo "=== Summary: $BLOCKING_FAILURES blocking failure(s), $WARNINGS warning(s) ==="
if [ "$BLOCKING_FAILURES" -gt 0 ]; then
  echo "RESULT: DO NOT DEPLOY - one or more blocking prerequisites are unhealthy"
  exit 1
else
  echo "RESULT: preflight passed - review any warnings above before proceeding"
  exit 0
fi
