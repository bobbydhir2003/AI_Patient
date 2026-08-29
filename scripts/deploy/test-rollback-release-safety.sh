#!/usr/bin/env bash
#
# test-rollback-release-safety.sh - Validates rollback-release.sh against
# synthetic/temp fixtures only. Never references or mutates the real
# /opt/ptai/current. Requires Linux (GNU mv -T) and bash >= 4.4.
#
# Usage: test-rollback-release-safety.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLLBACK="$SCRIPT_DIR/rollback-release.sh"
ACTIVATE="$SCRIPT_DIR/activate-release.sh"
[ -x "$ROLLBACK" ] || { echo "ERROR: $ROLLBACK not found or not executable" >&2; exit 1; }
[ -x "$ACTIVATE" ] || { echo "ERROR: $ACTIVATE not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-rollback-test.XXXXXX)
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

sha_n() { python3 -c "print(str($1) * 40)"; }
ASHA=$(sha_n 1)
BSHA=$(sha_n 2)
CSHA=$(sha_n 3)

# Fixture with three fully-ready releases (A, B, C) sharing one venv hash,
# with A activated first, then B (so current=B, rollback pointer says
# previous=A). C exists but was never activated - it must NEVER be picked by
# rollback, proving pointer-based (not directory-order) selection.
new_fixture_with_history() {
  FIXTURE=$(mktemp -d "$WORKDIR/fixture.XXXXXX")
  mkdir -p "$FIXTURE"/{releases,venvs,shared}
  for sha in "$ASHA" "$BSHA" "$CSHA"; do
    mkdir -p "$FIXTURE/releases/$sha/backend"
    echo "fastapi==0.100.0" > "$FIXTURE/releases/$sha/backend/requirements.lock.txt"
    HASH=$(sha256sum "$FIXTURE/releases/$sha/backend/requirements.lock.txt" | awk '{print $1}')
    echo "$HASH" > "$FIXTURE/releases/$sha/.venv-hash"
    touch "$FIXTURE/releases/$sha/.ready"
    sleep 1.1  # distinct mtimes, oldest to newest: A, B, C
  done
  mkdir -p "$FIXTURE/venvs/$HASH/backend" "$FIXTURE/venvs/$HASH/livekit"
  touch "$FIXTURE/venvs/$HASH/backend/.ready" "$FIXTURE/venvs/$HASH/livekit/.ready"
  PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$ASHA" >/dev/null 2>&1
  PTAI_BASE_DIR="$FIXTURE" "$ACTIVATE" --sha "$BSHA" >/dev/null 2>&1
  # C, the NEWEST by mtime, is never activated - if rollback ever picked
  # "second newest directory" instead of the recorded pointer, it would
  # wrongly target C or B instead of A.
}

# ---------------------------------------------------------------------------
echo "=== Test 1: valid previous release - rollback restores exactly the recorded pointer, not the newest/2nd-newest directory ==="
new_fixture_with_history
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb1.out 2>&1
RC=$?
if [ "$RC" -eq 0 ] && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$ASHA" ]; then
  pass "valid previous release: rollback correctly restored current -> $ASHA (the recorded previous_sha), not C (newest) or any mtime-based guess"
else
  fail "valid previous release: expected current -> $ASHA, got exit=$RC"; cat /tmp/rb1.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: missing rollback pointer entirely ==="
FIXTURE=$(mktemp -d "$WORKDIR/fixture.XXXXXX")
mkdir -p "$FIXTURE"/{releases,venvs,shared}
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb2.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "no rollback pointer found" /tmp/rb2.out; then
  pass "missing pointer: exit 1, clear diagnostic"
else
  fail "missing pointer: expected exit 1, got exit=$RC"; cat /tmp/rb2.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 3: previous target release missing .ready ==="
new_fixture_with_history
rm -f "$FIXTURE/releases/$ASHA/.ready"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb3.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "rollback target release is not ready" /tmp/rb3.out \
   && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$BSHA" ]; then
  pass "previous target not ready: exit 1, current unchanged (still $BSHA)"
else
  fail "previous target not ready: expected exit 1 + current unchanged, got exit=$RC"; cat /tmp/rb3.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 4: malformed rollback metadata (no previous_sha key) ==="
new_fixture_with_history
echo "activated_sha=$BSHA" > "$FIXTURE/shared/rollback-pointer"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb4a.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "malformed" /tmp/rb4a.out; then
  pass "malformed metadata (no previous_sha): exit 1, clear diagnostic"
else
  fail "malformed metadata (no previous_sha): expected exit 1, got exit=$RC"; cat /tmp/rb4a.out
fi

echo "=== Test 4b: malformed rollback metadata (previous_sha not valid hex) ==="
new_fixture_with_history
echo "previous_sha=not-a-real-sha" > "$FIXTURE/shared/rollback-pointer"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb4b.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "not a valid 40-char SHA" /tmp/rb4b.out; then
  pass "malformed metadata (invalid hex): exit 1, clear diagnostic"
else
  fail "malformed metadata (invalid hex): expected exit 1, got exit=$RC"; cat /tmp/rb4b.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 5: current target outside approved releases root ==="
new_fixture_with_history
OUTSIDE=$(mktemp -d "$WORKDIR/outside.XXXXXX")
rm -f "$FIXTURE/current"
ln -s "$OUTSIDE" "$FIXTURE/current"
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb5.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "outside $FIXTURE/releases" /tmp/rb5.out; then
  pass "current outside releases root: refused (exit 1), clear diagnostic"
else
  fail "current outside releases root: expected refusal, got exit=$RC"; cat /tmp/rb5.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 6: repeated rollback is symmetric (rollback, then rollback again restores the other side) ==="
new_fixture_with_history
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb6a.out 2>&1
FIRST_RC=$?
FIRST_CURRENT=$(basename "$(readlink -f "$FIXTURE/current")")
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb6b.out 2>&1
SECOND_RC=$?
SECOND_CURRENT=$(basename "$(readlink -f "$FIXTURE/current")")
if [ "$FIRST_RC" -eq 0 ] && [ "$FIRST_CURRENT" = "$ASHA" ] \
   && [ "$SECOND_RC" -eq 0 ] && [ "$SECOND_CURRENT" = "$BSHA" ]; then
  pass "repeated rollback: symmetric - first restores $ASHA, second restores $BSHA (rollback of the rollback)"
else
  fail "repeated rollback: expected A then B, got $FIRST_CURRENT then $SECOND_CURRENT"
fi

# ---------------------------------------------------------------------------
echo "=== Test 7: current already equals the recorded rollback target (idempotent no-op) ==="
new_fixture_with_history
# Pointer says previous_sha=A (from the B activation in new_fixture_with_history).
# Manually point 'current' at A WITHOUT going through rollback-release.sh, so
# the pointer's recorded target and the live 'current' already agree.
rm -f "$FIXTURE/current"
ln -s "$FIXTURE/releases/$ASHA" "$FIXTURE/current"
BEFORE_POINTER=$(cat "$FIXTURE/shared/rollback-pointer")
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --confirm >/tmp/rb7.out 2>&1
RC=$?
set -e
AFTER_POINTER=$(cat "$FIXTURE/shared/rollback-pointer")
if [ "$RC" -eq 0 ] && grep -q "already points to $ASHA -- nothing to do" /tmp/rb7.out \
   && [ "$BEFORE_POINTER" = "$AFTER_POINTER" ]; then
  pass "current already equals recorded target: exit 0 no-op, pointer left untouched"
else
  fail "current-equals-target case: expected no-op + unchanged pointer, got exit=$RC"; cat /tmp/rb7.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 8: --confirm required (refuses bare invocation) ==="
new_fixture_with_history
set +e
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" >/tmp/rb8.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "refusing to run without --confirm" /tmp/rb8.out \
   && [ "$(basename "$(readlink -f "$FIXTURE/current")")" = "$BSHA" ]; then
  pass "no --confirm: refused, current unchanged"
else
  fail "no --confirm: expected refusal + unchanged current, got exit=$RC"; cat /tmp/rb8.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 9: --dry-run makes zero filesystem changes and requires no --confirm ==="
new_fixture_with_history
BEFORE_CHECKSUM=$(find "$FIXTURE" | sort | sha256sum)
PTAI_BASE_DIR="$FIXTURE" "$ROLLBACK" --dry-run >/tmp/rb9.out 2>&1
RC=$?
AFTER_CHECKSUM=$(find "$FIXTURE" | sort | sha256sum)
if [ "$RC" -eq 0 ] && [ "$BEFORE_CHECKSUM" = "$AFTER_CHECKSUM" ] \
   && grep -q "DRY-RUN: would roll back current -> " /tmp/rb9.out; then
  pass "dry-run: exit 0, zero filesystem changes, correct plan printed, no --confirm required"
else
  fail "dry-run: expected zero changes + correct plan, got exit=$RC"; cat /tmp/rb9.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 10: invalid/traversal input is rejected (no recognized arguments accept a path) ==="
set +e
PTAI_BASE_DIR="$WORKDIR" "$ROLLBACK" --confirm --sha "../../../etc/passwd" >/tmp/rb10.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "Unknown argument" /tmp/rb10.out; then
  pass "invalid/unexpected argument: rejected outright (rollback-release.sh takes no path input at all, eliminating this attack surface by design)"
else
  fail "invalid argument handling: expected rejection, got exit=$RC"; cat /tmp/rb10.out
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
