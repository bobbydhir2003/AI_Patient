import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  getAiConfig,
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

  async function save() {
    if (!token) return;
    setBusy(true);
    try {
      const res = await patchOpenAI(token, form);
      toast.success(res.message || "Saved.");
      setEditing(null);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    if (!token) return;
    try {
      const r = await testCredential(token, "openai");
      (r.status === "success" ? toast.success : toast.error)(`openai: ${r.message}`);
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
          <p className="pt-page-sub">
            Edit the backend's active OpenAI settings. The patient interview voice is provided by
            OpenAI Realtime (hosted prompts + OpenAI voice), configured via environment variables.
          </p>
        </div>
      </div>

      {/* OpenAI */}
      <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">OpenAI <ApplyBadge mode="new_sessions" /></h2>
          <div className="pt-row">
            <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => test()}>Test Connection</button>
            {editing !== "openai" && (
              <button className="pt-btn pt-btn-sm" onClick={() => startEdit("openai", {
                model: cfg.openai.model, timeoutSeconds: cfg.openai.timeoutSeconds,
                maxOutputTokens: cfg.openai.maxOutputTokens,
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
            <div className="pt-row">
              <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
              <button className="pt-btn pt-btn-sm" onClick={() => save()} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        ) : (
          <dl className="pt-kv">
            <dt>Model</dt><dd>{cfg.openai.model}</dd>
            <dt>Timeout</dt><dd>{cfg.openai.timeoutSeconds ?? "—"}s</dd>
            <dt>Max tokens</dt><dd>{cfg.openai.maxOutputTokens ?? "—"}</dd>
            <dt>Status</dt><dd>{cfg.openai.status}</dd>
          </dl>
        )}
      </section>
    </div>
  );
}
