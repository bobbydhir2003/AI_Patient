#!/usr/bin/env bash
#
# test-classify-changes-matrix.sh - Runs classify-changes.sh against a
# synthetic throwaway git repo covering every category the script
# recognizes, plus mixed/unknown/empty edge cases. Never touches the real
# repository - builds its own temp git repo. Requires bash >= 4.4, git, jq.
#
# Usage: test-classify-changes-matrix.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFY="$SCRIPT_DIR/classify-changes.sh"
[ -x "$CLASSIFY" ] || { echo "ERROR: $CLASSIFY not found or not executable" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq required" >&2; exit 1; }

REPO=$(mktemp -d /tmp/ptai-classify-test.XXXXXX)
trap 'rm -rf -- "$REPO"' EXIT

git -C "$REPO" init -q
git -C "$REPO" config user.email test@example.com
git -C "$REPO" config user.name "Test"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

commit_files() {
  # commit_files <message> <path> [<path> ...] -- creates/updates each path
  # with placeholder content and commits.
  local msg="$1"; shift
  for p in "$@"; do
    mkdir -p "$(dirname "$REPO/$p")"
    echo "// change $(date +%s%N)" >> "$REPO/$p"
  done
  git -C "$REPO" add -A >/dev/null
  git -C "$REPO" commit -q -m "$msg"
}

# Baseline commit so every scenario below diffs against a real prior SHA.
mkdir -p "$REPO/backend/app"
echo "baseline" > "$REPO/backend/app/main.py"
git -C "$REPO" add -A >/dev/null
git -C "$REPO" commit -q -m "baseline"

run_case() {
  local desc="$1" base="$2" target="$3"
  "$CLASSIFY" --base "$base" --target "$target" --repo-path "$REPO" 2>/dev/null
}

expect_json() {
  # expect_json <desc> <json> <jq-filter> <expected>
  local desc="$1" json="$2" filter="$3" expected="$4"
  local actual
  actual=$(echo "$json" | jq -r "$filter")
  if [ "$actual" = "$expected" ]; then
    pass "$desc: $filter == $expected"
  else
    fail "$desc: $filter expected '$expected', got '$actual'"
  fi
}

# ---------------------------------------------------------------------------
echo "=== Case: frontend-only ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "frontend change" "src/App.tsx" "package.json"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case frontend "$BASE" "$TARGET")
expect_json "frontend-only" "$JSON" '.actions.build_frontend' "true"
expect_json "frontend-only" "$JSON" '.actions.restart_ptai' "false"
expect_json "frontend-only" "$JSON" '.actions.restart_livekit' "false"
expect_json "frontend-only" "$JSON" '.actions.venv_rebuild' "false"

# ---------------------------------------------------------------------------
echo "=== Case: FastAPI-only ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "fastapi change" "backend/app/api/sessions.py"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case fastapi "$BASE" "$TARGET")
expect_json "fastapi-only" "$JSON" '.actions.restart_ptai' "true"
expect_json "fastapi-only" "$JSON" '.actions.restart_livekit' "false"
expect_json "fastapi-only" "$JSON" '.actions.build_frontend' "false"
expect_json "fastapi-only" "$JSON" '.categories.FASTAPI | length' "1"

# ---------------------------------------------------------------------------
echo "=== Case: LiveKit-only ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "livekit change" "backend/app/livekit_agent/worker.py"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case livekit "$BASE" "$TARGET")
expect_json "livekit-only" "$JSON" '.actions.restart_livekit' "true"
expect_json "livekit-only" "$JSON" '.actions.restart_ptai' "false"
expect_json "livekit-only" "$JSON" '.categories.LIVEKIT | length' "1"

# ---------------------------------------------------------------------------
echo "=== Case: shared backend (backend/app/* outside api/schemas/livekit) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "shared backend change" "backend/app/core/config.py"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case shared_backend "$BASE" "$TARGET")
expect_json "shared-backend" "$JSON" '.actions.restart_ptai' "true"
expect_json "shared-backend" "$JSON" '.actions.restart_livekit' "true"
expect_json "shared-backend" "$JSON" '.categories.SHARED_BACKEND | length' "1"

# ---------------------------------------------------------------------------
echo "=== Case: requirements.txt change ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "deps change" "backend/requirements.txt"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case requirements "$BASE" "$TARGET")
expect_json "requirements.txt" "$JSON" '.actions.venv_rebuild' "true"
expect_json "requirements.txt" "$JSON" '.categories.DEPENDENCIES | length' "1"

echo "=== Case: requirements.lock.txt change (dependency-locking checkpoint fix) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "lock change" "backend/requirements.lock.txt"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case lockfile "$BASE" "$TARGET")
expect_json "requirements.lock.txt" "$JSON" '.actions.venv_rebuild' "true"
expect_json "requirements.lock.txt" "$JSON" '.categories.DEPENDENCIES | length' "1"
expect_json "requirements.lock.txt" "$JSON" '.categories.UNKNOWN | length' "0"

# ---------------------------------------------------------------------------
echo "=== Case: migration change ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "migration" "backend/app/database/migrations/versions/0003_add_x.py"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case migration "$BASE" "$TARGET")
expect_json "migration" "$JSON" '.actions.migration_gate' "true"
expect_json "migration" "$JSON" '.actions.restart_ptai' "true"
expect_json "migration" "$JSON" '.categories.DATABASE_MIGRATION | length' "1"

# ---------------------------------------------------------------------------
echo "=== Case: deploy script change (deployment tooling - explicitly NONE, never UNKNOWN, no restart) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "deploy script" "scripts/deploy/prepare-release.sh" "scripts/production-preflight.sh"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case deploy_script "$BASE" "$TARGET")
expect_json "deploy-script-change" "$JSON" '.categories.UNKNOWN | length' "0"
expect_json "deploy-script-change" "$JSON" '.categories.NONE | length' "2"
expect_json "deploy-script-change" "$JSON" '.actions.restart_ptai' "false"
expect_json "deploy-script-change" "$JSON" '.actions.restart_livekit' "false"
expect_json "deploy-script-change" "$JSON" '.actions.venv_rebuild' "false"

# ---------------------------------------------------------------------------
echo "=== Case: infrastructure/nginx change ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "nginx" "deploy/nginx/ptai.conf"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case nginx "$BASE" "$TARGET")
expect_json "nginx" "$JSON" '.actions.nginx_gate' "true"
expect_json "nginx" "$JSON" '.categories.NGINX | length' "1"

echo "=== Case: systemd/infrastructure change (no dedicated category - must default conservatively via UNKNOWN) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "systemd" "deploy/systemd/ptai.service"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case systemd "$BASE" "$TARGET")
expect_json "systemd-change" "$JSON" '.categories.UNKNOWN | length' "1"
expect_json "systemd-change" "$JSON" '.actions.restart_ptai' "true"

# ---------------------------------------------------------------------------
echo "=== Case: mixed changes (frontend + fastapi + migration in one diff) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "mixed" "src/pages/Home.tsx" "backend/app/api/voice.py" \
  "backend/app/database/migrations/versions/0004_add_y.py"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case mixed "$BASE" "$TARGET")
expect_json "mixed" "$JSON" '.actions.build_frontend' "true"
expect_json "mixed" "$JSON" '.actions.restart_ptai' "true"
expect_json "mixed" "$JSON" '.actions.migration_gate' "true"
expect_json "mixed" "$JSON" '.categories.FRONTEND | length' "1"
expect_json "mixed" "$JSON" '.categories.FASTAPI | length' "1"
expect_json "mixed" "$JSON" '.categories.DATABASE_MIGRATION | length' "1"

# ---------------------------------------------------------------------------
echo "=== Case: unknown paths (top-level file matching nothing) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "unknown" "Makefile"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case unknown "$BASE" "$TARGET")
expect_json "unknown-path" "$JSON" '.categories.UNKNOWN | length' "1"
expect_json "unknown-path" "$JSON" '.actions.restart_ptai' "true"
expect_json "unknown-path" "$JSON" '.actions.restart_livekit' "true"
expect_json "unknown-path" "$JSON" '.actions.venv_rebuild' "true"

echo "=== Case: NONE paths (docs/tests/CI - must NOT trigger any restart) ==="
BASE=$(git -C "$REPO" rev-parse HEAD)
commit_files "docs" "docs/notes.md" "backend/tests/test_x.py" ".github/workflows/ci.yml"
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case none_paths "$BASE" "$TARGET")
expect_json "none-paths" "$JSON" '.categories.NONE | length' "3"
expect_json "none-paths" "$JSON" '.actions.restart_ptai' "false"
expect_json "none-paths" "$JSON" '.actions.restart_livekit' "false"
expect_json "none-paths" "$JSON" '.actions.build_frontend' "false"

# ---------------------------------------------------------------------------
echo "=== Case: empty change set (base == target) ==="
TARGET=$(git -C "$REPO" rev-parse HEAD)
JSON=$(run_case empty "$TARGET" "$TARGET")
expect_json "empty-changeset" "$JSON" '.changed_files | length' "0"
expect_json "empty-changeset" "$JSON" '.actions.build_frontend' "false"
expect_json "empty-changeset" "$JSON" '.actions.restart_ptai' "false"
expect_json "empty-changeset" "$JSON" '.actions.restart_livekit' "false"
expect_json "empty-changeset" "$JSON" '.actions.venv_rebuild' "false"
expect_json "empty-changeset" "$JSON" '.actions.migration_gate' "false"
expect_json "empty-changeset" "$JSON" '.actions.nginx_gate' "false"

# ---------------------------------------------------------------------------
echo "=== Case: invalid ref (must fail, exit 1) ==="
set +e
"$CLASSIFY" --base "not-a-real-ref" --target HEAD --repo-path "$REPO" >/tmp/cc_invalid.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ]; then
  pass "invalid ref: correctly rejected (exit 1)"
else
  fail "invalid ref: expected exit 1, got exit=$RC"; cat /tmp/cc_invalid.out
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
