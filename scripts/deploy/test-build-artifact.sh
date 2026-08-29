#!/usr/bin/env bash
#
# test-build-artifact.sh - Validates build-artifact.sh against a synthetic
# throwaway git repo. Never touches the real repository. Requires bash
# >= 4.4, git, tar, rsync.
#
# Usage: test-build-artifact.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$SCRIPT_DIR/build-artifact.sh"
[ -x "$BUILD" ] || { echo "ERROR: $BUILD not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-build-artifact-test.XXXXXX)
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

# Build a minimal, valid synthetic repo checked out to a real commit.
new_repo() {
  REPO=$(mktemp -d "$WORKDIR/repo.XXXXXX")
  git -C "$REPO" init -q
  git -C "$REPO" config user.email test@example.com
  git -C "$REPO" config user.name "Test"
  mkdir -p "$REPO/backend/app" "$REPO/dist" "$REPO/scripts/deploy"
  echo "fastapi==0.100.0" > "$REPO/backend/requirements.lock.txt"
  echo "print('app')" > "$REPO/backend/app/main.py"
  echo "<html></html>" > "$REPO/dist/index.html"
  echo "#!/bin/sh" > "$REPO/scripts/deploy/example.sh"
  git -C "$REPO" add -A >/dev/null
  git -C "$REPO" commit -q -m "initial"
  SHA=$(git -C "$REPO" rev-parse HEAD)
}

# ---------------------------------------------------------------------------
echo "=== Test 1: valid build succeeds, artifact + SHA-256 produced ==="
new_repo
OUT="$WORKDIR/out1.tar.gz"
RESULT=$("$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$OUT" 2>/tmp/ba1.err)
RC=$?
if [ "$RC" -eq 0 ] && [ -f "$OUT" ] && echo "$RESULT" | grep -q "^artifact_sha256="; then
  pass "valid build: exit 0, artifact file created, SHA-256 printed"
else
  fail "valid build: expected exit 0 + artifact + sha256, got exit=$RC"; cat /tmp/ba1.err
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: --sha mismatch with actual HEAD is rejected ==="
new_repo
WRONG_SHA=$(python3 -c "print('a'*40)")
set +e
"$BUILD" --sha "$WRONG_SHA" --repo-path "$REPO" --output "$WORKDIR/out2.tar.gz" >/tmp/ba2.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -f "$WORKDIR/out2.tar.gz" ] && grep -q "not the requested" /tmp/ba2.out; then
  pass "SHA mismatch: rejected (exit 1), no artifact created"
else
  fail "SHA mismatch: expected exit 1 + no artifact, got exit=$RC"; cat /tmp/ba2.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 3: missing requirements.lock.txt is rejected ==="
new_repo
rm -f "$REPO/backend/requirements.lock.txt"
git -C "$REPO" add -A >/dev/null
git -C "$REPO" commit -q -m "remove lock"
SHA2=$(git -C "$REPO" rev-parse HEAD)
set +e
"$BUILD" --sha "$SHA2" --repo-path "$REPO" --output "$WORKDIR/out3.tar.gz" >/tmp/ba3.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "requirements.lock.txt not found" /tmp/ba3.out; then
  pass "missing lock file: rejected (exit 1), clear diagnostic"
else
  fail "missing lock file: expected exit 1, got exit=$RC"; cat /tmp/ba3.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 4: missing dist/ is rejected ==="
new_repo
rm -rf "$REPO/dist"
set +e
"$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$WORKDIR/out4.tar.gz" >/tmp/ba4.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "dist/ not found" /tmp/ba4.out; then
  pass "missing dist/: rejected (exit 1), clear diagnostic"
else
  fail "missing dist/: expected exit 1, got exit=$RC"; cat /tmp/ba4.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 5: secret files excluded, .env.example preserved ==="
new_repo
echo "DATABASE_URL=postgresql://test:test@localhost/test" > "$REPO/backend/.env"
echo "OPENAI_API_KEY=fake" > "$REPO/backend/.env.production"
echo "# template only" > "$REPO/backend/.env.example"
git -C "$REPO" add -A >/dev/null
# .env/.env.production are real files on disk (untracked is fine - build-artifact.sh reads the working tree, not just tracked files)
OUT5="$WORKDIR/out5.tar.gz"
"$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$OUT5" >/tmp/ba5.out 2>&1
RC=$?
TAR_LISTING=$(tar -tzf "$OUT5" 2>/dev/null || true)
if [ "$RC" -eq 0 ] && ! echo "$TAR_LISTING" | grep -qE '\.env$|\.env\.production$' \
   && echo "$TAR_LISTING" | grep -q '\.env\.example$'; then
  pass "secret exclusion: .env/.env.production absent from artifact, .env.example present"
else
  fail "secret exclusion: unexpected artifact contents"; echo "$TAR_LISTING"; cat /tmp/ba5.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 6: symlink in source tree is rejected ==="
new_repo
ln -s /etc/passwd "$REPO/backend/app/evil-symlink"
set +e
"$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$WORKDIR/out6.tar.gz" >/tmp/ba6.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && [ ! -f "$WORKDIR/out6.tar.gz" ] && grep -q "symlink" /tmp/ba6.out; then
  pass "symlink in source: rejected (exit 1), no artifact created"
else
  fail "symlink in source: expected exit 1 + no artifact, got exit=$RC"; cat /tmp/ba6.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 7: two builds of identical source produce byte-identical artifacts (determinism) ==="
new_repo
OUT7A="$WORKDIR/out7a.tar.gz"
OUT7B="$WORKDIR/out7b.tar.gz"
"$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$OUT7A" >/dev/null 2>&1
sleep 1.1  # ensure real wall-clock time passes between builds
"$BUILD" --sha "$SHA" --repo-path "$REPO" --output "$OUT7B" >/dev/null 2>&1
SHA_A=$(sha256sum "$OUT7A" | awk '{print $1}')
SHA_B=$(sha256sum "$OUT7B" | awk '{print $1}')
if [ "$SHA_A" = "$SHA_B" ]; then
  pass "determinism: two builds of the same source produced byte-identical artifacts ($SHA_A)"
else
  fail "determinism: expected identical hashes, got $SHA_A vs $SHA_B"
fi

# ---------------------------------------------------------------------------
echo "=== Test 8: invalid SHA format rejected ==="
new_repo
for bad in "abc" "" "$(python3 -c 'print("g"*40)')"; do
  set +e
  "$BUILD" --sha "$bad" --repo-path "$REPO" --output "$WORKDIR/out8.tar.gz" >/tmp/ba8.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    pass "invalid SHA '$bad': rejected (exit 1)"
  else
    fail "invalid SHA '$bad': expected exit 1, got exit=$RC"; cat /tmp/ba8.out
  fi
done

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
