import type { ApiSession } from "../api";
import type { ConversationMessage, MessageSaveStatus } from "../../types/interview";
import type { PatientTextMeta, StudentTextMeta } from "./livekitPocEngine";

/** Map the backend transcript verbatim. ConversationTurn.id is preserved as
 * the UI message id, which is also the patientTurnId carried by Realtime
 * transcript_sync events. */
export function mapSessionMessages(session: ApiSession): ConversationMessage[] {
  return session.messages.map((message) => ({
    id: message.id,
    sender: message.sender,
    text: message.text,
    speakerId: message.speakerId,
    speakerLabel: message.speakerLabel,
    saveStatus: "saved" as const,
    timestamp: new Date(message.timestamp).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
  }));
}

/** Insert or update one Realtime patient message using the persisted
 * ConversationTurn identity. Both ready and final events therefore address
 * the same bubble; an interrupted final replaces the approved full answer
 * with exactly the partial text that was delivered. */
export function reconcileLiveKitPatientMessage(
  messages: ConversationMessage[],
  text: string,
  meta: PatientTextMeta,
  timestamp: string,
): ConversationMessage[] {
  const id = meta.patientTurnId;
  if (!id) return messages;

  const index = messages.findIndex((message) => message.id === id);
  const saveStatus: MessageSaveStatus = meta.final ? "saved" : "pending";
  if (index >= 0) {
    const next = messages.slice();
    next[index] = { ...next[index], text, saveStatus };
    return next;
  }

  return [
    ...messages,
    { id, sender: "patient", text, timestamp, saveStatus },
  ];
}

/** Insert or update one FINAL student message using the persisted
 * ConversationTurn identity (prompt_agent mode, where OpenAI Realtime owns the
 * conversation and the browser never authored the student turn). Keyed by the
 * DB studentTurnId so the later authoritative session refetch reconciles onto
 * the SAME bubble instead of appending a duplicate - the exact mirror of
 * reconcileLiveKitPatientMessage. */
export function reconcileLiveKitStudentMessage(
  messages: ConversationMessage[],
  text: string,
  meta: StudentTextMeta,
  timestamp: string,
): ConversationMessage[] {
  const id = meta.studentTurnId;
  if (!id) return messages;

  const index = messages.findIndex((message) => message.id === id);
  if (index >= 0) {
    const next = messages.slice();
    next[index] = { ...next[index], text, saveStatus: "saved" };
    return next;
  }

  return [
    ...messages,
    { id, sender: "student", text, timestamp, saveStatus: "saved" },
  ];
}
