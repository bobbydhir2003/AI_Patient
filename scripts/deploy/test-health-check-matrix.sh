#!/usr/bin/env bash
#
# test-health-check-matrix.sh - Validates health-check.sh against mock HTTP
# endpoints and a faked systemctl - never contacts or affects any real
# production service. Requires bash >= 4.4, python3, curl.
#
# Usage: test-health-check-matrix.sh
# Exit codes: 0 - all tests passed. 1 - one or more tests failed.
set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "ERROR: requires bash >= 4.4, found ${BASH_VERSION}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HC="$SCRIPT_DIR/health-check.sh"
[ -x "$HC" ] || { echo "ERROR: $HC not found or not executable" >&2; exit 1; }

WORKDIR=$(mktemp -d /tmp/ptai-healthcheck-test.XXXXXX)
FAKEBIN="$WORKDIR/fakebin"
mkdir -p "$FAKEBIN"
MOCK_PIDS_FILE="$WORKDIR/mock_pids"
: > "$MOCK_PIDS_FILE"

cleanup() {
  local pid
  if [ -f "$MOCK_PIDS_FILE" ]; then
    while IFS= read -r pid; do [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true; done < "$MOCK_PIDS_FILE"
  fi
  rm -rf -- "$WORKDIR"
}
trap cleanup EXIT

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }

# Start a minimal mock HTTP server on a free port. Args: <name> <python-handler-body>
# The handler body is a Python snippet implementing do_GET on `self`.
start_mock() {
  local name="$1" handler="$2"
  local port
  port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  cat > "$WORKDIR/mock_$name.py" <<PYEOF
import http.server, socketserver, time
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
$handler
with socketserver.TCPServer(("127.0.0.1", $port), Handler) as httpd:
    httpd.serve_forever()
PYEOF
  # Redirected explicitly away from this function's own stdout/stderr:
  # start_mock is always invoked as `PORT=$(start_mock ...)`, so its stdout
  # is a pipe feeding a command substitution. Without this redirect, the
  # long-lived (serve_forever) child would inherit a copy of that pipe's
  # write end and the reader would block forever waiting for EOF that never
  # comes, even after this function's own visible work is done.
  python3 "$WORKDIR/mock_$name.py" >/dev/null 2>&1 &
  local pid=$!
  # Also written via file, not an array: this function runs inside a command
  # substitution subshell, so array mutations here would never be visible to
  # the parent shell's cleanup trap.
  echo "$pid" >> "$MOCK_PIDS_FILE"
  # wait for the port to actually accept connections
  for _ in $(seq 1 30); do
    python3 -c "import socket; socket.create_connection(('127.0.0.1', $port), timeout=0.2).close()" 2>/dev/null && break
    sleep 0.1
  done
  echo "$port"
}

FAKE_SYSTEMCTL_ACTIVE=true
write_fake_systemctl() {
  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/sh
if [ "\$1" = "is-active" ]; then
  if [ "$FAKE_SYSTEMCTL_ACTIVE" = "true" ]; then echo active; exit 0; else echo inactive; exit 3; fi
fi
exit 1
EOF
  chmod +x "$FAKEBIN/systemctl"
}

run_hc() {
  PATH="$FAKEBIN:$PATH" "$HC" "$@"
}

# ---------------------------------------------------------------------------
echo "=== Case: healthy FastAPI (active services, 200 + status=ok, database/redis connected) ==="
FAKE_SYSTEMCTL_ACTIVE=true; write_fake_systemctl
PORT=$(start_mock healthy '        body = b"{\"status\":\"ok\",\"database\":\"connected\",\"redis\":\"connected\"}"
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)')
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 0 ] && echo "$OUT" | grep -q "STATUS=HEALTHY"; then
  pass "healthy FastAPI: exit 0, STATUS=HEALTHY"
else
  fail "healthy FastAPI: expected exit 0 + HEALTHY, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: public HTTPS success ==="
set +e
OUT=$(run_hc --mode public --public-url "http://127.0.0.1:$PORT" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 0 ] && echo "$OUT" | grep -q "STATUS=HEALTHY"; then
  pass "public HTTPS success: exit 0, STATUS=HEALTHY"
else
  fail "public HTTPS success: expected exit 0 + HEALTHY, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: service inactive (mocked systemctl reports inactive; endpoint still up) ==="
FAKE_SYSTEMCTL_ACTIVE=false; write_fake_systemctl
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED" && echo "$OUT" | grep -q "not active"; then
  pass "service inactive: exit 2, STATUS=FAILED, issue names the inactive unit"
else
  fail "service inactive: expected exit 2 + FAILED, got exit=$RC"; echo "$OUT"
fi
FAKE_SYSTEMCTL_ACTIVE=true; write_fake_systemctl

# ---------------------------------------------------------------------------
echo "=== Case: malformed health JSON (200 but not valid JSON / missing status=ok) ==="
PORT2=$(start_mock malformed '        body = b"not-json-at-all{"
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)')
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT2" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 1 ] && echo "$OUT" | grep -q "STATUS=DEGRADED"; then
  pass "malformed JSON: exit 1, STATUS=DEGRADED (reachable but not confirmed healthy)"
else
  fail "malformed JSON: expected exit 1 + DEGRADED, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: HTTP non-200 (500) ==="
PORT3=$(start_mock err500 '        body = b"internal error"
        self.send_response(500); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)')
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT3" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED"; then
  pass "HTTP 500: exit 2, STATUS=FAILED (curl -f treats non-2xx as failure)"
else
  fail "HTTP 500: expected exit 2 + FAILED, got exit=$RC"; echo "$OUT"
fi
set +e
OUT=$(run_hc --mode public --public-url "http://127.0.0.1:$PORT3" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "returned 500"; then
  pass "HTTP 500 (public mode): exit 2, FAILED, exact status code reported"
else
  fail "HTTP 500 (public mode): expected exit 2 + code reported, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: timeout (endpoint accepts connection but never responds) ==="
PORT4=$(start_mock hang '        time.sleep(30)')
set +e
START=$(date +%s)
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT4" 2>&1)
RC=$?
END=$(date +%s)
set -e
ELAPSED=$((END - START))
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED" && [ "$ELAPSED" -le 10 ]; then
  pass "timeout: exit 2, STATUS=FAILED, bounded by curl's own --max-time (${ELAPSED}s elapsed, did not hang for the full 30s)"
else
  fail "timeout: expected exit 2 + FAILED + bounded wait, got exit=$RC elapsed=${ELAPSED}s"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: unreachable endpoint (nothing listening on the port) ==="
FREE_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$FREE_PORT" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED" && echo "$OUT" | grep -q "unreachable"; then
  pass "unreachable endpoint: exit 2, STATUS=FAILED"
else
  fail "unreachable endpoint: expected exit 2 + FAILED, got exit=$RC"; echo "$OUT"
fi
set +e
OUT=$(run_hc --mode public --public-url "http://127.0.0.1:$FREE_PORT" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED"; then
  pass "unreachable endpoint (public mode): exit 2, STATUS=FAILED"
else
  fail "unreachable endpoint (public mode): expected exit 2 + FAILED, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: severity ratchet - FAILED (inactive unit) must not be downgraded by a later DEGRADED finding ==="
FAKE_SYSTEMCTL_ACTIVE=false; write_fake_systemctl
set +e
OUT=$(run_hc --mode local --local-url "http://127.0.0.1:$PORT2" 2>&1)  # malformed JSON endpoint + inactive unit
RC=$?
set -e
if [ "$RC" -eq 2 ] && echo "$OUT" | grep -q "STATUS=FAILED"; then
  pass "severity ratchet: FAILED (from inactive unit) correctly wins over a later DEGRADED (malformed JSON) finding"
else
  fail "severity ratchet: expected FAILED to win, got exit=$RC"; echo "$OUT"
fi
FAKE_SYSTEMCTL_ACTIVE=true; write_fake_systemctl

# ---------------------------------------------------------------------------
echo "=== Case: no PTAI_PUBLIC_URL and no --public-url -- public check skipped gracefully, not FAILED ==="
set +e
unset -v PTAI_PUBLIC_URL
OUT=$(run_hc --mode public 2>&1)
RC=$?
set -e
if [ "$RC" -eq 0 ] && echo "$OUT" | grep -q "STATUS=HEALTHY" && echo "$OUT" | grep -q "skipping public check"; then
  pass "no public URL configured: skipped gracefully, STATUS=HEALTHY (not treated as a failure)"
else
  fail "no public URL configured: expected graceful skip + HEALTHY, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo "=== Case: bad --mode argument ==="
set +e
OUT=$(run_hc --mode bogus 2>&1)
RC=$?
set -e
if [ "$RC" -eq 2 ]; then
  pass "bad --mode: rejected (exit 2)"
else
  fail "bad --mode: expected exit 2, got exit=$RC"; echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ]
