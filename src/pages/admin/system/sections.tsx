import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  clearAudioCache,
  fetchVoicePreview,
  type AiConfiguration,
  type Concurrency,
  type ConcurrencyLane,
  type CredentialStatus,
  type InfraCheck,
  type SystemActivity,
  type SystemAlert,
  type SystemOverview,
  type VoiceRow,
  type WorkerFleet,
  type WorkerRow,
} from "../../../services/systemApi";
import { ConfirmModal, EmptyState, useToast } from "../../../portal/ui";
import {
  IconAlert,
  IconCloud,
  IconCpu,
  IconDatabase,
  IconKey,
  IconMic,
  IconPlay,
  IconPulse,
  IconServer,
} from "../../../components/admin/icons";

// ------------------------------------------------------------------ helpers
type BadgeTone = "green" | "amber" | "red" | "gray";

const TONE: Record<string, BadgeTone> = {
  healthy: "green", connected: "green", configured: "green", active: "green", ok: "green", enabled: "green",
  warning: "amber", degraded: "amber", stale: "amber", local_only: "amber",
  failed: "red", critical: "red", error: "red", misconfigured: "red",
  unavailable: "gray", not_configured: "gray", disabled: "gray", unknown: "gray",
};

const LABEL: Record<string, string> = {
  healthy: "Healthy", connected: "Connected", configured: "Configured", active: "Active",
  warning: "Warning", degraded: "Degraded", stale: "Stale", local_only: "Local only",
  unavailable: "Unavailable", not_configured: "Not configured", misconfigured: "Misconfigured",
  disabled: "Disabled", failed: "Failed", ok: "OK",
};

function StatusBadge({ status }: { status: string }) {
  const tone = TONE[status] ?? "gray";
  const label = LABEL[status] ?? status.replace(/_/g, " ");
  return <span className={`pt-badge pt-badge-${tone}`}>{label}</span>;
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// ------------------------------------------------------------ health overview
function HealthCard({
  icon,
  title,
  status,
  rows,
}: {
  icon: ReactNode;
  title: string;
  status: string;
  rows: [string, string][];
}) {
  return (
    <div className="pt-panel pt-sys-card" role="group" aria-label={`${title} health`}>
      <div className="pt-sys-card-head">
        <span className="pt-sys-card-icon" aria-hidden="true">{icon}</span>
        <span className="pt-sys-card-title">{title}</span>
        <StatusBadge status={status} />
      </div>
      <dl className="pt-sys-card-rows">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function SystemHealthOverview({ data }: { data: SystemOverview }) {
  const { backend, database, openai, elevenlabs, audioQueue, storage } = data;
  return (
    <section className="pt-section" aria-labelledby="sys-health-h">
      <h2 id="sys-health-h" className="pt-panel-title" style={{ marginBottom: "var(--space-4)" }}>
        System Health Overview
      </h2>
      <div className="pt-sys-health">
        <HealthCard
          icon={<IconServer />}
          title="Backend API"
          status={backend.status}
          rows={[
            ["Response time", backend.responseTimeMs != null ? `${backend.responseTimeMs} ms` : "—"],
            ["Version", backend.version || "—"],
            ["Environment", backend.environment || "—"],
          ]}
        />
        <HealthCard
          icon={<IconDatabase />}
          title="Database"
          status={database.status}
          rows={[
            ["Type", database.dbType || "—"],
            ["Query latency", database.latencyMs != null ? `${database.latencyMs} ms` : "—"],
            ["Migration", database.migrationVersion ?? "Unknown"],
          ]}
        />
        <HealthCard
          icon={<IconCpu />}
          title="OpenAI"
          status={openai.status}
          rows={[
            ["Model", openai.model || "—"],
            ["Streaming", openai.streamingEnabled ? "Enabled" : "Disabled"],
            ["Last success", fmtTime(openai.lastSuccessAt) === "—" ? "Never" : fmtTime(openai.lastSuccessAt)],
          ]}
        />
        <HealthCard
          icon={<IconMic />}
          title="ElevenLabs"
          status={elevenlabs.status}
          rows={[
            ["Model", elevenlabs.model || "—"],
            ["Last success", fmtTime(elevenlabs.lastSuccessAt) === "—" ? "Never" : fmtTime(elevenlabs.lastSuccessAt)],
            ["Last error", elevenlabs.lastError ?? "None recorded"],
          ]}
        />
        <HealthCard
          icon={<IconPulse />}
          title="Audio Queue"
          status={audioQueue.status}
          rows={[
            ["Available", audioQueue.available ? "Yes" : "No"],
            ["Note", audioQueue.available ? `${audioQueue.pending ?? 0} pending` : "Not applicable"],
          ]}
        />
        <HealthCard
          icon={<IconCloud />}
          title="Storage"
          status={storage.status}
          rows={[
            ["Disk used", storage.percentUsed != null ? `${storage.percentUsed}%` : "—"],
            ["Free", fmtBytes(storage.freeBytes)],
            ["Audio cache", `${storage.audioCacheEntries ?? 0} / ${storage.audioCacheMaxEntries ?? 0}`],
          ]}
        />
      </div>
      {!audioQueue.available && audioQueue.message && (
        <p className="pt-muted" style={{ fontSize: "0.8rem", marginTop: "var(--space-2)" }}>
          Audio queue: {audioQueue.message}
        </p>
      )}
    </section>
  );
}

// -------------------------------------------------- live worker architecture
const MONITORING_BADGE: Record<string, { tone: string; label: string }> = {
  observed: { tone: "green", label: "Observed live" },
  local_only: { tone: "amber", label: "Local only" },
  unavailable: { tone: "red", label: "Unavailable" },
};

function fmtUptime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function ArchNode({ title, status, lines, note }: { title: string; status?: string; lines: string[]; note?: string }) {
  return (
    <div className="pt-arch-node">
      <div className="pt-arch-node-head">
        <span className="pt-arch-node-title">{title}</span>
        {status && <StatusBadge status={status} />}
      </div>
      {lines.map((l, i) => (
        <div key={i} className="pt-arch-node-line">{l}</div>
      ))}
      {note && <div className="pt-arch-node-note">{note}</div>}
    </div>
  );
}

function WorkerCard({ w, ttl }: { w: WorkerRow; ttl: number | null }) {
  const val = (n: number | null | undefined, suffix = "") => (n == null ? "Unavailable" : `${n}${suffix}`);
  return (
    <div className="pt-panel pt-worker-card">
      <div className="pt-worker-card-head">
        <span className="pt-worker-card-title">{w.hostname ? `${w.hostname}` : "worker"}{w.pid != null ? ` · pid ${w.pid}` : ""}</span>
        <StatusBadge status={w.health} />
      </div>
      <dl className="pt-sys-card-rows">
        <div><dt>Uptime</dt><dd>{fmtUptime(w.uptimeSeconds)}</dd></div>
        <div><dt>Heartbeat age</dt><dd>{w.heartbeatAgeSeconds != null ? `${w.heartbeatAgeSeconds}s${ttl ? ` / ${ttl}s TTL` : ""}` : "—"}</dd></div>
        <div><dt>Requests handled</dt><dd>{val(w.requestsTotal)}</dd></div>
        <div><dt>Req/min</dt><dd>{val(w.requestsPerMinute)}</dd></div>
        <div><dt>In-flight (HTTP)</dt><dd>{val(w.httpInFlight)}</dd></div>
        <div><dt>Interview / TTS</dt><dd>{val(w.interviewInFlight)} / {val(w.ttsInFlight)}</dd></div>
        <div><dt>Memory</dt><dd>{w.memoryMb != null ? `${w.memoryMb} MB` : "Unavailable"}</dd></div>
        <div><dt>Current task</dt><dd>{w.currentTask ?? "Unavailable"}</dd></div>
      </dl>
    </div>
  );
}

export function WorkerArchitectureSection({
  fleet,
  concurrency,
  database,
}: {
  fleet: WorkerFleet;
  concurrency: Concurrency;
  database: SystemOverview["database"];
}) {
  const badge = MONITORING_BADGE[fleet.monitoring] ?? MONITORING_BADGE.unavailable;
  const observedLabel =
    fleet.observed != null ? `${fleet.observed} observed` : fleet.monitoring === "local_only" ? "local only" : "unavailable";
  return (
    <section className="pt-section" aria-labelledby="arch-h">
      <div className="pt-arch-header">
        <h2 id="arch-h" className="pt-panel-title" style={{ margin: 0 }}>Live Backend Worker Architecture</h2>
        <span className={`pt-badge pt-badge-${badge.tone}`}>{badge.label}</span>
      </div>

      <div className="pt-panel pt-arch-flow">
        <ArchNode title="Client Requests" lines={["Inbound HTTP"]} />
        <span className="pt-arch-arrow">→</span>
        <ArchNode title="Nginx" lines={["Reverse proxy"]} note="Not observable by the app" />
        <span className="pt-arch-arrow">→</span>
        <ArchNode title="FastAPI" status={/* real backend health handled above */ "healthy"} lines={["App router"]} />
        <span className="pt-arch-arrow">→</span>
        <ArchNode
          title="Uvicorn Workers"
          status={fleet.status === "local_only" ? "warning" : fleet.status}
          lines={[`Configured: ${fleet.configured}`, `Observed: ${observedLabel}`]}
        />
        <span className="pt-arch-arrow">→</span>
        <ArchNode
          title="Redis / PostgreSQL"
          status={concurrency.redis.status === "connected" ? "connected" : concurrency.redis.status}
          lines={[`Redis: ${concurrency.redis.status}`, `DB: ${database.status}`]}
        />
        <span className="pt-arch-arrow">→</span>
        <ArchNode title="OpenAI / ElevenLabs" lines={["External providers"]} note="See concurrency below" />
      </div>

      {fleet.note && <p className="pt-muted" style={{ fontSize: "0.82rem", marginTop: "var(--space-2)" }}>{fleet.note}</p>}

      {fleet.workers.length > 0 ? (
        <div className="pt-worker-grid">
          {fleet.workers.map((w) => (
            <WorkerCard key={w.workerId} w={w} ttl={fleet.heartbeatTtlSeconds} />
          ))}
        </div>
      ) : (
        <p className="pt-muted" style={{ marginTop: "var(--space-3)" }}>
          {fleet.monitoring === "unavailable"
            ? "Worker monitoring is unavailable (Redis unreachable)."
            : "No workers observed."}
        </p>
      )}
    </section>
  );
}

// ------------------------------------------------ global concurrency & capacity
function ConcurrencyBar({ lane }: { lane: ConcurrencyLane }) {
  const pct = lane.limit > 0 ? Math.min(100, Math.round((lane.active / lane.limit) * 100)) : 0;
  const tone = pct >= 90 ? "red" : pct >= 70 ? "amber" : "green";
  return (
    <div className="pt-conc-lane">
      <div className="pt-conc-lane-head">
        <span>{lane.name}</span>
        <span className="pt-muted">
          {lane.active} / {lane.limit} ({pct}%) · {lane.scope}
          {lane.waiting != null ? ` · ${lane.waiting} waiting` : ""}
          {lane.queued != null ? ` · ${lane.queued} queued` : ""}
        </span>
      </div>
      <div className="pt-conc-track"><div className={`pt-conc-fill pt-conc-${tone}`} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

export function GlobalConcurrencySection({ concurrency }: { concurrency: Concurrency }) {
  return (
    <section className="pt-panel pt-section" aria-labelledby="conc-h">
      <div className="pt-arch-header">
        <h2 id="conc-h" className="pt-panel-title" style={{ margin: 0 }}>Global Concurrency &amp; Capacity</h2>
        <span className="pt-muted" style={{ fontSize: "0.8rem" }}>Scope: {concurrency.scope}</span>
      </div>
      <ConcurrencyBar lane={concurrency.openai} />
      <ConcurrencyBar lane={concurrency.tts} />
      <ConcurrencyBar lane={concurrency.assessment} />
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-2)" }}>
        Active counts are fleet-wide via the Redis semaphore when scope is “global”; otherwise per-process. Denominators are the live configured limits.
      </p>
    </section>
  );
}

// ------------------------------------------------ realtime infrastructure checks
export function RealtimeChecksSection({ checks }: { checks: InfraCheck[] }) {
  return (
    <section className="pt-panel pt-section" aria-labelledby="checks-h">
      <h2 id="checks-h" className="pt-panel-title" style={{ marginBottom: "var(--space-3)" }}>Realtime Infrastructure Checks</h2>
      <ul className="pt-checks">
        {checks.map((c) => (
          <li key={c.key} className="pt-check-row">
            <StatusBadge status={c.status} />
            <span className="pt-check-label">{c.label}</span>
            {c.detail && <span className="pt-muted pt-check-detail">{c.detail}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------- patient voices
export function PatientVoicesSection({ voices }: { voices: VoiceRow[] }) {
  const { token } = useAuth();
  const toast = useToast();
  const [playing, setPlaying] = useState<string | null>(null);

  async function preview(v: VoiceRow) {
    if (!token) return;
    setPlaying(v.caseId + v.speakerId);
    try {
      const url = await fetchVoicePreview(token, v.caseId);
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
      toast.success(`Playing ${v.speakerLabel} preview`);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Voice preview failed.");
    } finally {
      setPlaying(null);
    }
  }

  return (
    <section className="pt-panel" aria-labelledby="sys-voices-h">
      <div className="pt-panel-head">
        <h2 id="sys-voices-h" className="pt-panel-title"><IconMic /> Patient Voices</h2>
        <Link to="/admin/system/voices" className="pt-panel-link">Edit</Link>
      </div>
      <div className="pt-table-wrap" style={{ overflowX: "auto" }}>
        <table className="pt-table">
          <thead>
            <tr>
              <th scope="col">Patient</th>
              <th scope="col">Speaker</th>
              <th scope="col">Voice ID (masked)</th>
              <th scope="col">Model</th>
              <th scope="col">Status</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {voices.map((v) => (
              <tr key={v.caseId + v.speakerId}>
                <td>
                  <div className="pt-row" style={{ gap: "var(--space-2)", flexWrap: "nowrap" }}>
                    <img
                      src={v.image}
                      alt=""
                      width={30}
                      height={30}
                      style={{ borderRadius: "50%", objectFit: "cover", objectPosition: "center top" }}
                    />
                    <span style={{ color: "var(--color-text-primary)" }}>{v.patientName}</span>
                  </div>
                </td>
                <td>{v.speakerLabel}</td>
                <td>{v.maskedVoiceId ?? <span className="pt-muted">Not configured</span>}</td>
                <td>{v.model ?? "—"}</td>
                <td><StatusBadge status={v.status} /></td>
                <td>
                  {v.status === "active" ? (
                    <button
                      type="button"
                      className="pt-btn pt-btn-secondary pt-btn-sm"
                      onClick={() => preview(v)}
                      disabled={playing === v.caseId + v.speakerId}
                      aria-label={`Preview ${v.speakerLabel} voice`}
                    >
                      <IconPlay width={14} height={14} />{" "}
                      {playing === v.caseId + v.speakerId ? "Loading…" : "Preview"}
                    </button>
                  ) : (
                    <span className="pt-muted" style={{ fontSize: "0.8rem" }}>No preview</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        Preview plays a fixed sample sentence generated by the real ElevenLabs voice. Voice IDs are
        masked and the ElevenLabs key never leaves the backend.
      </p>
    </section>
  );
}

// ---------------------------------------------------------- ai configuration
function KvBlock({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div>
      <h3 style={{ fontSize: "0.95rem", margin: "0 0 var(--space-2)" }}>{title}</h3>
      <dl className="pt-kv">
        {rows.map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function AiConfigurationSection({ config }: { config: AiConfiguration }) {
  const { openai, elevenlabs, conversation } = config;
  return (
    <section className="pt-panel" aria-labelledby="sys-ai-h">
      <div className="pt-panel-head">
        <h2 id="sys-ai-h" className="pt-panel-title"><IconCpu /> AI Configuration</h2>
        <Link to="/admin/system/config" className="pt-panel-link">Edit</Link>
      </div>
      <div className="pt-row" style={{ gap: "var(--space-6)", alignItems: "flex-start" }}>
        <KvBlock
          title={`OpenAI — ${openai.status === "configured" ? "Configured" : "Not configured"}`}
          rows={[
            ["Model", openai.model || "—"],
            ["Streaming", openai.streamingEnabled ? "Enabled" : "Disabled"],
            ["Timeout", openai.timeoutSeconds != null ? `${openai.timeoutSeconds}s` : "—"],
            ["Max tokens", openai.maxOutputTokens != null ? String(openai.maxOutputTokens) : "—"],
          ]}
        />
        <KvBlock
          title={`ElevenLabs — ${elevenlabs.status === "configured" ? "Configured" : "Not configured"}`}
          rows={[
            ["Model", elevenlabs.model || "—"],
            ["Output format", elevenlabs.outputFormat || "—"],
            ["Timeout", elevenlabs.timeoutSeconds != null ? `${elevenlabs.timeoutSeconds}s` : "—"],
            ["Enabled", elevenlabs.enabled ? "Yes" : "No"],
          ]}
        />
      </div>
      <h3 style={{ fontSize: "0.95rem", margin: "var(--space-5) 0 var(--space-2)" }}>
        Conversation settings
      </h3>
      <dl className="pt-kv">
        <dt>Sentence-level streaming</dt><dd>{conversation.sentenceLevelStreaming}</dd>
        <dt>Patient streaming</dt><dd>{conversation.patientStreaming}</dd>
        <dt>Disclosure control</dt><dd>{conversation.disclosureControl}</dd>
        <dt>Motivational interviewing</dt><dd>{conversation.motivationalInterviewing}</dd>
        <dt>Age-appropriate language</dt><dd>{conversation.ageAppropriateLanguage}</dd>
        <dt>Caregiver routing</dt><dd>{conversation.caregiverRouting}</dd>
        <dt>Max patient response</dt><dd>{conversation.maxPatientResponseChars} chars</dd>
      </dl>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        These values reflect the backend's active configuration (read-only). Editing requires a
        server configuration change.
      </p>
    </section>
  );
}

// ------------------------------------------------------------- api credentials
export function ApiCredentialsSection({ credentials }: { credentials: CredentialStatus[] }) {
  return (
    <section className="pt-panel" aria-labelledby="sys-cred-h">
      <div className="pt-panel-head">
        <h2 id="sys-cred-h" className="pt-panel-title"><IconKey /> API Credentials</h2>
        <Link to="/admin/system/credentials" className="pt-panel-link">Edit</Link>
      </div>
      <div className="pt-table-wrap" style={{ overflowX: "auto" }}>
        <table className="pt-table">
          <thead>
            <tr>
              <th scope="col">Service</th>
              <th scope="col">Key (masked)</th>
              <th scope="col">Updated</th>
              <th scope="col">Updated by</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {credentials.map((c) => (
              <tr key={c.service}>
                <td style={{ color: "var(--color-text-primary)", textTransform: "capitalize" }}>{c.service}</td>
                <td>{c.maskedValue ?? <span className="pt-muted">Not configured</span>}</td>
                <td>{c.updatedAt ? fmtTime(c.updatedAt) : <span className="pt-muted">Unknown</span>}</td>
                <td>{c.updatedBy ?? <span className="pt-muted">Unknown</span>}</td>
                <td><StatusBadge status={c.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        The full key is never returned to the browser. Change history is not tracked in this
        deployment, and keys are managed through secure server configuration (not editable here).
      </p>
    </section>
  );
}

// -------------------------------------------------------------- recent activity
export function RecentActivitySection({ activity }: { activity: SystemActivity[] }) {
  return (
    <section className="pt-panel" aria-labelledby="sys-act-h">
      <div className="pt-panel-head">
        <h2 id="sys-act-h" className="pt-panel-title"><IconPulse /> Recent Admin Activity</h2>
      </div>
      {activity.length === 0 ? (
        <EmptyState title="No recorded admin activity yet." hint="Admin actions will appear here as they happen." />
      ) : (
        <div>
          {activity.map((a) => (
            <div key={a.id} className="pt-rs-item" style={{ cursor: "default" }}>
              <div className="pt-rs-body">
                <div className="pt-rs-name" style={{ fontWeight: 500 }}>{a.action}</div>
                <div className="pt-rs-meta">
                  {a.admin}{a.target ? ` · ${a.target}` : ""} · {fmtTime(a.timestamp)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// --------------------------------------------------------------- system alerts
export function SystemAlertsSection({ alerts }: { alerts: SystemAlert[] }) {
  const tone: Record<string, BadgeTone> = { info: "gray", warning: "amber", critical: "red" };
  return (
    <section className="pt-panel" aria-labelledby="sys-alerts-h">
      <div className="pt-panel-head">
        <h2 id="sys-alerts-h" className="pt-panel-title"><IconAlert /> System Alerts</h2>
      </div>
      {alerts.length === 0 ? (
        <EmptyState title="No active system alerts." />
      ) : (
        <div>
          {alerts.map((a) => (
            <div key={a.id} className="pt-na-row">
              <div className="pt-na-icon"><IconAlert width={16} height={16} /></div>
              <div className="pt-na-body">
                <div className="pt-na-title">{a.message}</div>
                <div className="pt-na-desc">
                  {a.service} · detected {fmtTime(a.detectedAt)}
                </div>
              </div>
              <span className={`pt-badge pt-badge-${tone[a.severity] ?? "gray"}`}>{a.severity}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- quick actions
export function QuickActionsSection({
  overview,
  onRefresh,
  refreshing,
}: {
  overview: SystemOverview;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const { token } = useAuth();
  const toast = useToast();
  const [confirmClear, setConfirmClear] = useState(false);
  const [busy, setBusy] = useState(false);

  function exportReport() {
    const blob = new Blob([JSON.stringify(overview, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `system-report-${new Date().toISOString().slice(0, 19)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success("System report exported.");
  }

  async function doClear() {
    if (!token) return;
    setBusy(true);
    try {
      const res = await clearAudioCache(token);
      toast.success(res.message || "Audio cache cleared.");
      onRefresh();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Could not clear the audio cache.");
    } finally {
      setBusy(false);
      setConfirmClear(false);
    }
  }

  return (
    <section className="pt-panel" aria-labelledby="sys-qa-h">
      <div className="pt-panel-head">
        <h2 id="sys-qa-h" className="pt-panel-title">Quick Actions</h2>
      </div>
      <div className="pt-row">
        <button type="button" className="pt-btn pt-btn-secondary" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh Health Checks"}
        </button>
        <button type="button" className="pt-btn pt-btn-secondary" onClick={exportReport}>
          Export System Report
        </button>
        <button type="button" className="pt-btn pt-btn-secondary" onClick={() => setConfirmClear(true)}>
          Clear Audio Cache
        </button>
      </div>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        Only safe, real actions are shown. Clearing the audio cache is non-destructive (it only
        drops cached synthesized clips) and is recorded in the admin activity log.
      </p>
      {confirmClear && (
        <ConfirmModal
          title="Clear audio cache?"
          body="This drops all cached synthesized patient audio. Future replies will be re-synthesized on demand. This action is logged."
          confirmLabel="Clear cache"
          busy={busy}
          onConfirm={doClear}
          onCancel={() => setConfirmClear(false)}
        />
      )}
    </section>
  );
}
