#!/usr/bin/env bash
#
# test-prepare-release-safety.sh - Regression tests for the Phase 3B-3
# incident: prepare-release.sh must NEVER delete, rebuild, or rename an
# existing venv path just because it lacks a .ready marker. It must first
# prove the path is unreferenced by anything real (a release's .venv-hash,
# 'current', the rollback pointer, a systemd unit's ExecStart, or a running
# process's cmdline).
#
# Runs entirely inside a throwaway temp directory (PTAI_BASE_DIR override) -
# never touches /opt/ptai or any real system state. Requires Linux (GNU mv
# -T, /proc) and bash >= 4.4, matching the scripts under test; this is
# expected to run on the target EC2 instance or Linux CI, not macOS.
#
# Usage: test-prepare-release-safety.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREPARE="$SCRIPT_DIR/prepare-release.sh"
[ -x "$PREPARE" ] || { echo "ERROR: $PREPARE not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-prepare-release-test.XXXXXX)
FAKEPATH_DIR="$WORKDIR/fakebin"
mkdir -p "$FAKEPATH_DIR"
BG_PIDS=()

cleanup() {
  local pid
  for pid in "${BG_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true
  done
  rm -rf -- "$WORKDIR"
}
trap cleanup EXIT

PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

sha_n() { python3 -c "print(str($1) * 40)"; }

ASHA=$(sha_n 1)  # pre-existing, ready release that protects venv hash H
BSHA=$(sha_n 2)  # the NEW, not-yet-existing release every test targets

# Source dir whose requirements.lock.txt hashes to the SAME venv hash H that
# release A already protects - this reproduces the incident precisely: a
# brand new release resolves to a venv hash that already exists on disk
# (built for a different release) but lacks .ready. Venv content identity is
# keyed on the LOCK file (dependency-locking checkpoint), not
# requirements.txt, so that's what these fixtures hash. Content doesn't need
# real --require-hashes-valid entries: every scenario except Test 6 is
# refused (or reused) before ever reaching a real `uv pip install`, and
# Test 6 is expected to fail at that step anyway in this offline test
# environment - only the safety-gate decision before it is under test.
SRC="$WORKDIR/fake-source"
mkdir -p "$SRC/backend" "$SRC/dist"
echo "fastapi>=0.100,<1.0" > "$SRC/backend/requirements.txt"
echo "fastapi==0.100.0" > "$SRC/backend/requirements.lock.txt"
# Clearly-fake test values only - never a real secret.
echo "DATABASE_URL=postgresql://test-fixture-only:not-a-real-secret@localhost/test" > "$SRC/backend/.env"
echo "OPENAI_API_KEY=" > "$SRC/backend/.env.production"
echo "# example template, no real values" > "$SRC/backend/.env.example"

# A second source dir with different lock content -> different venv hash,
# used only by the "genuinely unreferenced" test.
SRC_OTHER="$WORKDIR/fake-source-other"
mkdir -p "$SRC_OTHER/backend" "$SRC_OTHER/dist"
echo "sqlalchemy>=0.200,<1.0" > "$SRC_OTHER/backend/requirements.txt"
echo "sqlalchemy==0.200.0" > "$SRC_OTHER/backend/requirements.lock.txt"

# Build a fresh isolated fixture: release A already exists, is .ready, and
# declares venv hash H via .venv-hash. Venv H's directories exist on disk
# but WITHOUT .ready - exactly the state the manually-repaired Phase 3B-2
# venv was in relative to this script's later-introduced convention.
new_fixture() {
  FIXTURE=$(mktemp -d "$WORKDIR/fixture.XXXXXX")
  mkdir -p "$FIXTURE"/{releases,venvs,shared}
  mkdir -p "$FIXTURE/releases/$ASHA/backend"
  cp "$SRC/backend/requirements.txt" "$FIXTURE/releases/$ASHA/backend/requirements.txt"
  cp "$SRC/backend/requirements.lock.txt" "$FIXTURE/releases/$ASHA/backend/requirements.lock.txt"
  mkdir -p "$FIXTURE/releases/$ASHA/dist"
  HASH=$(sha256sum "$FIXTURE/releases/$ASHA/backend/requirements.lock.txt" | awk '{print $1}')
  echo "$HASH" > "$FIXTURE/releases/$ASHA/.venv-hash"
  touch "$FIXTURE/releases/$ASHA/.ready"
  VENV_ROOT="$FIXTURE/venvs/$HASH"
  mkdir -p "$VENV_ROOT/backend/bin" "$VENV_ROOT/livekit/bin"
}

checksum_dir() {
  find "$1" -type f -exec sha256sum {} \; 2>/dev/null | sort | sha256sum | awk '{print $1}'
}

# ---------------------------------------------------------------------------
echo "=== Test 1: new release resolves to a venv hash already owned by another release + 'current', WITHOUT .ready ==="
echo "    (this is the exact Phase 3B-3 incident scenario)"
new_fixture
ln -s "$FIXTURE/releases/$ASHA" "$FIXTURE/current"
BEFORE=$(checksum_dir "$VENV_ROOT")
set +e
PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC" >/tmp/t1.out 2>&1
RC=$?
set -e
AFTER=$(checksum_dir "$VENV_ROOT")
if [ "$RC" -eq 4 ] && [ "$BEFORE" = "$AFTER" ]; then
  pass "incident scenario: refused (exit 4), zero mutation to venv contents"
else
  fail "incident scenario: expected exit 4 + zero mutation, got exit=$RC before=$BEFORE after=$AFTER"
  cat /tmp/t1.out
fi
if grep -q "ACTIVE 'current' release" /tmp/t1.out; then
  pass "incident scenario: diagnostic correctly names 'current' as the reference"
else
  fail "incident scenario: diagnostic did not mention 'current' as the reference"
  cat /tmp/t1.out
fi

# The rsync + post-copy secret check both run BEFORE the venv-hash safety
# gate that refused this activation above, so $FIXTURE/releases/$BSHA is
# already populated - a good, free opportunity to verify SRC's .env/.env.*
# never made it into the copied release while .env.example was preserved.
if [ ! -e "$FIXTURE/releases/$BSHA/backend/.env" ] && [ ! -e "$FIXTURE/releases/$BSHA/backend/.env.production" ]; then
  pass "secret exclusion: .env and .env.production were NOT copied into the prepared release"
else
  fail "secret exclusion: a real secrets file was copied into the prepared release"
fi
if [ -f "$FIXTURE/releases/$BSHA/backend/.env.example" ]; then
  pass "secret exclusion: .env.example (template, no real values) was correctly preserved"
else
  fail "secret exclusion: .env.example was unexpectedly excluded too"
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: target hash referenced only as the rollback target, WITHOUT .ready ==="
new_fixture
{
  echo "previous_sha=$ASHA"
  echo "activated_sha=$BSHA"
} > "$FIXTURE/shared/rollback-pointer"
BEFORE=$(checksum_dir "$VENV_ROOT")
set +e
PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC" >/tmp/t2.out 2>&1
RC=$?
set -e
AFTER=$(checksum_dir "$VENV_ROOT")
if [ "$RC" -eq 4 ] && [ "$BEFORE" = "$AFTER" ] && grep -q "ROLLBACK target" /tmp/t2.out; then
  pass "rollback-target reference: refused (exit 4), zero mutation, correctly diagnosed"
else
  fail "rollback-target reference: expected exit 4 + zero mutation + correct diagnosis, got exit=$RC"
  cat /tmp/t2.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 3: target hash referenced by a running process's cmdline, WITHOUT .ready ==="
echo "    (no systemd, no current, no rollback pointer -- process is the ONLY signal)"
new_fixture
cat > "$VENV_ROOT/backend/bin/python" <<'EOF'
#!/bin/sh
sleep 300
EOF
chmod +x "$VENV_ROOT/backend/bin/python"
"$VENV_ROOT/backend/bin/python" &
BGPID=$!
BG_PIDS+=("$BGPID")
sleep 0.3  # let the kernel populate /proc/<pid>/cmdline
BEFORE=$(checksum_dir "$VENV_ROOT")
set +e
PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC" >/tmp/t3.out 2>&1
RC=$?
set -e
AFTER=$(checksum_dir "$VENV_ROOT")
kill "$BGPID" >/dev/null 2>&1 || true
if [ "$RC" -eq 4 ] && [ "$BEFORE" = "$AFTER" ] && grep -q "running process PID $BGPID" /tmp/t3.out; then
  pass "running-process reference: refused (exit 4), zero mutation, correctly named PID $BGPID"
else
  fail "running-process reference: expected exit 4 + zero mutation + PID $BGPID named, got exit=$RC"
  cat /tmp/t3.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 4: target hash referenced by a systemd unit's ExecStart, WITHOUT .ready ==="
new_fixture
cat > "$FAKEPATH_DIR/systemctl" <<EOF
#!/bin/sh
if [ "\$1" = "show" ] && [ "\$2" = "ptai.service" ]; then
  echo "ExecStart={ path=$VENV_ROOT/backend/bin/uvicorn ; argv[]=$VENV_ROOT/backend/bin/uvicorn app.main:app }"
  exit 0
fi
if [ "\$1" = "show" ]; then
  echo "ExecStart="
  exit 0
fi
exit 1
EOF
chmod +x "$FAKEPATH_DIR/systemctl"
BEFORE=$(checksum_dir "$VENV_ROOT")
set +e
PATH="$FAKEPATH_DIR:$PATH" PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC" >/tmp/t4.out 2>&1
RC=$?
set -e
AFTER=$(checksum_dir "$VENV_ROOT")
if [ "$RC" -eq 4 ] && [ "$BEFORE" = "$AFTER" ] && grep -q "systemd unit ptai.service ExecStart" /tmp/t4.out; then
  pass "systemd ExecStart reference: refused (exit 4), zero mutation, correctly diagnosed"
else
  fail "systemd ExecStart reference: expected exit 4 + zero mutation + correct diagnosis, got exit=$RC"
  cat /tmp/t4.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 5: target hash already has a valid .ready venv -- must be reused, not touched ==="
new_fixture
touch "$VENV_ROOT/backend/.ready" "$VENV_ROOT/livekit/.ready"
echo "marker" > "$VENV_ROOT/backend/bin/uvicorn"  # would fail exec checks if rebuilt
BEFORE=$(checksum_dir "$VENV_ROOT")
set +e
PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC" >/tmp/t5.out 2>&1
RC=$?
set -e
AFTER=$(checksum_dir "$VENV_ROOT")
if [ "$RC" -eq 0 ] && [ "$BEFORE" = "$AFTER" ]; then
  pass "ready venv: reused unchanged, exit 0"
else
  fail "ready venv: expected exit 0 + zero mutation, got exit=$RC before=$BEFORE after=$AFTER"
  cat /tmp/t5.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 6: genuinely unreferenced venv, no .ready -- must be preserved via rename, never rm -rf ==="
new_fixture
# A second, distinct hash that release A does NOT reference and nothing else
# points to either - the only genuinely-safe-to-touch case.
OTHERHASH=$(sha256sum "$SRC_OTHER/backend/requirements.lock.txt" | awk '{print $1}')
OTHER_VENV_ROOT="$FIXTURE/venvs/$OTHERHASH"
mkdir -p "$OTHER_VENV_ROOT/backend/bin" "$OTHER_VENV_ROOT/livekit/bin"
echo "stale-marker" > "$OTHER_VENV_ROOT/backend/canary-file"
set +e
PTAI_BASE_DIR="$FIXTURE" timeout 5 "$PREPARE" --sha "$BSHA" --source-dir "$SRC_OTHER" >/tmp/t6.out 2>&1
RC=$?
set -e
# Expected to proceed past the safety gate (no reference found) and then
# fail later at the real `uv venv`/`uv pip install` step in this offline
# test environment - that's fine, this test only cares about the
# safety-gate's decision, not the build itself. What matters: the ORIGINAL
# directory must have been preserved via rename, canary file intact, never
# deleted.
FOUND_BACKUP=$(find "$FIXTURE/venvs" -maxdepth 2 -name "backend.stale-*" 2>/dev/null | head -1)
if [ -n "$FOUND_BACKUP" ] && [ -f "$FOUND_BACKUP/canary-file" ]; then
  pass "unreferenced no-.ready venv: preserved via rename to $FOUND_BACKUP, canary file intact"
else
  fail "unreferenced no-.ready venv: expected a backend.stale-* rename with canary-file preserved, found: ${FOUND_BACKUP:-none}"
  cat /tmp/t6.out
fi
if [ ! -e "$OTHER_VENV_ROOT/backend/canary-file" ]; then
  pass "unreferenced no-.ready venv: original path no longer has stale content (renamed away, not merged/overwritten)"
else
  fail "unreferenced no-.ready venv: canary-file still present at original path -- rename did not occur as expected"
fi

# ---------------------------------------------------------------------------
echo "=== Test 7: idempotent repeated execution (already-ready release) ==="
new_fixture
touch "$VENV_ROOT/backend/.ready" "$VENV_ROOT/livekit/.ready"
set +e
OUT1=$(PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$ASHA" --source-dir "$SRC" 2>&1)
RC1=$?
OUT2=$(PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$ASHA" --source-dir "$SRC" 2>&1)
RC2=$?
set -e
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && [ "$OUT1" = "$OUT2" ]; then
  pass "idempotent repeated execution: identical exit 0 + identical output both times"
else
  fail "idempotent repeated execution: rc1=$RC1 rc2=$RC2 output-matched=$([ "$OUT1" = "$OUT2" ] && echo yes || echo no)"
fi

# ---------------------------------------------------------------------------
echo "=== Test 8: missing requirements.lock.txt in source-dir must fail closed, never fall back ==="
new_fixture
SRC_NO_LOCK="$WORKDIR/fake-source-no-lock"
mkdir -p "$SRC_NO_LOCK/backend" "$SRC_NO_LOCK/dist"
echo "fastapi>=0.100,<1.0" > "$SRC_NO_LOCK/backend/requirements.txt"
# deliberately no requirements.lock.txt
set +e
PTAI_BASE_DIR="$FIXTURE" "$PREPARE" --sha "$BSHA" --source-dir "$SRC_NO_LOCK" >/tmp/t8.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -e "$FIXTURE/releases/$BSHA" ] && grep -q "missing backend/requirements.lock.txt" /tmp/t8.out; then
  pass "missing lock file: refused (exit 1), no release directory created, clear diagnostic"
else
  fail "missing lock file: expected exit 1 + no release dir + clear diagnostic, got exit=$RC"
  cat /tmp/t8.out
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
