import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import { IconRefresh } from "../../../components/admin/icons";
import { ErrorState, LoadingState } from "../../../portal/ui";
import {
  fetchLiveSessions,
  fetchTrafficCapacity,
  fetchTrafficHistory,
  fetchTrafficOverview,
  type HistoryPoint,
  type LiveSession,
  type TrafficCapacity,
  type TrafficOverview,
} from "../../../services/trafficApi";

const REFRESH_MS = 12_000; // ~12s: fresh enough, light enough not to self-load

type HealthTone = "green" | "amber" | "red" | "gray";

function badge(tone: HealthTone, label: string) {
  const cls = { green: "pt-badge-green", amber: "pt-badge-amber", red: "pt-badge-red", gray: "pt-badge-gray" }[tone];
  return <span className={`pt-badge ${cls}`}>{label}</span>;
}

function capacityTone(state: string): HealthTone {
  if (state === "CRITICAL") return "red";
  if (state === "PROTECTING" || state === "BUSY") return "amber";
  return "green";
}

function insightTone(tone: string): HealthTone {
  if (tone === "red") return "red";
  if (tone === "orange" || tone === "yellow") return "amber";
  if (tone === "info") return "gray";
  return "green";
}

function ms(v: number | null | undefined) {
  return v == null ? "—" : `${v} ms`;
}

function pct(v: number | null | undefined) {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtDuration(sec: number) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Dependency-free inline SVG line chart (matches the minimalist admin style). */
function LineChart({ points, series }: { points: HistoryPoint[]; series: { key: keyof HistoryPoint; color: string; label: string }[] }) {
  const W = 640;
  const H = 180;
  const P = 24;
  if (points.length < 2) {
    return <div className="pt-muted" style={{ padding: "var(--space-5)" }}>Collecting data… (the chart fills in as telemetry is sampled)</div>;
  }
  const xs = points.map((_, i) => P + (i / (points.length - 1)) * (W - 2 * P));
  const allVals = series.flatMap((s) => points.map((p) => Number(p[s.key]) || 0));
  const maxV = Math.max(1, ...allVals);
  const y = (v: number) => H - P - (v / maxV) * (H - 2 * P);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Live traffic chart">
      <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="var(--color-border)" />
      {series.map((s) => {
        const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${xs[i].toFixed(1)},${y(Number(p[s.key]) || 0).toFixed(1)}`).join(" ");
        return <path key={String(s.key)} d={d} fill="none" stroke={s.color} strokeWidth={2} />;
      })}
      <text x={P} y={14} fontSize="10" fill="var(--color-text-muted)">{maxV}</text>
    </svg>
  );
}

interface Data {
  overview: TrafficOverview;
  sessions: LiveSession[];
  history: HistoryPoint[];
  capacity: TrafficCapacity;
}

export function TrafficDashboardPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [data, setData] = useState<Data | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [minutes, setMinutes] = useState(60);
  const [showOpenAI, setShowOpenAI] = useState(false);
  const [show429, setShow429] = useState(false);
  const minutesRef = useRef(minutes);
  minutesRef.current = minutes;

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const [overview, sessionsResp, history, capacity] = await Promise.all([
        fetchTrafficOverview(token),
        fetchLiveSessions(token),
        fetchTrafficHistory(token, minutesRef.current),
        fetchTrafficCapacity(token),
      ]);
      setData({ overview, sessions: sessionsResp.sessions, history: history.points, capacity });
      setLastUpdated(new Date());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load traffic telemetry.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [load]);

  if (loading && !data) return <LoadingState label="Loading traffic telemetry…" />;
  if (error && !data) return <ErrorState message={error} onRetry={load} />;
  if (!data) return null;

  const { overview: ov, sessions, history, capacity } = data;
  const cap = ov.openai_capacity;
  const iq = ov.concurrency.interview;
  const chartSeries = [
    { key: "active_users" as const, color: "#8ecb3a", label: "Active users" },
    { key: "http_rpm" as const, color: "#5cc8ff", label: "HTTP req/min" },
    ...(showOpenAI ? [{ key: "openai_rpm" as const, color: "#f5923e", label: "OpenAI req/min" }] : []),
    ...(show429 ? [{ key: "rate_limited" as const, color: "#ef4f8b", label: "429s" }] : []),
  ];

  const serverTone: HealthTone = ov.server.available
    ? (ov.status === "critical" ? "red" : ov.status === "elevated" ? "amber" : "green")
    : "gray";

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>Traffic Control &amp; Scalability</h1>
          <p className="pt-page-sub">
            Monitor live application load, AI service traffic, concurrency, and system capacity.
          </p>
        </div>
        <div className="pt-header-actions">
          <span className="pt-muted" style={{ fontSize: "0.82rem" }}>
            Last updated: {lastUpdated ? lastUpdated.toLocaleTimeString() : "never"}
          </span>
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" onClick={load}>
            <IconRefresh width={15} height={15} /> Refresh
          </button>
        </div>
      </div>

      {error && <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>Some data may be stale: {error}</p>}

      {/* ---- Top status row ---- */}
      <div className="pt-traffic-cards">
        <TC label="Active Users" value={ov.users.active} chip={badge("green", "Live")} />
        <TC label="Live Interviews" value={ov.interviews.active} chip={badge("green", "Active")} sub={`${ov.interviews.waiting_for_ai} waiting for AI`} />
        <TC label="Requests / min" value={ov.http.requests_per_minute} sub={`${ov.http.in_flight} in flight`} />
        <TC label="OpenAI Active" value={ov.openai.active} sub={`${ov.openai.requests_per_minute}/min`} />
        <TC label="ElevenLabs Active" value={ov.elevenlabs.active} sub={`${ov.elevenlabs.requests_per_minute}/min`} />
        <TC label="Capacity State" value={ov.openai_capacity.capacity_state} chip={badge(capacityTone(ov.openai_capacity.capacity_state), `${(ov.openai_capacity.utilization_pct * 100).toFixed(0)}%`)} />
        <TC label="Server Status" value={ov.server.available ? (ov.status === "healthy" ? "Healthy" : ov.status[0].toUpperCase() + ov.status.slice(1)) : "N/A"} chip={badge(serverTone, ov.server.available ? "Live" : "psutil off")} />
      </div>

      {/* ---- Live Session Activity | Live Traffic ---- */}
      <div className="pt-traffic-grid">
        <div className="pt-card">
          <div className="pt-tc-head">
            <h2 className="pt-h2" style={{ margin: 0 }}>Live Session Activity</h2>
            <span className="pt-muted">{sessions.length} active</span>
          </div>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr><th>Student</th><th>Case</th><th>Status</th><th>Time</th><th>AI Latency</th></tr>
              </thead>
              <tbody>
                {sessions.length === 0 && (
                  <tr><td colSpan={5} className="pt-muted">No active interview sessions right now.</td></tr>
                )}
                {sessions.slice(0, 12).map((s) => (
                  <tr key={s.session_id} style={{ cursor: "pointer" }} onClick={() => navigate(`/admin/sessions/${s.session_id}`)}>
                    <td>{s.student_name || s.student_number || "—"}</td>
                    <td>{s.case_name}</td>
                    <td>{sessionStatusChip(s.status)}</td>
                    <td>{fmtDuration(s.duration_seconds)}</td>
                    <td>{ms(s.latest_latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="pt-card">
          <div className="pt-tc-head">
            <h2 className="pt-h2" style={{ margin: 0 }}>OpenAI Capacity &amp; Live Traffic</h2>
            <span className="pt-header-actions">
              {badge(capacityTone(cap.capacity_state), cap.capacity_state)}
              <select className="pt-select" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="Time range">
                <option value={15}>Last 15 minutes</option>
                <option value={30}>Last 30 minutes</option>
                <option value={60}>Last 60 minutes</option>
              </select>
            </span>
          </div>
          <div className="pt-cap-strip">
            <CapCell label="TPM" value={`${cap.tpm_used.toLocaleString()} / ${cap.tpm_limit.toLocaleString()}`} tone={healthTone(cap.tpm_pct * 100, 70, 85)} />
            <CapCell label="RPM" value={`${cap.rpm_used} / ${cap.rpm_limit}`} tone={healthTone(cap.rpm_pct * 100, 70, 85)} />
            <CapCell label="Token Headroom" value={cap.headroom_tokens.toLocaleString()} tone="green" />
            <CapCell label="AI Slots" value={`${iq.active} / ${iq.limit}`} tone={healthTone((iq.active / Math.max(1, iq.limit)) * 100, 70, 90)} />
            <CapCell label="Waiting" value={String(iq.waiting)} tone={iq.waiting > 0 ? "amber" : "green"} />
            <CapCell label="Queue Wait p95" value={ms(iq.wait_p95_ms)} tone={healthTone(iq.wait_p95_ms ?? 0, 1000, 2000)} />
            <CapCell label="429s (5m)" value={String(ov.openai.rate_limits_last_5m)} tone={ov.openai.rate_limits_last_5m > 0 ? "red" : "green"} />
          </div>
          <LineChart points={history} series={chartSeries} />
          <div className="pt-chart-legend">
            <Legend color="#8ecb3a" label="Active users" />
            <Legend color="#5cc8ff" label="HTTP req/min" />
            <label className="pt-legend-toggle"><input type="checkbox" checked={showOpenAI} onChange={(e) => setShowOpenAI(e.target.checked)} /> OpenAI</label>
            <label className="pt-legend-toggle"><input type="checkbox" checked={show429} onChange={(e) => setShow429(e.target.checked)} /> 429s</label>
          </div>
        </div>
      </div>

      {/* ---- AI Provider Traffic | Infrastructure Health ---- */}
      <div className="pt-traffic-grid">
        <div className="pt-card">
          <h2 className="pt-h2">AI Service Traffic</h2>
          <div className="pt-provider-grid">
            <ProviderPanel title="OpenAI" p={ov.openai} extraRows={[
              ["TPM", `${cap.tpm_used.toLocaleString()} / ${cap.tpm_limit.toLocaleString()}`],
              ["RPM", `${cap.rpm_used} / ${cap.rpm_limit}`],
            ]} />
            <ProviderPanel title="ElevenLabs" p={ov.elevenlabs} />
          </div>
        </div>

        <div className="pt-card">
          <h2 className="pt-h2">Infrastructure Health</h2>
          <div className="pt-health-grid">
            {ov.server.available ? (
              <>
                <Health label="CPU" value={`${ov.server.cpu_percent ?? "—"}%`} tone={healthTone(ov.server.cpu_percent, 75, 90)} />
                <Health label="Memory" value={`${ov.server.system_memory_percent ?? "—"}%`} tone={healthTone(ov.server.system_memory_percent, 75, 90)} />
              </>
            ) : (
              <Health label="CPU / Memory" value="Not available" tone="gray" />
            )}
            <Health label="DB Pool" value={dbPoolText(ov.server.db_pool)} tone={dbPoolTone(ov.server.db_pool)} />
            <Health label="API Error Rate" value={pct(ov.http.error_rate)} tone={healthTone(ov.http.error_rate * 100, 1, 5)} />
            <Health label="p95 Latency" value={ms(ov.http.p95_ms)} tone={healthTone(ov.http.p95_ms ?? 0, 1500, 2500)} />
            <Health label="Rate Limited (5m)" value={String(ov.http.rate_limited_last_5m)} tone={ov.http.rate_limited_last_5m > 0 ? "amber" : "green"} />
            <Health label="Uptime" value={fmtUptime(ov.server.uptime_seconds)} tone="green" />
          </div>
        </div>
      </div>

      {/* ---- Assessment Queue / Workers | Traffic Protection ---- */}
      <div className="pt-traffic-grid">
        <div className="pt-card">
          <h2 className="pt-h2">Assessment Queue &amp; Workers</h2>
          {ov.assessment.execution === "background_queue" ? (
            <div className="pt-health-grid">
              <Health label="Queued" value={String(ov.assessment.pending)} tone={ov.assessment.pending > (capacity.assessment_workers * 4) ? "amber" : "green"} />
              <Health label="Processing" value={String(ov.assessment.processing)} tone="green" />
              <Health label="Workers (eff/cfg)" value={`${ov.assessment.effective_workers} / ${ov.assessment.workers}`} tone="gray" />
              <Health label="Oldest Wait" value={ov.assessment.oldest_wait_seconds == null ? "—" : `${ov.assessment.oldest_wait_seconds}s`} tone={healthTone(ov.assessment.oldest_wait_seconds ?? 0, 60, 180)} />
              <Health label="Throttle Mode" value={ov.assessment.throttle_mode} tone={ov.assessment.throttle_mode === "PAUSED" ? "red" : ov.assessment.throttle_mode === "NORMAL" ? "green" : "amber"} />
            </div>
          ) : (
            <div>
              <div className="pt-tc-value">Synchronous</div>
              <p className="pt-muted">Assessment runs in-request (background queue disabled). Set ASSESSMENT_QUEUE_ENABLED=true to queue jobs.</p>
            </div>
          )}
        </div>

        <div className="pt-card">
          <h2 className="pt-h2">Traffic Protection</h2>
          <p className="pt-muted" style={{ marginTop: 0 }}>
            Configured by server environment · restart required to change
          </p>
          <div className="pt-protect-list">
            <Protect label="Rate Limiting" value={capacity.protection.rate_limiting.enabled ? "Enabled" : "Disabled"} tone={capacity.protection.rate_limiting.enabled ? "green" : "gray"} detail={`interview ${capacity.protection.rate_limiting.interview} · voice ${capacity.protection.rate_limiting.voice} · assessment ${capacity.protection.rate_limiting.assessment} · scope ${capacity.protection.rate_limiting.scope}`} />
            <Protect label="Login Throttle" value={capacity.protection.login_throttle.enabled ? "Enabled" : "Disabled"} tone={capacity.protection.login_throttle.enabled ? "green" : "gray"} detail={`${capacity.protection.login_throttle.max_failed_attempts} attempts → ${capacity.protection.login_throttle.lockout_seconds}s lockout`} />
            <Protect label="AI Interview Concurrency" value={`${capacity.protection.interview_concurrency.active} / ${capacity.protection.interview_concurrency.limit}`} tone="green" detail="Bounded semaphore; controlled 503 when saturated" />
            <Protect label="TTS Concurrency" value={`${capacity.protection.tts_concurrency.active} / ${capacity.protection.tts_concurrency.limit}`} tone="green" detail="Degrades to text-only when saturated" />
            <Protect label="Assessment Execution" value={capacity.protection.assessment_execution === "background_queue" ? "Background queue" : "Synchronous"} tone="green" detail={`${capacity.assessment_workers} workers`} />
            <Protect label="Retry / Backoff" value={capacity.protection.retry_backoff.enabled ? "Active" : "Off"} tone="green" detail={`max ${capacity.protection.retry_backoff.max_retries} retries · exp backoff + jitter · honors Retry-After`} />
            <Protect label="Circuit Breaker" value="Not configured" tone="gray" detail="Telemetry + retry cover current failure patterns" />
          </div>
        </div>
      </div>

      {/* ---- Deployment | Operator Insights ---- */}
      <div className="pt-traffic-grid">
        <div className="pt-card">
          <h2 className="pt-h2">Operator Insights</h2>
          <div className="pt-alert-list">
            {ov.insights.length === 0 && <p className="pt-muted">No insights.</p>}
            {ov.insights.map((ins, i) => (
              <div key={i} className="pt-alert-row">
                {badge(insightTone(ins.tone), ins.tone.toUpperCase())}
                <span className="pt-alert-msg">{ins.message}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="pt-card">
          <div className="pt-tc-head">
            <h2 className="pt-h2" style={{ margin: 0 }}>Recent System Alerts</h2>
          </div>
          <div className="pt-alert-list">
            {ov.alerts.length === 0 && <p className="pt-muted">No alerts. All monitored thresholds are within limits.</p>}
            {ov.alerts.map((a, i) => (
              <div key={`${a.key}-${i}`} className="pt-alert-row">
                {badge(a.severity === "CRITICAL" ? "red" : a.severity === "WARNING" ? "amber" : "gray", a.severity)}
                <span className="pt-alert-msg">{a.message}</span>
                <span className="pt-muted pt-alert-time">{new Date(a.ts).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ---- Deployment & Capacity ---- */}
      <div className="pt-traffic-grid">
        <div className="pt-card">
          <h2 className="pt-h2">Deployment &amp; Capacity</h2>
          <div className="pt-health-grid">
            <Health label="Deployment" value={capacity.deployment_mode.replace(/_/g, " ")} tone="gray" />
            <Health label="App Workers" value={String(capacity.app_workers)} tone="gray" />
            <Health label="Max AI Interviews" value={String(capacity.max_ai_interview_concurrency)} tone="gray" />
            <Health label="Max TTS" value={String(capacity.max_tts_concurrency)} tone="gray" />
            <Health label="Assessment Workers" value={String(capacity.assessment_workers)} tone="gray" />
            <Health label="Rate Limiter Scope" value={capacity.rate_limiter_scope.replace(/_/g, " ")} tone="amber" />
          </div>
          <p className="pt-muted" style={{ fontSize: "0.8rem" }}>{capacity.notes.global_rate_limiting}</p>
          <p className="pt-muted" style={{ fontSize: "0.8rem" }}>{capacity.notes.autoscaling}</p>
        </div>

        <div className="pt-card">
          <h2 className="pt-h2">Priority Policy</h2>
          <p className="pt-muted" style={{ marginTop: 0 }}>
            Under load, capacity is protected in this order (live interviews first):
          </p>
          <div className="pt-protect-list">
            <div className="pt-protect-row"><div className="pt-protect-main"><span>1 · Live patient interview</span>{badge("green", "Highest")}</div><div className="pt-muted pt-protect-detail">Interview generation keeps its own (larger) concurrency budget.</div></div>
            <div className="pt-protect-row"><div className="pt-protect-main"><span>2 · TTS / ElevenLabs</span>{badge("amber", "Medium")}</div><div className="pt-muted pt-protect-detail">Degrades to text-only when saturated; never fails the interview.</div></div>
            <div className="pt-protect-row"><div className="pt-protect-main"><span>3 · Assessment</span>{badge("gray", "Backs off")}</div><div className="pt-muted pt-protect-detail">Adaptive workers reduce/pause as OpenAI capacity tightens (current: {ov.assessment.throttle_mode}).</div></div>
          </div>
        </div>
      </div>

      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-4)" }}>
        Telemetry is process-local (per worker) · dashboard refreshes every {REFRESH_MS / 1000}s.
      </p>
    </div>
  );
}

// ------------------------------ small components ------------------------------
function TC({ label, value, sub, chip }: { label: string; value: number | string; sub?: string; chip?: React.ReactNode }) {
  return (
    <div className="pt-card pt-tc">
      <div className="pt-tc-head"><span className="pt-tc-label">{label}</span>{chip}</div>
      <div className="pt-tc-value">{value}</div>
      {sub && <div className="pt-muted pt-tc-sub">{sub}</div>}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return <span className="pt-legend-item"><span className="pt-legend-dot" style={{ background: color }} />{label}</span>;
}

function ProviderPanel({ title, p, extraRows }: { title: string; p: TrafficOverview["openai"]; extraRows?: [string, string][] }) {
  return (
    <div className="pt-provider-panel">
      <div className="pt-tc-head"><strong>{title}</strong>{p.model && <span className="pt-badge pt-badge-gray">{p.model}</span>}</div>
      <Row k="Requests/min" v={String(p.requests_per_minute)} />
      <Row k="Active" v={String(p.active)} />
      <Row k="Success rate" v={p.success_rate == null ? "—" : pct(p.success_rate)} />
      <Row k="429 (5m)" v={String(p.rate_limits_last_5m)} />
      <Row k="Retries (5m)" v={String(p.retries_last_5m)} />
      <Row k="Avg / p95" v={`${ms(p.avg_ms)} / ${ms(p.p95_ms)}`} />
      {extraRows?.map(([k, v]) => <Row key={k} k={k} v={v} />)}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return <div className="pt-kv"><span className="pt-muted">{k}</span><span>{v}</span></div>;
}

function CapCell({ label, value, tone }: { label: string; value: string; tone: HealthTone }) {
  const color = { green: "var(--color-success)", amber: "var(--color-warning)", red: "var(--color-danger)", gray: "var(--color-text-muted)" }[tone];
  return (
    <div className="pt-cap-cell">
      <div className="pt-muted pt-cap-label">{label}</div>
      <div className="pt-cap-value" style={{ color }}>{value}</div>
    </div>
  );
}

function Health({ label, value, tone }: { label: string; value: string; tone: HealthTone }) {
  const color = { green: "var(--color-success)", amber: "var(--color-warning)", red: "var(--color-danger)", gray: "var(--color-text-muted)" }[tone];
  return (
    <div className="pt-health-cell">
      <div className="pt-health-value" style={{ color }}>{value}</div>
      <div className="pt-muted pt-health-label">{label}</div>
    </div>
  );
}

function Protect({ label, value, tone, detail }: { label: string; value: string; tone: HealthTone; detail: string }) {
  return (
    <div className="pt-protect-row">
      <div className="pt-protect-main"><span>{label}</span>{badge(tone, value)}</div>
      <div className="pt-muted pt-protect-detail">{detail}</div>
    </div>
  );
}

function sessionStatusChip(status: string) {
  const map: Record<string, HealthTone> = {
    INTERVIEWING: "green", WAITING_FOR_AI: "amber", STREAMING_RESPONSE: "green",
    STREAMING_AUDIO: "green", ASSESSMENT_PENDING: "amber", COMPLETED: "gray", IDLE: "gray",
  };
  return badge(map[status] ?? "gray", status.replace(/_/g, " "));
}

function healthTone(v: number | null | undefined, warn: number, crit: number): HealthTone {
  if (v == null) return "gray";
  if (v >= crit) return "red";
  if (v >= warn) return "amber";
  return "green";
}

function dbPoolText(pool: TrafficOverview["server"]["db_pool"]): string {
  if (!pool.applicable) return pool.kind === "sqlite" ? "SQLite (local)" : "N/A";
  return `${pool.checked_out}/${(pool.size ?? 0) + (pool.max_overflow || 0)}`;
}

function dbPoolTone(pool: TrafficOverview["server"]["db_pool"]): HealthTone {
  if (!pool.applicable || pool.utilization == null) return "gray";
  if (pool.utilization >= 0.9) return "red";
  if (pool.utilization >= 0.75) return "amber";
  return "green";
}

function fmtUptime(sec: number): string {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
