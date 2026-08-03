/**
 * Tests for the interview session-init error classifier (Part 1 fix).
 * Run with: npm run test:interview
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyInterviewInitError } from "../.test-build/services/interviewErrors.js";

function apiErr(status, code, message) {
  return { name: "ApiError", status, code, message };
}

test("admin 403 -> role/access message, NOT offline", () => {
  const r = classifyInterviewInitError(apiErr(403, "forbidden", "This resource is only available to student accounts."));
  assert.equal(r.offline, false);
  assert.equal(r.connection, "error");
  assert.match(r.message, /student account/i);
});

test("401 -> sign-in / session-expired message", () => {
  const r = classifyInterviewInitError(apiErr(401, "not_authenticated", "x"));
  assert.equal(r.offline, false);
  assert.equal(r.connection, "error");
  assert.match(r.message, /sign in/i);
});

test("5xx -> temporarily unavailable, NOT offline", () => {
  const r = classifyInterviewInitError(apiErr(502, "bad_gateway", "x"));
  assert.equal(r.offline, false);
  assert.match(r.message, /temporarily unavailable/i);
});

test("network_error (status 0) -> offline", () => {
  const r = classifyInterviewInitError(apiErr(0, "network_error", "unreachable"));
  assert.equal(r.offline, true);
  assert.equal(r.connection, "offline");
});

test("non-ApiError (raw Error) -> offline", () => {
  const r = classifyInterviewInitError(new Error("fetch failed"));
  assert.equal(r.offline, true);
  assert.equal(r.connection, "offline");
});

test("other 4xx -> error surfaces server message, not offline", () => {
  const r = classifyInterviewInitError(apiErr(404, "not_found", "gone"));
  assert.equal(r.offline, false);
  assert.equal(r.message, "gone");
});
