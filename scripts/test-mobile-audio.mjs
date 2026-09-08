/**
 * Tests for the mobile interview audio logic (pure modules).
 * Run with: npm run test:mobileaudio
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { audioSetupOptions, autoInterruptNote } from "../.test-build/services/mobileAudio.js";

// --------------------------------------------------------------- mobile labels
test("mobile audio labels never say 'laptop', values preserved", () => {
  const mobile = audioSetupOptions(true);
  assert.deepEqual(mobile.map((o) => o.value), ["speakers", "headphones"]);
  assert.doesNotMatch(mobile[0].label, /laptop/i);
  assert.match(mobile[0].label, /device|phone/i);
  assert.match(mobile[1].label, /earbuds|headphones/i);

  const desktop = audioSetupOptions(false);
  assert.match(desktop[0].label, /laptop/i);
});

test("auto-interrupt note is stronger on mobile", () => {
  assert.match(autoInterruptNote(true), /headphones|earbuds/i);
  assert.notEqual(autoInterruptNote(true), autoInterruptNote(false));
});
