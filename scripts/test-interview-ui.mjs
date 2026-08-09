/**
 * Static-source guarantees that the redesigned interview UI is fully
 * data-driven across every patient case (no hardcoded Camden, real photo only
 * in the profile panel, generic avatars in the transcript, real transcript).
 *
 * These read the component source (same approach as test-landing-ui.mjs) so
 * they run with no bundler/DOM and can't drift from the implementation.
 *
 * Run with: npm run test:interviewui
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const interviewPage = read("src/pages/InterviewPage.tsx");
const welcomeCard = read("src/components/interview/InterviewWelcomeCard.tsx");
const messageBubble = read("src/components/interview/MessageBubble.tsx");
const conversationPanel = read("src/components/interview/ConversationPanel.tsx");
const patientProfile = read("src/components/cases/PatientProfile.tsx");

// 1 + 2 + 3: patient image / name / age come from the case object.
test("interview profile is driven by the real case image/name/age", () => {
  // The left patient panel renders the case's OWN image, name and age (whether
  // via <PatientProfile/> or the inline patient panel — both are data-driven).
  assert.match(interviewPage, /src=\{patientCase\.image\}/);
  assert.match(interviewPage, /\{patientCase\.name\}/);
  assert.match(interviewPage, /patientCase\.age/);
  // Main title is interpolated from the case, never a literal.
  assert.match(interviewPage, /Interview with \{patientCase\.name\}/);
});

// 3 (explicit): Camden is not hardcoded anywhere in the interview UI.
test("no hardcoded Camden in the reusable interview UI", () => {
  for (const [name, src] of [
    ["InterviewPage", interviewPage],
    ["InterviewWelcomeCard", welcomeCard],
    ["MessageBubble", messageBubble],
    ["ConversationPanel", conversationPanel],
  ]) {
    // Allow the word inside comments (explanations), but never as a rendered
    // string literal like >Camden< or "Camden".
    const codeOnly = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.doesNotMatch(codeOnly, /["'>]\s*Camden\b/, `${name} must not hardcode Camden`);
    assert.doesNotMatch(codeOnly, /caseId\s*===\s*["']camden["']/, `${name} must not branch on the Camden case`);
  }
});

// 4: chat bubbles use generic icons, never the real patient photograph.
test("transcript bubbles render generic icons (no real photo passed in)", () => {
  // MessageBubble defines generic person/user glyphs.
  assert.match(messageBubble, /IconUser/);
  assert.match(messageBubble, /IconPatient/);
  // The transcript never feeds a real photo to the bubbles: ConversationPanel
  // renders MessageBubble WITHOUT a patientImage prop, so the generic icons are
  // always used (the real photo lives only in the left profile panel).
  const bubbleCall = conversationPanel.match(/<MessageBubble[\s\S]*?\/>/);
  assert.ok(bubbleCall, "ConversationPanel must render <MessageBubble/>");
  assert.doesNotMatch(bubbleCall[0], /patientImage/);
});

// 5: transcript renders REAL messages + preserves speaker identity.
test("transcript renders real messages and preserves speaker labels", () => {
  assert.match(conversationPanel, /messages\.map\(/);
  // Caregiver/patient identity from the backend is respected (not forced).
  assert.match(messageBubble, /message\.speakerLabel/);
  assert.match(messageBubble, /message\.sender === "patient"/);
});

// 6: an empty/welcome state shows before any conversation.
test("empty state + welcome card are present before conversation", () => {
  assert.match(conversationPanel, /messages\.length === 0/);
  assert.match(conversationPanel, /\{welcome\}/);
  // Welcome card is generic: only the display name is interpolated.
  assert.match(welcomeCard, /interviewing \{patientName\}/);
});

// 9: the composer mic button wires into the existing voice hook (no 2nd system).
test("composer mic toggles the existing voice hook", () => {
  assert.match(conversationPanel, /mic\?\.supported/);
  assert.match(interviewPage, /voice\.active \? voice\.stopConversation\(\) : voice\.startConversation\(\)/);
});

// Fixed-size interview window: ONLY the transcript scrolls.
const panelCss = read("src/components/interview/ConversationPanel.module.css");
const interviewCss = read("src/pages/InterviewPage.module.css");

function block(css, selector) {
  const i = css.indexOf(selector);
  assert.ok(i >= 0, `missing selector ${selector}`);
  return css.slice(i, css.indexOf("}", i) + 1);
}

test("interview card is a fixed column that clips; only .messages scrolls", () => {
  // The card must shrink to the fixed panel and hide its own overflow.
  const panel = block(panelCss, ".panel {");
  assert.match(panel, /min-height:\s*0/);
  assert.match(panel, /overflow:\s*hidden/);
  // The transcript is the single scroll region.
  const messages = block(panelCss, ".messages {");
  assert.match(messages, /overflow-y:\s*auto/);
  assert.match(messages, /min-height:\s*0/);
  // Welcome + controls are fixed (do not scroll with the transcript).
  assert.match(panelCss, /\.welcomeSlot\s*\{[\s\S]*?flex-shrink:\s*0/);
  assert.match(panelCss, /\.inputBar\s*\{[\s\S]*?flex-shrink:\s*0/);
});

test("the panel card can shrink inside the fixed main panel (no forced tall min-height)", () => {
  const card = block(interviewCss, ".mainPanel :global(.card) {");
  assert.match(card, /min-height:\s*0/);
  assert.doesNotMatch(card, /min-height:\s*680px/);
  // The workspace itself is a fixed viewport region that clips (no page growth).
  assert.match(block(interviewCss, ".page {"), /overflow:\s*hidden/);
});

test("transcript has a polished (non-default) scrollbar", () => {
  assert.match(panelCss, /scrollbar-width:\s*thin/);
  assert.match(panelCss, /::-webkit-scrollbar/);
});
