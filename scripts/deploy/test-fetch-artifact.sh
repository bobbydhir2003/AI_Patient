#!/usr/bin/env bash
#
# test-fetch-artifact.sh - Validates fetch-artifact.sh against synthetic
# archives. Uses a fake `aws` shim (PATH-shadowed ahead of the real binary)
# that serves pre-placed local fixture files in place of a real S3
# GetObject call, so every archive-safety scenario can be tested quickly
# and deterministically without a real S3 round-trip per case - the real
# S3 upload/download/checksum mechanism itself was already proven end to
# end in the Phase 3B-4 transport validation checkpoint. Requires Linux
# (GNU tar) and bash >= 4.4 - run this on the target EC2 instance or Linux
# CI, not macOS (bsdtar produces different archives).
#
# Usage: test-fetch-artifact.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FETCH="$SCRIPT_DIR/fetch-artifact.sh"
[ -x "$FETCH" ] || { echo "ERROR: $FETCH not found or not executable" >&2; exit 1; }

REAL_AWS=$(command -v aws) || { echo "ERROR: real aws CLI not found on PATH" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-fetch-artifact-test.XXXXXX)
FAKEBIN="$WORKDIR/fakebin"
mkdir -p "$FAKEBIN"
trap 'rm -rf -- "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

sha_n() { python3 -c "print(str($1) * 40)"; }

# The fake `aws` shim: serves FIXTURE_FILE (set per-test) for `s3api
# get-object ... <dest>`, delegates everything else to the real binary.
write_shim() {
  local fixture="$1"
  cat > "$FAKEBIN/aws" <<EOF
#!/bin/sh
if [ "\$1" = "s3api" ] && [ "\$2" = "get-object" ]; then
  # POSIX-portable "last positional argument" (dash has no \${@: -1}).
  for dest in "\$@"; do :; done
  cp "$fixture" "\$dest"
  exit 0
fi
exec "$REAL_AWS" "\$@"
EOF
  chmod +x "$FAKEBIN/aws"
}

run_fetch() {
  local key="$1" expected_sha="$2" staging="$3"
  PATH="$FAKEBIN:$PATH" "$FETCH" --bucket test-bucket --key "$key" --expected-sha256 "$expected_sha" --staging-root "$staging"
}

SHA1=$(sha_n 1)
VALID_KEY="releases/$SHA1/ptai-release.tar.gz"

build_archive() {
  # build_archive <output.tar.gz> <staging-dir-to-tar>
  tar -czf "$1" -C "$2" .
}

# ---------------------------------------------------------------------------
echo "=== Test 1: valid artifact, correct SHA -- succeeds, correct staging path ==="
STAGE=$(mktemp -d "$WORKDIR/src.XXXXXX")
mkdir -p "$STAGE/backend"
echo "print()" > "$STAGE/backend/main.py"
ARCHIVE="$WORKDIR/valid.tar.gz"
build_archive "$ARCHIVE" "$STAGE"
SHA256=$(sha256sum "$ARCHIVE" | awk '{print $1}')
write_shim "$ARCHIVE"
STAGING_ROOT=$(mktemp -d "$WORKDIR/staging.XXXXXX")
OUT=$(run_fetch "$VALID_KEY" "$SHA256" "$STAGING_ROOT" 2>/tmp/fa1.err)
RC=$?
STAGING_PATH=$(echo "$OUT" | grep '^staging_path=' | cut -d= -f2-)
if [ "$RC" -eq 0 ] && [ -n "$STAGING_PATH" ] && [ -f "$STAGING_PATH/backend/main.py" ] \
   && [[ "$STAGING_PATH" == "$STAGING_ROOT"/* ]]; then
  pass "valid artifact: exit 0, extracted correctly, staging path under the given root"
else
  fail "valid artifact: expected exit 0 + correct extraction, got exit=$RC"; cat /tmp/fa1.err
fi

# ---------------------------------------------------------------------------
echo "=== Test 2: wrong SHA-256 -- rejected, no extraction ==="
STAGING_ROOT2=$(mktemp -d "$WORKDIR/staging2.XXXXXX")
WRONG_SHA=$(python3 -c "print('0'*64)")
set +e
run_fetch "$VALID_KEY" "$WRONG_SHA" "$STAGING_ROOT2" >/tmp/fa2.out 2>&1
RC=$?
set -e
EXTRACTED_DIRS=$(find "$STAGING_ROOT2" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
if [ "$RC" -eq 1 ] && [ -z "$EXTRACTED_DIRS" ] && grep -q "Checksum mismatch" /tmp/fa2.out; then
  pass "wrong SHA-256: rejected (exit 1), no extraction occurred"
else
  fail "wrong SHA-256: expected exit 1 + no extraction, got exit=$RC"; cat /tmp/fa2.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 3: malformed SHA-256 (wrong length / non-hex) rejected ==="
STAGING_ROOT3=$(mktemp -d "$WORKDIR/staging3.XXXXXX")
for bad in "abc" "$(python3 -c 'print("g"*64)')" "$(python3 -c 'print("a"*63)')"; do
  set +e
  run_fetch "$VALID_KEY" "$bad" "$STAGING_ROOT3" >/tmp/fa3.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    pass "malformed SHA '$bad': rejected (exit 1)"
  else
    fail "malformed SHA '$bad': expected exit 1, got exit=$RC"; cat /tmp/fa3.out
  fi
done

# ---------------------------------------------------------------------------
echo "=== Test 4: invalid object key format rejected ==="
STAGING_ROOT4=$(mktemp -d "$WORKDIR/staging4.XXXXXX")
for bad_key in "releases/short/ptai-release.tar.gz" "other/$SHA1/ptai-release.tar.gz" "releases/$SHA1/wrong-name.tar.gz" "releases/../etc/ptai-release.tar.gz"; do
  set +e
  run_fetch "$bad_key" "$SHA256" "$STAGING_ROOT4" >/tmp/fa4.out 2>&1
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    pass "invalid key '$bad_key': rejected (exit 1)"
  else
    fail "invalid key '$bad_key': expected exit 1, got exit=$RC"; cat /tmp/fa4.out
  fi
done

# ---------------------------------------------------------------------------
echo "=== Test 5: path-traversal entry in archive rejected ==="
STAGE5=$(mktemp -d "$WORKDIR/src5.XXXXXX")
mkdir -p "$STAGE5/backend"
echo "ok" > "$STAGE5/backend/file.txt"
ARCHIVE5="$WORKDIR/traversal.tar.gz"
(cd "$STAGE5" && tar -czf "$ARCHIVE5" backend/file.txt --transform 's,^backend/file.txt,../evil.txt,')
SHA5=$(sha256sum "$ARCHIVE5" | awk '{print $1}')
write_shim "$ARCHIVE5"
STAGING_ROOT5=$(mktemp -d "$WORKDIR/staging5.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA5" "$STAGING_ROOT5" >/tmp/fa5.out 2>&1
RC=$?
set -e
EXTRACTED5=$(find "$STAGING_ROOT5" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
if [ "$RC" -eq 1 ] && [ -z "$EXTRACTED5" ] && grep -q "path-traversal" /tmp/fa5.out; then
  pass "path traversal entry: rejected (exit 1), no extraction occurred"
else
  fail "path traversal entry: expected exit 1 + no extraction, got exit=$RC"; cat /tmp/fa5.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 6: absolute path entry in archive rejected ==="
STAGE6=$(mktemp -d "$WORKDIR/src6.XXXXXX")
echo "ok" > "$STAGE6/file.txt"
ARCHIVE6="$WORKDIR/absolute.tar.gz"
(cd "$STAGE6" && tar -czf "$ARCHIVE6" file.txt --transform 's,^file.txt,/etc/evil.txt,')
SHA6=$(sha256sum "$ARCHIVE6" | awk '{print $1}')
write_shim "$ARCHIVE6"
STAGING_ROOT6=$(mktemp -d "$WORKDIR/staging6.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA6" "$STAGING_ROOT6" >/tmp/fa6.out 2>&1
RC=$?
set -e
EXTRACTED6=$(find "$STAGING_ROOT6" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
if [ "$RC" -eq 1 ] && [ -z "$EXTRACTED6" ] && grep -q "absolute path" /tmp/fa6.out; then
  pass "absolute path entry: rejected (exit 1), no extraction occurred"
else
  fail "absolute path entry: expected exit 1 + no extraction, got exit=$RC"; cat /tmp/fa6.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 7: symlink entry in archive rejected ==="
STAGE7=$(mktemp -d "$WORKDIR/src7.XXXXXX")
ln -s /etc/passwd "$STAGE7/evil-link"
ARCHIVE7="$WORKDIR/symlink.tar.gz"
(cd "$STAGE7" && tar -czf "$ARCHIVE7" evil-link)
SHA7=$(sha256sum "$ARCHIVE7" | awk '{print $1}')
write_shim "$ARCHIVE7"
STAGING_ROOT7=$(mktemp -d "$WORKDIR/staging7.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA7" "$STAGING_ROOT7" >/tmp/fa7.out 2>&1
RC=$?
set -e
EXTRACTED7=$(find "$STAGING_ROOT7" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
if [ "$RC" -eq 1 ] && [ -z "$EXTRACTED7" ] && grep -q "disallowed entry type" /tmp/fa7.out; then
  pass "symlink entry: rejected (exit 1), no extraction occurred"
else
  fail "symlink entry: expected exit 1 + no extraction, got exit=$RC"; cat /tmp/fa7.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 8: hard-linked entry in archive rejected ==="
STAGE8=$(mktemp -d "$WORKDIR/src8.XXXXXX")
echo "original" > "$STAGE8/original.txt"
ln "$STAGE8/original.txt" "$STAGE8/hardlink.txt"
ARCHIVE8="$WORKDIR/hardlink.tar.gz"
(cd "$STAGE8" && tar -czf "$ARCHIVE8" original.txt hardlink.txt)
SHA8=$(sha256sum "$ARCHIVE8" | awk '{print $1}')
write_shim "$ARCHIVE8"
STAGING_ROOT8=$(mktemp -d "$WORKDIR/staging8.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA8" "$STAGING_ROOT8" >/tmp/fa8.out 2>&1
RC=$?
set -e
EXTRACTED8=$(find "$STAGING_ROOT8" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
# GNU tar marks hard-linked entries with a leading 'h' type character in
# verbose listings, so the entry-type check (which runs first) rejects them
# before the dedicated " link to " grep ever gets a chance to - either
# diagnostic is a correct rejection, so accept both messages.
if [ "$RC" -eq 1 ] && [ -z "$EXTRACTED8" ] \
   && grep -qE "hard link|disallowed entry type" /tmp/fa8.out; then
  pass "hard-linked entry: rejected (exit 1), no extraction occurred"
else
  fail "hard-linked entry: expected exit 1 + no extraction, got exit=$RC"; cat /tmp/fa8.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 9: top-level .env in archive rejected post-extraction ==="
STAGE9=$(mktemp -d "$WORKDIR/src9.XXXXXX")
echo "DATABASE_URL=postgresql://test:test@localhost/test" > "$STAGE9/.env"
echo "ok" > "$STAGE9/file.txt"
ARCHIVE9="$WORKDIR/secret.tar.gz"
build_archive "$ARCHIVE9" "$STAGE9"
SHA9=$(sha256sum "$ARCHIVE9" | awk '{print $1}')
write_shim "$ARCHIVE9"
STAGING_ROOT9=$(mktemp -d "$WORKDIR/staging9.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA9" "$STAGING_ROOT9" >/tmp/fa9.out 2>&1
RC=$?
set -e
LEFTOVER9=$(find "$STAGING_ROOT9" -maxdepth 1 -type d -name 'release-*' 2>/dev/null || true)
if [ "$RC" -eq 1 ] && [ -z "$LEFTOVER9" ] && grep -q "secret-shaped file" /tmp/fa9.out && ! grep -q "DATABASE_URL" /tmp/fa9.out; then
  pass "top-level .env: rejected (exit 1), leftover extraction dir removed, no secret VALUE printed in logs"
else
  fail "top-level .env: expected exit 1 + cleanup + no leaked value, got exit=$RC"; cat /tmp/fa9.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 10: nested backend/.env in archive rejected ==="
STAGE10=$(mktemp -d "$WORKDIR/src10.XXXXXX")
mkdir -p "$STAGE10/backend"
echo "OPENAI_API_KEY=fake-value-for-test" > "$STAGE10/backend/.env"
ARCHIVE10="$WORKDIR/nested-secret.tar.gz"
build_archive "$ARCHIVE10" "$STAGE10"
SHA10=$(sha256sum "$ARCHIVE10" | awk '{print $1}')
write_shim "$ARCHIVE10"
STAGING_ROOT10=$(mktemp -d "$WORKDIR/staging10.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA10" "$STAGING_ROOT10" >/tmp/fa10.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && ! grep -q "fake-value-for-test" /tmp/fa10.out; then
  pass "nested backend/.env: rejected (exit 1), no secret value leaked in logs"
else
  fail "nested backend/.env: expected exit 1 + no leaked value, got exit=$RC"; cat /tmp/fa10.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 11: .env.production rejected ==="
STAGE11=$(mktemp -d "$WORKDIR/src11.XXXXXX")
echo "SECRET=fake" > "$STAGE11/.env.production"
ARCHIVE11="$WORKDIR/env-production.tar.gz"
build_archive "$ARCHIVE11" "$STAGE11"
SHA11=$(sha256sum "$ARCHIVE11" | awk '{print $1}')
write_shim "$ARCHIVE11"
STAGING_ROOT11=$(mktemp -d "$WORKDIR/staging11.XXXXXX")
set +e
run_fetch "$VALID_KEY" "$SHA11" "$STAGING_ROOT11" >/tmp/fa11.out 2>&1
RC=$?
set -e
if [ "$RC" -eq 1 ] && grep -q "secret-shaped file" /tmp/fa11.out; then
  pass ".env.production: rejected (exit 1)"
else
  fail ".env.production: expected exit 1, got exit=$RC"; cat /tmp/fa11.out
fi

# ---------------------------------------------------------------------------
echo "=== Test 12: .env.example is allowed (not treated as a secret) ==="
STAGE12=$(mktemp -d "$WORKDIR/src12.XXXXXX")
echo "# template, no real values" > "$STAGE12/.env.example"
ARCHIVE12="$WORKDIR/env-example.tar.gz"
build_archive "$ARCHIVE12" "$STAGE12"
SHA12=$(sha256sum "$ARCHIVE12" | awk '{print $1}')
write_shim "$ARCHIVE12"
STAGING_ROOT12=$(mktemp -d "$WORKDIR/staging12.XXXXXX")
OUT12=$(run_fetch "$VALID_KEY" "$SHA12" "$STAGING_ROOT12" 2>/tmp/fa12.err)
RC=$?
STAGING_PATH12=$(echo "$OUT12" | grep '^staging_path=' | cut -d= -f2-)
if [ "$RC" -eq 0 ] && [ -f "$STAGING_PATH12/.env.example" ]; then
  pass ".env.example: allowed through, present in verified staging output"
else
  fail ".env.example: expected exit 0 + file present, got exit=$RC"; cat /tmp/fa12.err
fi

# ---------------------------------------------------------------------------
echo "=== Test 13: staging containment -- extraction always lands under the given staging root ==="
STAGING_ROOT13=$(mktemp -d "$WORKDIR/staging13.XXXXXX")
write_shim "$ARCHIVE"  # reuse the valid archive from Test 1
OUT13=$(run_fetch "$VALID_KEY" "$SHA256" "$STAGING_ROOT13" 2>/tmp/fa13.err)
STAGING_PATH13=$(echo "$OUT13" | grep '^staging_path=' | cut -d= -f2-)
REALPATH_ROOT=$(cd "$STAGING_ROOT13" && pwd)
case "$STAGING_PATH13" in
  "$REALPATH_ROOT"/*) pass "staging containment: output path is strictly under the given staging root" ;;
  *) fail "staging containment: output path $STAGING_PATH13 escaped the staging root $REALPATH_ROOT" ;;
esac

echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
