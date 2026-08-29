#!/usr/bin/env bash
#
# test-activate-release-safety.sh - Validates activate-release.sh against
# synthetic/temp fixtures only. Never references or mutates the real
# /opt/ptai/current. Requires Linux (GNU mv -T) and bash >= 4.4.
#
# Usage: test-activate-release-safety.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTIVATE="$SCRIPT_DIR/activate-release.sh"
[ -x "$ACTIVATE" ] || { echo "ERROR: $ACTIVATE not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-activate-test.XXXXXX)
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

sha_n() { python3 -c "print(str($1) * 40)"; }
ASHA=$(sha_n 1)
BSHA=$(sha_n 2)
CSHA=$(sha_n 3)

# Build a fresh fixture with a fully-ready release+venv at $ASHA, not yet
# activated (no 'current' symlink).
new_fixture() {
  FIXTURE=$(mktemp -d "$WORKDIR/fixture.XXXXXX")
  mkdir -p "$FIXTURE"/{releases,venvs,shared}
  mkdir -p "$FIXTURE/releases/$ASHA/backend"
  echo "fastapi==0.100.0" > "$FIXTURE/releases/$ASHA/backend/requirements.lock.txt"
  HASH=$(sha256sum "$FIXTURE/releases/$ASHA/backend/requirements.lock.txt" | awk '{print $1}')
  echo "$HASH" > "$FIXTURE/releases/$ASHA/.venv-hash"
  touch "$FIXTURE/releases/$ASHA/.ready"
  mkdir -p "$FIXTURE/venvs/$HASH/backend" "$FIXTURE/venvs/$HASH/livekit"
  touch "$FIXTURE/venvs/$HASH/backend/.ready" "$FIXTURE/venvs/$HASH/livekit/.ready"
}

# Add a second fully-ready release ($BSHA), same venv hash as $ASHA (so
# activating it doesn't require a second venv).
add_release_b() {
  mkdir -p "$FIXTURE/releases/$BSHA/backend"
  cp "$FIXTURE/releases/$ASHA/backend/requirements.lock.txt" "$FIXTURE/releases/$BSHA/backend/requirements.lock.txt"
  echo "$HASH" > "$FIXTURE/releases/$BSHA/.venv-hash"
  touch "$FIXTURE/releases/$BSHA/.ready"
}

# ---------------------------------------------------------------------------
echo "=== Test 1: missing release (well-formed SHA, no release dir at all) ==="
new_fixture
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$CSHA" >/tmp/at1.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -L "$FIXTURE/current" ] && grep -q "release is not ready" /tmp/at1.out; then
  pass "missing release: exit 1, no current symlink created, clear diagnostic"
else
  fail "missing release: expected exit 1 + no current + clear diagnostic, got exit=$RC"; cat /tmp/at1.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: release dir exists but missing .ready ==="
new_fixture
mkdir -p "$FIXTURE/releases/$BSHA"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at2.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -L "$FIXTURE/current" ] && grep -q "not ready (no .ready marker)" /tmp/at2.out; then
  pass "missing .ready: exit 1, no current symlink created"
else
  fail "missing .ready: expected exit 1 + no current, got exit=$RC"; cat /tmp/at2.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 3a: malformed release - .ready present but .venv-hash missing ==="
new_fixture
mkdir -p "$FIXTURE/releases/$BSHA"
touch "$FIXTURE/releases/$BSHA/.ready"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at3a.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -L "$FIXTURE/current" ] && grep -q "missing .venv-hash" /tmp/at3a.out; then
  pass "malformed release (no .venv-hash): exit 1, no current symlink created"
else
  fail "malformed release (no .venv-hash): expected exit 1, got exit=$RC"; cat /tmp/at3a.out
fi

echo "=== Test 3b: malformed release - .venv-hash points to a venv that isn't ready ==="
new_fixture
mkdir -p "$FIXTURE/releases/$BSHA"
touch "$FIXTURE/releases/$BSHA/.ready"
echo "0000000000000000000000000000000000000000000000000000000000000000" > "$FIXTURE/releases/$BSHA/.venv-hash"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at3b.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -L "$FIXTURE/current" ] && grep -q "backend venv is not ready" /tmp/at3b.out; then
  pass "malformed release (venv not ready): exit 1, no current symlink created"
else
  fail "malformed release (venv not ready): expected exit 1, got exit=$RC"; cat /tmp/at3b.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 4: valid synthetic release, first activation ==="
new_fixture
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$ASHA" >/tmp/at4.out 2>&1
RC=$?
if [ "$RC" -eq 0 ] && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$ASHA" ] \
   && [ ! -f "$FIXTURE/shared/rollback-pointer" ]; then
  pass "first activation: exit 0, current -> $ASHA, no rollback pointer written"
else
  fail "first activation: expected exit 0 + current=$ASHA + no pointer, got exit=$RC"; cat /tmp/at4.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 5: previous-current capture on second activation ==="
add_release_b
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at5.out 2>&1
RC=$?
POINTER_OK=false
if [ -f "$FIXTURE/shared/rollback-pointer" ]; then
  grep -q "^previous_sha=$ASHA$" "$FIXTURE/shared/rollback-pointer" \
    && grep -q "^activated_sha=$BSHA$" "$FIXTURE/shared/rollback-pointer" \
    && POINTER_OK=true
fi
if [ "$RC" -eq 0 ] && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$BSHA" ] && [ "$POINTER_OK" = true ]; then
  pass "second activation: exit 0, current -> $BSHA, rollback pointer correctly captures previous=$ASHA activated=$BSHA"
else
  fail "second activation: expected correct pointer + current=$BSHA, got exit=$RC pointer_ok=$POINTER_OK"; cat /tmp/at5.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 6: symlink switching is atomic (current never missing/dangling mid-swap, points to exact release dir) ==="
if [ "$(readlink -f "$FIXTURE/current")" = "$(cd "$FIXTURE/releases/$BSHA" && pwd)" ]; then
  pass "symlink switching: current resolves to the exact release directory"
else
  fail "symlink switching: current does not resolve to the expected release directory"
fi
# No .tmp symlink artifacts left behind after a successful swap.
if ! find "$FIXTURE" -maxdepth 1 -name 'current.tmp.*' | grep -q .; then
  pass "symlink switching: no leftover current.tmp.* artifacts"
else
  fail "symlink switching: leftover current.tmp.* artifact found"
fi

# ---------------------------------------------------------------------------
echo "=== Test 7: repeated/idempotent invocation (activate BSHA again) ==="
BEFORE_POINTER_MTIME=$(stat -c %Y "$FIXTURE/shared/rollback-pointer" 2>/dev/null || stat -f %m "$FIXTURE/shared/rollback-pointer")
sleep 1.1
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at7.out 2>&1
RC=$?
AFTER_POINTER_MTIME=$(stat -c %Y "$FIXTURE/shared/rollback-pointer" 2>/dev/null || stat -f %m "$FIXTURE/shared/rollback-pointer")
if [ "$RC" -eq 0 ] && grep -q "already points to $BSHA -- nothing to do" /tmp/at7.out \
   && [ "$BEFORE_POINTER_MTIME" = "$AFTER_POINTER_MTIME" ]; then
  pass "idempotent re-activation: exit 0, no-op message, rollback pointer NOT rewritten"
else
  fail "idempotent re-activation: expected no-op + unchanged pointer, got exit=$RC before=$BEFORE_POINTER_MTIME after=$AFTER_POINTER_MTIME"
  cat /tmp/at7.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 8: invalid SHA inputs ==="
for bad_sha in "" "abc" "$(sha_n 1 | head -c 39)" "$(python3 -c 'print("G"*40)')" "$(python3 -c 'print("ABCDEF0123"*4)')"; do
  set +e
  PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$bad_sha" >/tmp/at8.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    pass "invalid SHA '$bad_sha': correctly rejected (exit 1)"
  else
    fail "invalid SHA '$bad_sha': expected exit 1, got exit=$RC"; cat /tmp/at8.out
  fi
done

# ---------------------------------------------------------------------------
echo "=== Test 9: path traversal attempts via --sha ==="
for traversal in "../../../etc/passwd" "$ASHA/../../../etc/passwd" "../$BSHA" "$ASHA/../$BSHA"; do
  set +e
  PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$traversal" >/tmp/at9.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ] && grep -q "must be a full 40-character" /tmp/at9.out; then
    pass "path traversal '$traversal': rejected by SHA format validation before any path use"
  else
    fail "path traversal '$traversal': expected rejection by format validation, got exit=$RC"; cat /tmp/at9.out
  fi
done

# ---------------------------------------------------------------------------
echo "=== Test 10: current target outside approved release root ==="
new_fixture
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$ASHA" >/dev/null 2>&1
# Corrupt 'current' to point somewhere outside releases/ entirely.
OUTSIDE=$(mktemp -d "$WORKDIR/outside.XXXXXX")
rm -f "$FIXTURE/current"
ln -s "$OUTSIDE" "$FIXTURE/current"
add_release_b
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at10.out 2>&1
RC=$?
set -e
POINTER_SANE=true
if [ -f "$FIXTURE/shared/rollback-pointer" ] && ! grep -qE '^previous_sha=[0-9a-f]{40}$' "$FIXTURE/shared/rollback-pointer"; then
  POINTER_SANE=false
fi
if [ "$RC" -eq 0 ] && grep -q "outside $FIXTURE/releases -- treating as no valid prior release" /tmp/at10.out \
   && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$BSHA" ] && [ "$POINTER_SANE" = true ]; then
  pass "current outside release root: detected, warned, activation still succeeds cleanly with no garbage rollback pointer"
else
  fail "current outside release root: expected detection + clean activation + no garbage pointer, got exit=$RC pointer_sane=$POINTER_SANE"
  cat /tmp/at10.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 11: migration gate - refuse without acknowledgment, succeed with it ==="
new_fixture
mkdir -p "$FIXTURE/releases/$ASHA/backend/app/database/migrations/versions"
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$ASHA" >/dev/null 2>&1
add_release_b
mkdir -p "$FIXTURE/releases/$BSHA/backend/app/database/migrations/versions"
echo "def upgrade(): pass" > "$FIXTURE/releases/$BSHA/backend/app/database/migrations/versions/0002_new.py"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/tmp/at11a.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 3 ] && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$ASHA" ]; then
  pass "migration gate: refused without acknowledgment (exit 3), current unchanged"
else
  fail "migration gate: expected exit 3 + current unchanged, got exit=$RC"; cat /tmp/at11a.out
fi
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" --acknowledge-migrations >/tmp/at11b.out 2>&1
RC=$?
if [ "$RC" -eq 0 ] && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$BSHA" ]; then
  pass "migration gate: succeeds with --acknowledge-migrations, current now $BSHA"
else
  fail "migration gate: expected exit 0 + current=$BSHA with acknowledgment, got exit=$RC"; cat /tmp/at11b.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 12: --dry-run makes zero filesystem changes ==="
new_fixture
add_release_b
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$ASHA" >/dev/null 2>&1
BEFORE_CHECKSUM=$(find "$FIXTURE" | sort | sha256sum)
set +e
PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" --dry-run >/tmp/at12.out 2>&1
RC=$?
set -e
AFTER_CHECKSUM=$(find "$FIXTURE" | sort | sha256sum)
if [ "$RC" -eq 0 ] && [ "$BEFORE_CHECKSUM" = "$AFTER_CHECKSUM" ] \
   && grep -q "DRY-RUN: would activate current -> " /tmp/at12.out \
   && grep -q "DRY-RUN: would record rollback pointer: previous=$ASHA activated=$BSHA" /tmp/at12.out; then
  pass "dry-run: exit 0, zero filesystem changes (identical directory-listing checksum), correct would-be plan printed"
else
  fail "dry-run: expected zero changes + correct plan, got exit=$RC checksum_match=$([ "$BEFORE_CHECKSUM" = "$AFTER_CHECKSUM" ] && echo yes || echo no)"
  cat /tmp/at12.out
fi
# current must still point at ASHA, unaffected by the dry-run.
if [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$ASHA" ]; then
  pass "dry-run: current still points at $ASHA (unaffected)"
else
  fail "dry-run: current was mutated by a --dry-run invocation"
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
