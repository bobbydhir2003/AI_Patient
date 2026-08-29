#!/usr/bin/env bash
#
# test-cleanup-releases-safety.sh - Validates cleanup-releases.sh against
# synthetic/temp fixtures only. Never touches the real /opt/ptai/releases.
# Requires Linux (GNU find -printf) and bash >= 4.4.
#
# Usage: test-cleanup-releases-safety.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP="$SCRIPT_DIR/cleanup-releases.sh"
[ -x "$CLEANUP" ] || { echo "ERROR: $CLEANUP not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-cleanup-test.XXXXXX)
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

sha_n() { python3 -c "print(str($1) * 40)"; }

# ---------------------------------------------------------------------------
echo "=== Test 1: dry-run protects current + rollback target even when they're the oldest ==="
FIXTURE=$(mktemp -d "$WORKDIR/fixture.XXXXXX")
mkdir -p "$FIXTURE"/{releases,venvs,shared}
for i in 1 2 3 4 5 6; do
  sha=$(sha_n "$i")
  mkdir -p "$FIXTURE/releases/$sha"
  touch "$FIXTURE/releases/$sha/.ready"
  sleep 1.1
done
SHA1=$(sha_n 1); SHA2=$(sha_n 2); SHA6=$(sha_n 6)
ln -s "$FIXTURE/releases/$SHA6" "$FIXTURE/current"
{ echo "previous_sha=$SHA2"; echo "activated_sha=$SHA6"; } > "$FIXTURE/shared/rollback-pointer"

BEFORE_CHECKSUM=$(find "$FIXTURE/releases" | sort | sha256sum)
OUT=$(PTAI_BASE_DIR="$FIXTURE" "$CLEANUP" --keep 3 2>&1)
RC=$?
AFTER_CHECKSUM=$(find "$FIXTURE/releases" | sort | sha256sum)
if [ "$RC" -eq 0 ] && [ "$BEFORE_CHECKSUM" = "$AFTER_CHECKSUM" ] \
   && echo "$OUT" | grep -q "$SHA2 (rollback target)" \
   && echo "$OUT" | grep -qE "^\[cleanup-releases\]   - $(sha_n 1)$"; then
  pass "dry-run: zero deletions occurred, rollback target ($SHA2) explicitly protected, oldest unprotected release ($SHA1) correctly identified as a candidate"
else
  fail "dry-run: expected zero deletions + correct protection reporting, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: --execute deletes only the unprotected candidates, never current/rollback-target ==="
OUT=$(PTAI_BASE_DIR="$FIXTURE" "$CLEANUP" --keep 3 --execute 2>&1)
RC=$?
REMAINING=$(find "$FIXTURE/releases" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
EXPECTED=$(printf '%s\n%s\n%s\n%s\n' "$SHA2" "$(sha_n 4)" "$(sha_n 5)" "$SHA6" | sort)
if [ "$RC" -eq 0 ] && [ "$REMAINING" = "$EXPECTED" ]; then
  pass "execute: exactly the expected releases remain (current, rollback target, and 3 most recent), unprotected old releases removed"
else
  fail "execute: unexpected remaining set"; echo "remaining: $REMAINING"; echo "expected: $EXPECTED"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Test 3: current protected even when it is the single oldest release ==="
FIXTURE2=$(mktemp -d "$WORKDIR/fixture2.XXXXXX")
mkdir -p "$FIXTURE2"/{releases,venvs,shared}
for i in 1 2 3 4; do
  sha=$(sha_n "$i")
  mkdir -p "$FIXTURE2/releases/$sha"
  touch "$FIXTURE2/releases/$sha/.ready"
  sleep 1.1
done
ln -s "$FIXTURE2/releases/$(sha_n 1)" "$FIXTURE2/current"
OUT=$(PTAI_BASE_DIR="$FIXTURE2" "$CLEANUP" --keep 1 --execute 2>&1)
RC=$?
REMAINING=$(find "$FIXTURE2/releases" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
EXPECTED=$(printf '%s\n%s\n' "$(sha_n 1)" "$(sha_n 4)" | sort)
if [ "$RC" -eq 0 ] && [ "$REMAINING" = "$EXPECTED" ]; then
  pass "oldest-is-current: protected despite being oldest, only unprotected middle releases removed"
else
  fail "oldest-is-current: expected current preserved, got remaining=$REMAINING"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Test 4: never touches venvs/, shared/, or current itself ==="
FIXTURE3=$(mktemp -d "$WORKDIR/fixture3.XXXXXX")
mkdir -p "$FIXTURE3"/{releases,venvs/somehash/backend,venvs/somehash/livekit,shared/systemd-backups}
touch "$FIXTURE3/venvs/somehash/backend/.ready" "$FIXTURE3/venvs/somehash/livekit/.ready"
touch "$FIXTURE3/shared/backend.env"
echo "unit-backup" > "$FIXTURE3/shared/systemd-backups/ptai.service"
for i in 1 2 3; do
  sha=$(sha_n "$i")
  mkdir -p "$FIXTURE3/releases/$sha"
  touch "$FIXTURE3/releases/$sha/.ready"
  sleep 1.1
done
ln -s "$FIXTURE3/releases/$(sha_n 3)" "$FIXTURE3/current"
BEFORE_VENV_CHECKSUM=$(find "$FIXTURE3/venvs" "$FIXTURE3/shared" | sort | sha256sum)
PTAI_BASE_DIR="$FIXTURE3" "$CLEANUP" --keep 1 --execute >/dev/null 2>&1
AFTER_VENV_CHECKSUM=$(find "$FIXTURE3/venvs" "$FIXTURE3/shared" | sort | sha256sum)
if [ "$BEFORE_VENV_CHECKSUM" = "$AFTER_VENV_CHECKSUM" ] && [ -L "$FIXTURE3/current" ]; then
  pass "scope containment: venvs/, shared/ (including systemd backups + backend.env), and current symlink itself all untouched"
else
  fail "scope containment: something outside releases/ was modified"
fi

# ---------------------------------------------------------------------------
echo "=== Test 5: bad --keep values rejected ==="
for bad in "0" "-1" "abc" ""; do
  set +e
  PTAI_BASE_DIR="$FIXTURE3" "$CLEANUP" --keep "$bad" >/tmp/cl5.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    pass "bad --keep '$bad': rejected (exit 1)"
  else
    fail "bad --keep '$bad': expected exit 1, got exit=$RC"; cat /tmp/cl5.out
  fi
done

# ---------------------------------------------------------------------------
echo "=== Test 6: total releases at or below --keep -- nothing pruned ==="
FIXTURE4=$(mktemp -d "$WORKDIR/fixture4.XXXXXX")
mkdir -p "$FIXTURE4/releases"
for i in 1 2; do
  sha=$(sha_n "$i")
  mkdir -p "$FIXTURE4/releases/$sha"
  touch "$FIXTURE4/releases/$sha/.ready"
done
OUT=$(PTAI_BASE_DIR="$FIXTURE4" "$CLEANUP" --keep 5 --execute 2>&1)
RC=$?
REMAINING=$(find "$FIXTURE4/releases" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
if [ "$RC" -eq 0 ] && [ "$REMAINING" -eq 2 ] && echo "$OUT" | grep -q "Nothing to prune"; then
  pass "total <= keep: nothing pruned, clear message"
else
  fail "total <= keep: expected no-op, got exit=$RC remaining=$REMAINING"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
