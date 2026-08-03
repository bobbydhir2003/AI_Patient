import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  getAiConfig,
  patchConversation,
  patchElevenLabs,
  patchOpenAI,
  testCredential,
  type AiConfig,
} from "../../../services/runtimeApi";
import { ErrorState, LoadingState, useToast } from "../../../portal/ui";

function ApplyBadge({ mode }: { mode: string }) {
  const label = mode === "immediate" ? "Applies immediately"
    : mode === "new_sessions" ? "Applies to new interview sessions"
    : mode === "restart_required" ? "Requires restart" : mode;
  return <span className="pt-ai-tag">{label}</span>;
}

export function AiConfigurationPage() {
  const { token } = useAuth();
  const toast = useToast();
  const [cfg, setCfg] = useState<AiConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!token) return;
    getAiConfig(token).then(setCfg).catch((e) =>
      setError(e instanceof ApiError ? e.message : "Could not load AI configuration."));
  }, [token]);
  useEffect(load, [load]);

  if (error && !cfg) return <ErrorState message={error} onRetry={load} />;
  if (!cfg) return <LoadingState label="Loading AI configuration…" />;

  const startEdit = (card: string, initial: Record<string, unknown>) => {
    setEditing(card);
    setForm(initial);
  };

  async function save(card: "openai" | "elevenlabs" | "conversation") {
    if (!token) return;
    setBusy(true);
    try {
      const fn = card === "openai" ? patchOpenAI : card === "elevenlabs" ? patchElevenLabs : patchConversation;
      const res = await fn(token, form);
      toast.success(res.message || "Saved.");
      setEditing(null);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  async function test(service: "openai" | "elevenlabs") {
    if (!token) return;
    try {
      const r = await testCredential(token, service);
      (r.status === "success" ? toast.success : toast.error)(`${service}: ${r.message}`);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Test failed.");
    }
  }

  const num = (v: unknown) => (v === "" || v === undefined ? undefined : Number(v));

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>AI Configuration</h1>
          <p className="pt-page-sub">Edit the backend's active OpenAI, ElevenLabs, and conversation settings.</p>
        </div>
      </div>

      {/* OpenAI */}
      <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">OpenAI <ApplyBadge mode="new_sessions" /></h2>
          <div className="pt-row">
            <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => test("openai")}>Test Connection</button>
            {editing !== "openai" && (
              <button className="pt-btn pt-btn-sm" onClick={() => startEdit("openai", {
                model: cfg.openai.model, timeoutSeconds: cfg.openai.timeoutSeconds,
                maxOutputTokens: cfg.openai.maxOutputTokens, streamingEnabled: cfg.openai.streamingEnabled,
              })}>Edit</button>
            )}
          </div>
        </div>
        {editing === "openai" ? (
          <div className="pt-row" style={{ gap: "var(--space-4)", alignItems: "flex-end" }}>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Model</span>
              <select className="pt-select" value={String(form.model ?? "")}
                onChange={(e) => setForm({ ...form, model: e.target.value })}>
                {cfg.openai.modelAllowlist.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Timeout (s)</span>
              <input className="pt-input" type="number" value={String(form.timeoutSeconds ?? "")}
                onChange={(e) => setForm({ ...form, timeoutSeconds: num(e.target.value) })} />
            </label>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Max tokens</span>
              <input className="pt-input" type="number" value={String(form.maxOutputTokens ?? "")}
                onChange={(e) => setForm({ ...form, maxOutputTokens: num(e.target.value) })} />
            </label>
            <label className="pt-row" style={{ gap: 6 }}>
              <input type="checkbox" checked={!!form.streamingEnabled}
                onChange={(e) => setForm({ ...form, streamingEnabled: e.target.checked })} />
              <span>Streaming</span>
            </label>
            <div className="pt-row">
              <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
              <button className="pt-btn pt-btn-sm" onClick={() => save("openai")} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        ) : (
          <dl className="pt-kv">
            <dt>Model</dt><dd>{cfg.openai.model}</dd>
            <dt>Streaming</dt><dd>{cfg.openai.streamingEnabled ? "Enabled" : "Disabled"}</dd>
            <dt>Timeout</dt><dd>{cfg.openai.timeoutSeconds ?? "—"}s</dd>
            <dt>Max tokens</dt><dd>{cfg.openai.maxOutputTokens ?? "—"}</dd>
            <dt>Status</dt><dd>{cfg.openai.status}</dd>
          </dl>
        )}
      </section>

      {/* ElevenLabs */}
      <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">ElevenLabs <ApplyBadge mode="immediate" /></h2>
          <div className="pt-row">
            <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => test("elevenlabs")}>Test Connection</button>
            {editing !== "elevenlabs" && (
              <button className="pt-btn pt-btn-sm" onClick={() => startEdit("elevenlabs", {
                enabled: cfg.elevenlabs.enabled,
                model: cfg.elevenlabs.model, outputFormat: cfg.elevenlabs.outputFormat,
                timeoutSeconds: cfg.elevenlabs.timeoutSeconds,
              })}>Edit</button>
            )}
          </div>
        </div>
        {editing === "elevenlabs" ? (
          <div className="pt-row" style={{ gap: "var(--space-4)", alignItems: "flex-end" }}>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Service</span>
              <select className="pt-select" value={form.enabled ? "enabled" : "disabled"}
                onChange={(e) => setForm({ ...form, enabled: e.target.value === "enabled" })}>
                <option value="enabled">Enabled</option>
                <option value="disabled">Disabled</option>
              </select>
            </label>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Model</span>
              <select className="pt-select" value={String(form.model ?? "")}
                onChange={(e) => setForm({ ...form, model: e.target.value })}>
                {cfg.elevenlabs.modelAllowlist.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Output format</span>
              <select className="pt-select" value={String(form.outputFormat ?? "")}
                onChange={(e) => setForm({ ...form, outputFormat: e.target.value })}>
                {cfg.elevenlabs.formatAllowlist.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="pt-field" style={{ marginBottom: 0 }}>
              <span>Timeout (s)</span>
              <input className="pt-input" type="number" value={String(form.timeoutSeconds ?? "")}
                onChange={(e) => setForm({ ...form, timeoutSeconds: num(e.target.value) })} />
            </label>
            <div className="pt-row">
              <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
              <button className="pt-btn pt-btn-sm" onClick={() => save("elevenlabs")} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        ) : (
          <dl className="pt-kv">
            <dt>Service</dt><dd>{cfg.elevenlabs.enabled ? "Enabled" : "Disabled"}</dd>
            <dt>Model</dt><dd>{cfg.elevenlabs.model}</dd>
            <dt>Output format</dt><dd>{cfg.elevenlabs.outputFormat}</dd>
            <dt>Timeout</dt><dd>{cfg.elevenlabs.timeoutSeconds ?? "—"}s</dd>
            <dt>Status</dt><dd>{cfg.elevenlabs.status}</dd>
          </dl>
        )}
      </section>

      {/* Conversation */}
      <section className="pt-panel">
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">Conversation settings <ApplyBadge mode="new_sessions" /></h2>
          {editing !== "conversation" && (
            <button className="pt-btn pt-btn-sm" onClick={() => startEdit("conversation", {
              sentenceLevelStreaming: cfg.conversation.sentenceLevelStreaming === "Enabled",
              patientStreaming: cfg.conversation.patientStreaming === "Enabled",
            })}>Edit</button>
          )}
        </div>
        {editing === "conversation" ? (
          <div className="pt-row" style={{ gap: "var(--space-5)", alignItems: "center" }}>
            <label className="pt-row" style={{ gap: 6 }}>
              <input type="checkbox" checked={!!form.sentenceLevelStreaming}
                onChange={(e) => setForm({ ...form, sentenceLevelStreaming: e.target.checked })} />
              <span>Sentence-level streaming</span>
            </label>
            <label className="pt-row" style={{ gap: 6 }}>
              <input type="checkbox" checked={!!form.patientStreaming}
                onChange={(e) => setForm({ ...form, patientStreaming: e.target.checked })} />
              <span>Patient streaming</span>
            </label>
            <div className="pt-row">
              <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
              <button className="pt-btn pt-btn-sm" onClick={() => save("conversation")} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        ) : (
          <dl className="pt-kv">
            {Object.entries(cfg.conversation).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase())}</dt>
                <dd>{String(v)}</dd>
              </div>
            ))}
          </dl>
        )}
        <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
          Built-in behaviors (disclosure, motivational interviewing, age-appropriate language) are not
          runtime-toggleable and are shown for reference.
        </p>
      </section>
    </div>
  );
}
