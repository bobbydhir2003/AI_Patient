#!/usr/bin/env bash
#
# test-lock-reproducibility.sh - Proves backend/requirements.lock.txt
# produces byte-identical installed package sets across independent builds,
# and that both builds are functionally sound (real binaries execute, both
# entrypoints import cleanly, zero broken shebangs). This is the regression
# test for the dependency-locking checkpoint: it directly demonstrates the
# property that closes the Phase 3B-3 reproducibility gap (installs keyed on
# ranged requirements.txt could silently differ build to build; installs
# keyed on the hash-verified lock cannot).
#
# Builds two temporary, fully independent venvs under a throwaway directory -
# never touches /opt/ptai or any real system state. Requires Linux, uv, and
# Python 3.12 available via uv (matches the target EC2 instance or Linux CI,
# not necessarily macOS - --require-hashes resolution is platform-specific).
#
# Usage: test-lock-reproducibility.sh [path/to/requirements.lock.txt]
#   defaults to backend/requirements.lock.txt relative to the repo root.
#
# Exit codes: 0 - all checks passed. 1 - one or more checks failed.
set -Eeuo pipefail

log() { echo "[test-lock-reproducibility] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOCK="${1:-$REPO_ROOT/backend/requirements.lock.txt}"

[ -f "$LOCK" ] || die "lock file not found: $LOCK"
command -v uv >/dev/null 2>&1 || die "uv is required but not found on PATH"

WORKDIR=$(mktemp -d /tmp/ptai-lock-repro-test.XXXXXX)
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

build_venv() {
  local venv_path="$1"
  log "Building $venv_path from $LOCK"
  uv venv --python 3.12 "$venv_path" >&2
  uv pip install --python "$venv_path/bin/python" -r "$LOCK" --require-hashes >&2
}

VENV1="$WORKDIR/venv1"
VENV2="$WORKDIR/venv2"

echo "=== Building two independent venvs from the same lock ==="
build_venv "$VENV1"
build_venv "$VENV2"
pass "both venvs built successfully with --require-hashes"

echo
echo "=== Comparing normalized package/version listings ==="
uv pip list --python "$VENV1/bin/python" --format=freeze 2>/dev/null | sort > "$WORKDIR/packages1.txt"
uv pip list --python "$VENV2/bin/python" --format=freeze 2>/dev/null | sort > "$WORKDIR/packages2.txt"
if diff -u "$WORKDIR/packages1.txt" "$WORKDIR/packages2.txt" > "$WORKDIR/packages.diff"; then
  pass "installed package/version sets are byte-identical across both builds ($(wc -l < "$WORKDIR/packages1.txt") packages)"
else
  fail "installed package/version sets differ between builds:"
  cat "$WORKDIR/packages.diff"
fi

echo
echo "=== Execution + shebang checks on both venvs ==="
for label in venv1 venv2; do
  V="$WORKDIR/$label"
  ok=true
  "$V/bin/python" --version >/dev/null 2>&1 || { ok=false; fail "$label: python --version failed"; }
  "$V/bin/uvicorn" --version >/dev/null 2>&1 || { ok=false; fail "$label: uvicorn --version failed"; }
  "$V/bin/alembic" --version >/dev/null 2>&1 || { ok=false; fail "$label: alembic --version failed"; }
  "$V/bin/pytest" --version >/dev/null 2>&1 || { ok=false; fail "$label: pytest --version failed"; }

  building_refs=$(grep -rl '\.building' "$V/bin" 2>/dev/null || true)
  if [ -n "$building_refs" ]; then
    ok=false
    fail "$label: found .building references: $building_refs"
  fi

  for f in "$V/bin/"*; do
    [ -f "$f" ] || continue
    file "$f" | grep -qi 'text' || continue
    first_line=$(head -1 "$f")
    case "$first_line" in
      "#!"*)
        target=$(echo "$first_line" | sed 's/^#!//' | awk '{print $1}')
        [ -e "$target" ] || { fail "$label: broken shebang in $f -> $target"; ok=false; }
        ;;
    esac
  done

  if [ "$ok" = "true" ]; then
    pass "$label: all real binaries execute correctly, zero .building references, zero broken shebangs"
  fi
done

echo
echo "=== Isolated import checks (app.main, app.livekit_agent.worker) ==="
BACKEND_APP_DIR="$REPO_ROOT/backend"
if [ -d "$BACKEND_APP_DIR/app" ]; then
  for label in venv1 venv2; do
    V="$WORKDIR/$label"
    if (
      cd "$BACKEND_APP_DIR"
      env -i PATH=/usr/bin:/bin HOME="${HOME:-/tmp}" DATABASE_URL="sqlite://" REDIS_URL="" \
        REDIS_REQUIRED_FOR_CONCURRENCY=false OPENAI_API_KEY="" ENVIRONMENT=test \
        "$V/bin/python" -c "import app.main" >/dev/null 2>"$WORKDIR/$label-main-import.err"
    ); then
      pass "$label: app.main isolated import OK"
    else
      fail "$label: app.main isolated import failed"
      cat "$WORKDIR/$label-main-import.err"
    fi
    if (
      cd "$BACKEND_APP_DIR"
      env -i PATH=/usr/bin:/bin HOME="${HOME:-/tmp}" DATABASE_URL="sqlite://" REDIS_URL="" \
        REDIS_REQUIRED_FOR_CONCURRENCY=false OPENAI_API_KEY="" ENVIRONMENT=test \
        "$V/bin/python" -c "import app.livekit_agent.worker" >/dev/null 2>"$WORKDIR/$label-worker-import.err"
    ); then
      pass "$label: app.livekit_agent.worker isolated import OK"
    else
      fail "$label: app.livekit_agent.worker isolated import failed"
      cat "$WORKDIR/$label-worker-import.err"
    fi
  done
else
  log "backend/app not found at $BACKEND_APP_DIR -- skipping isolated import checks (lock-only environment, not a full checkout)"
fi

echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
