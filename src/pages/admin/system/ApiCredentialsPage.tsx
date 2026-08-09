import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  getHistory,
  getRuntimeCredentials,
  removeCredential,
  replaceCredential,
  testCredential,
  type HistoryItem,
  type RuntimeCredential,
} from "../../../services/runtimeApi";
import { ConfirmModal, ErrorState, LoadingState, useToast } from "../../../portal/ui";

/** Human label for the effective credential source. */
function sourceLabel(source: string): string {
  if (source === "database") return "Database";
  if (source === "environment") return "Environment";
  return "Not configured";
}

function ReplaceKeyModal({ service, onClose, onDone }: {
  service: string; onClose: () => void; onDone: () => void;
}) {
  const { token } = useAuth();
  const toast = useToast();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!token || key.trim().length < 8) return;
    setBusy(true);
    try {
      const r = await replaceCredential(token, service, key.trim());
      setKey(""); // never keep the key in state
      toast.success(r.message || "Key stored securely.");
      onDone();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Could not store the key.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="pt-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true"
        aria-label={`Replace ${service} API key`}>
        <h3>Replace {service} API key</h3>
        <p className="pt-sub" style={{ marginBottom: "var(--space-3)" }}>
          The key is sent over HTTPS, validated and encrypted server-side, and never returned to the
          browser. The previous key is not shown.
        </p>
        <div className="pt-field">
          <label htmlFor="newkey">New API key</label>
          <input id="newkey" className="pt-input" type="password" autoComplete="off" value={key}
            placeholder="Paste the new key" onChange={(e) => setKey(e.target.value)} autoFocus />
        </div>
        <div className="pt-modal-actions">
          <button className="pt-btn pt-btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="pt-btn" onClick={submit} disabled={busy || key.trim().length < 8}>
            {busy ? "Saving…" : "Save key"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ApiCredentialsPage() {
  const { token } = useAuth();
  const toast = useToast();
  const [creds, setCreds] = useState<RuntimeCredential[] | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [replacing, setReplacing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!token) return;
    getRuntimeCredentials(token).then((r) => setCreds(r.credentials)).catch((e) =>
      setError(e instanceof ApiError ? e.message : "Could not load credentials."));
    getHistory(token).then((r) => setHistory(r.history.filter((h) => h.type === "credential"))).catch(() => undefined);
  }, [token]);
  useEffect(load, [load]);

  async function test(service: string) {
    if (!token) return;
    try {
      const r = await testCredential(token, service);
      (r.status === "success" ? toast.success : toast.error)(`${service}: ${r.message}`);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Test failed.");
    }
  }

  async function doRemove() {
    if (!token || !removing) return;
    setBusy(true);
    try {
      await removeCredential(token, removing);
      toast.success(`${removing} key removed.`);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Remove failed.");
    } finally {
      setBusy(false);
      setRemoving(null);
    }
  }

  if (error && !creds) return <ErrorState message={error} onRetry={load} />;
  if (!creds) return <LoadingState label="Loading credentials…" />;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>API Credentials</h1>
          <p className="pt-page-sub">Securely manage OpenAI and ElevenLabs keys. Full keys are never shown.</p>
        </div>
      </div>

      <div className="pt-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" }}>
        {creds.map((c) => (
          <div key={c.service} className="pt-panel">
            <div className="pt-panel-head">
              <h2 className="pt-panel-title" style={{ textTransform: "capitalize" }}>{c.service}</h2>
              <span className={`pt-badge ${c.configured ? "pt-badge-green" : "pt-badge-gray"}`}>
                {c.configured ? "Configured" : "Not configured"}
              </span>
            </div>
            <dl className="pt-kv">
              <dt>Key</dt><dd>{c.maskedValue ?? "—"}</dd>
              <dt>Source</dt><dd>{sourceLabel(c.source)}</dd>
              <dt>Last test</dt><dd>{c.lastTestStatus}{c.lastTestMessage ? ` — ${c.lastTestMessage}` : ""}</dd>
              <dt>Updated by</dt><dd>{c.updatedBy ?? "Unknown"}</dd>
            </dl>
            {!c.secureStorageAvailable && (
              <p className="pt-muted" style={{ fontSize: "0.82rem", marginTop: "var(--space-2)" }} role="note">
                Runtime credential storage requires CONFIG_ENCRYPTION_KEY to be configured on the server.
              </p>
            )}
            <div className="pt-row" style={{ marginTop: "var(--space-3)" }}>
              <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => test(c.service)}>Test Connection</button>
              <button
                className="pt-btn pt-btn-sm"
                onClick={() => setReplacing(c.service)}
                disabled={!c.secureStorageAvailable}
                title={c.secureStorageAvailable ? "" : "CONFIG_ENCRYPTION_KEY is not set on the server"}
              >
                Replace Key
              </button>
              {c.configured && c.source === "database" && (
                <button className="pt-btn pt-btn-danger pt-btn-sm" onClick={() => setRemoving(c.service)}>Remove Override</button>
              )}
            </div>
          </div>
        ))}
      </div>

      <section className="pt-panel" style={{ marginTop: "var(--space-5)" }}>
        <div className="pt-panel-head"><h2 className="pt-panel-title">Credential change history</h2></div>
        {history.length === 0 ? (
          <p className="pt-muted">No credential changes recorded.</p>
        ) : (
          <table className="pt-table">
            <thead><tr><th scope="col">Service</th><th scope="col">Change</th><th scope="col">By</th><th scope="col">When</th></tr></thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td style={{ textTransform: "capitalize" }}>{h.entityId}</td>
                  <td>{h.newValue}</td>
                  <td>{h.changedBy}</td>
                  <td>{h.changedAt ? new Date(h.changedAt).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
          History records only safe metadata (configured/replaced/removed) — never a key value. Old keys
          are not versioned, so a removed key must be re-entered rather than rolled back.
        </p>
      </section>

      {replacing && (
        <ReplaceKeyModal service={replacing} onClose={() => setReplacing(null)}
          onDone={() => { setReplacing(null); load(); }} />
      )}
      {removing && (
        <ConfirmModal title={`Remove ${removing} key?`}
          body="Future requests will fall back to the environment key (if any) or fail until a new key is set. This is logged."
          confirmLabel="Remove key" danger busy={busy} onConfirm={doRemove} onCancel={() => setRemoving(null)} />
      )}
    </div>
  );
}
