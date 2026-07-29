import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  getRuntimeVoices,
  patchVoice,
  previewVoiceConfig,
  restoreVoice,
  type RuntimeVoice,
} from "../../../services/runtimeApi";
import { ErrorState, LoadingState, useToast } from "../../../portal/ui";

const SLIDERS: [keyof RuntimeVoice, string, number, number, number][] = [
  ["stability", "Stability", 0, 1, 0.05],
  ["similarityBoost", "Similarity boost", 0, 1, 0.05],
  ["style", "Style", 0, 1, 0.05],
  ["speed", "Speed", 0.7, 1.2, 0.01],
];

function VoiceEditModal({ voice, onClose, onSaved }: {
  voice: RuntimeVoice; onClose: () => void; onSaved: () => void;
}) {
  const { token } = useAuth();
  const toast = useToast();
  const [f, setF] = useState({
    voiceId: "", displayName: voice.displayName, voiceName: voice.voiceName ?? "",
    modelId: voice.model ?? "", stability: voice.stability, similarityBoost: voice.similarityBoost,
    style: voice.style, speed: voice.speed, speakerBoost: voice.speakerBoost,
    previewText: voice.previewText, isActive: voice.status !== "not_configured",
  });
  const [busy, setBusy] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  async function preview() {
    if (!token) return;
    setPreviewing(true);
    try {
      const url = await previewVoiceConfig(token, voice.caseId, voice.speakerId, { ...f });
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Preview failed.");
    } finally {
      setPreviewing(false);
    }
  }

  async function save() {
    if (!token) return;
    setBusy(true);
    try {
      const body: Record<string, unknown> = { ...f, expectedUpdatedAt: voice.updatedAt ?? undefined };
      if (!body.voiceId) delete body.voiceId; // keep existing id if left blank
      await patchVoice(token, voice.caseId, voice.speakerId, body);
      toast.success(`${voice.speakerLabel} voice saved. Applies to new interview sessions.`);
      onSaved();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="pt-modal" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={`Edit ${voice.speakerLabel} voice`}>
        <h3>Edit voice — {voice.speakerLabel}</h3>
        <p className="pt-muted" style={{ fontSize: "0.8rem", marginTop: -4 }}>
          Current: {voice.maskedVoiceId ?? "not configured"} · applies to new interview sessions
          {voice.updatedBy ? ` · last updated by ${voice.updatedBy}` : ""}
        </p>
        <div className="pt-field">
          <label>New Voice ID <span className="pt-muted">(leave blank to keep current)</span></label>
          <input className="pt-input" value={f.voiceId} placeholder="ElevenLabs voice ID"
            onChange={(e) => setF({ ...f, voiceId: e.target.value })} />
        </div>
        <div className="pt-row" style={{ gap: "var(--space-3)" }}>
          <label className="pt-field" style={{ flex: 1 }}>
            <span>Display name</span>
            <input className="pt-input" value={f.displayName} onChange={(e) => setF({ ...f, displayName: e.target.value })} />
          </label>
          <label className="pt-field" style={{ flex: 1 }}>
            <span>Model ID</span>
            <input className="pt-input" value={f.modelId} onChange={(e) => setF({ ...f, modelId: e.target.value })} />
          </label>
        </div>
        {SLIDERS.map(([key, label, min, max, step]) => (
          <div className="pt-field" key={String(key)} style={{ marginBottom: "var(--space-3)" }}>
            <label>{label}: {Number(f[key as keyof typeof f]).toFixed(2)}</label>
            <input type="range" min={min} max={max} step={step}
              value={Number(f[key as keyof typeof f])}
              onChange={(e) => setF({ ...f, [key]: Number(e.target.value) })}
              aria-label={label} />
          </div>
        ))}
        <label className="pt-row" style={{ gap: 6, marginBottom: "var(--space-3)" }}>
          <input type="checkbox" checked={f.speakerBoost} onChange={(e) => setF({ ...f, speakerBoost: e.target.checked })} />
          <span>Speaker boost</span>
        </label>
        <label className="pt-field">
          <span>Preview text</span>
          <input className="pt-input" value={f.previewText} onChange={(e) => setF({ ...f, previewText: e.target.value })} />
        </label>
        <div className="pt-modal-actions" style={{ justifyContent: "space-between" }}>
          <button className="pt-btn pt-btn-secondary" onClick={preview} disabled={previewing || busy}>
            {previewing ? "Previewing…" : "Preview (unsaved)"}
          </button>
          <div className="pt-row">
            <button className="pt-btn pt-btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="pt-btn" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function PatientVoicesPage() {
  const { token } = useAuth();
  const toast = useToast();
  const [voices, setVoices] = useState<RuntimeVoice[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<RuntimeVoice | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(() => {
    if (!token) return;
    getRuntimeVoices(token).then((r) => setVoices(r.voices)).catch((e) =>
      setError(e instanceof ApiError ? e.message : "Could not load voices."));
  }, [token]);
  useEffect(load, [load]);

  async function restore(v: RuntimeVoice) {
    if (!token) return;
    try {
      await restoreVoice(token, v.caseId, v.speakerId);
      toast.success(`Restored default voice for ${v.speakerLabel}.`);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Restore failed.");
    }
  }

  if (error && !voices) return <ErrorState message={error} onRetry={load} />;
  if (!voices) return <LoadingState label="Loading voices…" />;

  const filtered = voices.filter((v) =>
    (v.patientName + v.speakerLabel).toLowerCase().includes(query.toLowerCase()));

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>Patient Voices</h1>
          <p className="pt-page-sub">Edit each speaker's voice, preview unsaved settings, and restore defaults.</p>
        </div>
        <input className="pt-input" style={{ maxWidth: 220 }} placeholder="Search patients"
          value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search patients" />
      </div>

      <div className="pt-table-wrap" style={{ overflowX: "auto" }}>
        <table className="pt-table">
          <thead>
            <tr>
              <th scope="col">Patient</th><th scope="col">Speaker</th><th scope="col">Voice ID (masked)</th>
              <th scope="col">Model</th><th scope="col">Status</th><th scope="col">Source</th><th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((v) => (
              <tr key={v.caseId + v.speakerId}>
                <td>
                  <div className="pt-row" style={{ gap: 8, flexWrap: "nowrap" }}>
                    <img src={v.image} alt="" width={30} height={30}
                      style={{ borderRadius: "50%", objectFit: "cover", objectPosition: "center top" }} />
                    <span style={{ color: "var(--color-text-primary)" }}>{v.patientName}</span>
                  </div>
                </td>
                <td>{v.speakerLabel}</td>
                <td>{v.maskedVoiceId ?? <span className="pt-muted">Not configured</span>}</td>
                <td>{v.model ?? "—"}</td>
                <td><span className={`pt-badge ${v.status === "active" ? "pt-badge-green" : "pt-badge-gray"}`}>
                  {v.status === "active" ? "Active" : "Not configured"}</span></td>
                <td><span className="pt-muted" style={{ fontSize: "0.8rem" }}>{v.source}</span></td>
                <td>
                  <div className="pt-row" style={{ gap: 6 }}>
                    <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setEditing(v)}>Edit</button>
                    {v.hasOverride && (
                      <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => restore(v)}>Restore default</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        Voice IDs are masked; the ElevenLabs key never leaves the backend. Voice changes apply to new
        interview sessions. Camden and his mother are separate speaker records.
      </p>

      {editing && (
        <VoiceEditModal voice={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      )}
    </div>
  );
}
