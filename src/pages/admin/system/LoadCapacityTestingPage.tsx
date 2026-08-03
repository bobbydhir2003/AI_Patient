import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import { IconRefresh } from "../../../components/admin/icons";
import { ErrorState, LoadingState, useToast } from "../../../portal/ui";
import {
  createJob,
  getActiveJob,
  getJobMetrics,
  getLoadTestConfig,
  getRecentJobs,
  stopJob,
  type CapacityAnalysis,
  type LoadTestConfig,
  type LoadTestJob,
  type MetricsResponse,
  type MetricsSample,
} from "../../../services/loadTestApi";

const POLL_MS = 3000; // live metrics poll while a test runs

const TEST_TYPE_LABELS: Record<string, string> = {
  smoke: "Smoke",
  concurrent: "Concurrent Student Simulation",
  ramp: "Ramp",
  spike: "Spike",
  stress: "Stress (bounded)",
  soak: "Soak (capability)",
  ai_traffic: "AI Traffic",
  tts_traffic: "TTS Traffic",
};

const PROVIDER_LABELS: Record<string, string> = {
  SIMULATED_AI: "Simulated AI (no provider spend)",
  REAL_OPENAI: "Real OpenAI",
  REAL_OPENAI_TTS: "Real OpenAI + ElevenLabs",
};

const REAL_MODES = new Set(["REAL_OPENAI", "REAL_OPENAI_TTS"]);

// Quick profiles (target users / duration seconds). These only pre-fill the
// form; nothing runs until the operator clicks Start.
const QUICK_PROFILES: { key: string; label: string; testType: string; users: number; duration: number; ramp: number }[] = [
  { key: "quick10", label: "Quick 10 (2 min)", testType: "smoke", users: 10, duration: 120, ramp: 15 },
  { key: "classroom", label: "Classroom 20 (5 min)", testType: "concurrent", users: 20, duration: 300, ramp: 30 },
  { key: "fullclass", label: "Full Class 70", testType: "concurrent", users: 70, duration: 300, ramp: 60 },
  { key: "stress", label: "Stress (bounded)", testType: "stress", users: 100, duration: 300, ramp: 120 },
  { key: "soak", label: "Soak (long)", testType: "soak", users: 20, duration: 1800, ramp: 60 },
];

const ACTIVE_STATUSES = new Set(["PENDING", "STARTING", "RUNNING", "STOPPING"]);

function num(v: number | null | undefined, suffix = ""): string {
  return v == null ? "Not available" : `${v}${suffix}`;
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    PASS: "pt-badge-green", COMPLETED: "pt-badge-green", RUNNING: "pt-badge-green",
    PASS_WITH_WARNING: "pt-badge-amber", STOPPING: "pt-badge-amber", STARTING: "pt-badge-amber",
    PENDING: "pt-badge-amber", FAIL: "pt-badge-red", FAILED: "pt-badge-red",
    INCONCLUSIVE: "pt-badge-gray", CANCELLED: "pt-badge-gray",
  };
  return <span className={`pt-badge ${map[status] ?? "pt-badge-gray"}`}>{status.replace(/_/g, " ")}</span>;
}

/** Dependency-free inline chart over real measured samples. */
function SampleChart({ samples, keys }: { samples: MetricsSample[]; keys: { key: keyof MetricsSample; color: string; label: string }[] }) {
  const W = 640, H = 180, P = 26;
  const pts = samples.filter((s) => s != null);
  if (pts.length < 2) {
    return <div className="pt-muted" style={{ padding: "var(--space-5)" }}>Collecting data… the chart fills in from real samples as the test runs.</div>;
  }
  const xs = pts.map((_, i) => P + (i / (pts.length - 1)) * (W - 2 * P));
  const allVals = keys.flatMap((s) => pts.map((p) => Number(p[s.key]) || 0));
  const maxV = Math.max(1, ...allVals);
  const y = (v: number) => H - P - (v / maxV) * (H - 2 * P);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Load test chart">
      <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="var(--color-border)" />
      {keys.map((s) => {
        const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${xs[i].toFixed(1)},${y(Number(p[s.key]) || 0).toFixed(1)}`).join(" ");
        return <path key={String(s.key)} d={d} fill="none" stroke={s.color} strokeWidth={2} />;
      })}
      <text x={P} y={14} fontSize="10" fill="var(--color-text-muted)">{Math.round(maxV)}</text>
    </svg>
  );
}

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="pt-card pt-tc">
      <div className="pt-tc-head"><span className="pt-tc-label">{label}</span></div>
      <div className="pt-tc-value">{value}</div>
      {sub && <div className="pt-muted pt-tc-sub">{sub}</div>}
    </div>
  );
}

export function LoadCapacityTestingPage() {
  const { token } = useAuth();
  const toast = useToast();
  const [cfg, setCfg] = useState<LoadTestConfig | null>(null);
  const [recent, setRecent] = useState<LoadTestJob[]>([]);
  const [activeJob, setActiveJob] = useState<LoadTestJob | null>(null);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // form state
  const [testType, setTestType] = useState("smoke");
  const [providerMode, setProviderMode] = useState("SIMULATED_AI");
  const [targetUsers, setTargetUsers] = useState(10);
  const [rampSeconds, setRampSeconds] = useState(15);
  const [durationSeconds, setDurationSeconds] = useState(120);

  const activeId = activeJob && ACTIVE_STATUSES.has(activeJob.status) ? activeJob.id : null;
  const activeIdRef = useRef<string | null>(activeId);
  activeIdRef.current = activeId;

  const loadStatic = useCallback(async () => {
    if (!token) return;
    try {
      const [c, r, a] = await Promise.all([
        getLoadTestConfig(token), getRecentJobs(token), getActiveJob(token),
      ]);
      setCfg(c);
      setRecent(r.jobs);
      setActiveJob(a.job);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load load-test configuration.");
    }
  }, [token]);

  useEffect(() => { loadStatic(); }, [loadStatic]);

  // Poll live metrics only while a test is active.
  useEffect(() => {
    if (!token || !activeId) return;
    let stop = false;
    const tick = async () => {
      try {
        const m = await getJobMetrics(token, activeId);
        if (stop) return;
        setMetrics(m);
        if (!ACTIVE_STATUSES.has(m.job.status)) {
          setActiveJob(m.job);
          loadStatic();
        }
      } catch {
        /* transient; keep last */
      }
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { stop = true; window.clearInterval(id); };
  }, [token, activeId, loadStatic]);

  function applyProfile(p: typeof QUICK_PROFILES[number]) {
    setTestType(p.testType);
    setTargetUsers(p.users);
    setDurationSeconds(p.duration);
    setRampSeconds(p.ramp);
  }

  async function start() {
    if (!token) return;
    const isReal = REAL_MODES.has(providerMode);
    if (isReal) {
      const ok = window.confirm(
        `⚠ REAL PROVIDER COST WARNING\n\n"${PROVIDER_LABELS[providerMode]}" sends real, billable traffic to ` +
        `${providerMode === "REAL_OPENAI_TTS" ? "OpenAI and ElevenLabs" : "OpenAI"} for ` +
        `${targetUsers} virtual students over ${durationSeconds}s. This will incur provider charges.\n\n` +
        `Continue and start paid traffic?`,
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      const job = await createJob(token, {
        testType, providerMode, targetUsers, rampSeconds, durationSeconds,
        confirmRealProvider: isReal,
      });
      setActiveJob(job);
      setMetrics(null);
      toast.success("Load test started.");
      loadStatic();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Could not start load test.");
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!token || !activeId) return;
    setBusy(true);
    try {
      await stopJob(token, activeId);
      toast.success("Stopping load test…");
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Could not stop load test.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !cfg) return <ErrorState message={error} onRetry={loadStatic} />;
  if (!cfg) return <LoadingState label="Loading load & capacity testing…" />;

  const running = activeJob != null && ACTIVE_STATUSES.has(activeJob.status);
  const ov = metrics?.overall ?? null;
  const cap = metrics?.capacity ?? null;
  const series = metrics?.series ?? [];
  const isReal = REAL_MODES.has(providerMode);

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>Load &amp; Capacity Testing</h1>
          <p className="pt-page-sub">
            Drive realistic virtual-student traffic in a separate process and read real, measured
            capacity. Every value below is a live measurement — nothing is simulated in the UI.
          </p>
        </div>
        <div className="pt-header-actions">
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" onClick={loadStatic}>
            <IconRefresh width={15} height={15} /> Refresh
          </button>
        </div>
      </div>

      {!cfg.enabled && (
        <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>
          Load &amp; capacity testing is disabled by configuration.
        </p>
      )}

      {/* ---------- Configuration bar ---------- */}
      <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">Configure test</h2>
          <span className="pt-muted" style={{ fontSize: "0.8rem" }}>
            {cfg.environment} · target {cfg.targetBaseUrl} · caps: {cfg.maxUsers} users / {Math.round(cfg.maxDurationSeconds / 60)} min
          </span>
        </div>

        <div className="pt-row" style={{ gap: 8, flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
          {QUICK_PROFILES.map((p) => (
            <button key={p.key} type="button" className="pt-btn pt-btn-secondary pt-btn-sm"
              disabled={running} onClick={() => applyProfile(p)}>{p.label}</button>
          ))}
        </div>

        <div className="pt-row" style={{ gap: "var(--space-4)", alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="pt-field" style={{ marginBottom: 0 }}>
            <span>Test type</span>
            <select className="pt-select" value={testType} disabled={running}
              onChange={(e) => setTestType(e.target.value)}>
              {cfg.testTypes.map((t) => <option key={t} value={t}>{TEST_TYPE_LABELS[t] ?? t}</option>)}
            </select>
          </label>
          <label className="pt-field" style={{ marginBottom: 0 }}>
            <span>Provider mode</span>
            <select className="pt-select" value={providerMode} disabled={running}
              onChange={(e) => setProviderMode(e.target.value)}>
              {cfg.providerModes.map((m) => <option key={m} value={m}>{PROVIDER_LABELS[m] ?? m}</option>)}
            </select>
          </label>
          <label className="pt-field" style={{ marginBottom: 0, maxWidth: 120 }}>
            <span>Virtual users</span>
            <input className="pt-input" type="number" min={1} max={cfg.maxUsers} value={targetUsers}
              disabled={running} onChange={(e) => setTargetUsers(Number(e.target.value))} />
          </label>
          <label className="pt-field" style={{ marginBottom: 0, maxWidth: 120 }}>
            <span>Ramp (s)</span>
            <input className="pt-input" type="number" min={0} value={rampSeconds}
              disabled={running} onChange={(e) => setRampSeconds(Number(e.target.value))} />
          </label>
          <label className="pt-field" style={{ marginBottom: 0, maxWidth: 130 }}>
            <span>Duration (s)</span>
            <input className="pt-input" type="number" min={1} max={cfg.maxDurationSeconds} value={durationSeconds}
              disabled={running} onChange={(e) => setDurationSeconds(Number(e.target.value))} />
          </label>
          <div className="pt-row">
            {running ? (
              <button className="pt-btn pt-btn-sm" onClick={stop} disabled={busy}>
                {busy ? "Stopping…" : "Stop test"}
              </button>
            ) : (
              <button className="pt-btn pt-btn-sm" onClick={start} disabled={busy || !cfg.enabled}>
                {busy ? "Starting…" : "Start test"}
              </button>
            )}
          </div>
        </div>

        {isReal && !running && (
          <p className="pt-error-text" style={{ marginTop: "var(--space-3)", fontSize: "0.85rem" }}>
            ⚠ This provider mode sends real, billable traffic. You'll be asked to confirm the cost before it starts.
          </p>
        )}
      </section>

      {/* ---------- Live run ---------- */}
      {running && activeJob && (
        <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
          <div className="pt-panel-head">
            <h2 className="pt-panel-title">
              Live run — {TEST_TYPE_LABELS[activeJob.testType] ?? activeJob.testType} {statusBadge(activeJob.status)}
            </h2>
            <span className="pt-muted" style={{ fontSize: "0.8rem" }}>
              {PROVIDER_LABELS[activeJob.providerMode] ?? activeJob.providerMode}
              {metrics?.elapsedSeconds != null ? ` · ${Math.round(metrics.elapsedSeconds)}s elapsed` : ""}
            </span>
          </div>

          <div className="pt-traffic-cards">
            <MetricCard label="Virtual Users"
              value={`${series.at(-1)?.activeUsers ?? 0} / ${activeJob.targetUsers}`} sub="active / target" />
            <MetricCard label="Requests / sec" value={num(series.at(-1)?.requestsPerSec)} />
            <MetricCard label="Success Rate" value={ov?.successRate != null ? `${ov.successRate}%` : "Not available"}
              sub={ov ? `${ov.success}/${ov.requests}` : undefined} />
            <MetricCard label="Failed" value={num(ov?.failed)} sub={ov ? `${ov.networkErrors} network` : undefined} />
            <MetricCard label="p50 / p95 / p99"
              value={ov ? `${num(ov.latencyMs.p50)} / ${num(ov.latencyMs.p95)} / ${num(ov.latencyMs.p99)}` : "Not available"}
              sub="ms" />
            <MetricCard label="Peak Concurrency" value={num(ov?.maxActiveUsers)} />
          </div>

          <div className="pt-card" style={{ marginTop: "var(--space-3)" }}>
            <div className="pt-tc-head"><h3 className="pt-h2" style={{ margin: 0 }}>User load & throughput</h3></div>
            <SampleChart samples={series} keys={[
              { key: "activeUsers", color: "#8ecb3a", label: "Active users" },
              { key: "requestsPerSec", color: "#5cc8ff", label: "Req/sec" },
              { key: "p95", color: "#f5923e", label: "p95 ms" },
            ]} />
          </div>

          {metrics?.telemetry && <TelemetryBlocks telemetry={metrics.telemetry} />}
        </section>
      )}

      {/* ---------- Result / capacity analysis ---------- */}
      {!running && metrics && cap && (
        <section className="pt-panel" style={{ marginBottom: "var(--space-4)" }}>
          <div className="pt-panel-head">
            <h2 className="pt-panel-title">Result {statusBadge(cap.overallStatus)}</h2>
            <span className="pt-muted" style={{ fontSize: "0.8rem" }}>
              {metrics.job.testType} · {PROVIDER_LABELS[metrics.job.providerMode] ?? metrics.job.providerMode}
            </span>
          </div>
          <CapacityReport cap={cap} />
          {series.length > 1 && (
            <div className="pt-card" style={{ marginTop: "var(--space-3)" }}>
              <div className="pt-tc-head"><h3 className="pt-h2" style={{ margin: 0 }}>Measured samples</h3></div>
              <SampleChart samples={series} keys={[
                { key: "activeUsers", color: "#8ecb3a", label: "Active users" },
                { key: "requestsPerSec", color: "#5cc8ff", label: "Req/sec" },
                { key: "p95", color: "#f5923e", label: "p95 ms" },
              ]} />
            </div>
          )}
        </section>
      )}

      {/* ---------- Recent runs ---------- */}
      <section className="pt-panel">
        <div className="pt-panel-head">
          <h2 className="pt-panel-title">Recent Test Runs</h2>
        </div>
        <div className="pt-table-wrap">
          <table className="pt-table">
            <thead>
              <tr><th>Started</th><th>Type</th><th>Provider</th><th>Users</th><th>Duration</th><th>Status</th><th>By</th></tr>
            </thead>
            <tbody>
              {recent.length === 0 && (
                <tr><td colSpan={7} className="pt-muted">No load tests have been run yet.</td></tr>
              )}
              {recent.map((j) => (
                <tr key={j.id}>
                  <td>{j.startedAt ? new Date(j.startedAt).toLocaleString() : (j.createdAt ? new Date(j.createdAt).toLocaleString() : "—")}</td>
                  <td>{TEST_TYPE_LABELS[j.testType] ?? j.testType}</td>
                  <td>{j.providerMode === "SIMULATED_AI" ? "Simulated" : (j.providerMode === "REAL_OPENAI_TTS" ? "Real +TTS" : "Real")}</td>
                  <td>{j.targetUsers}</td>
                  <td>{j.durationSeconds}s</td>
                  <td>{statusBadge(j.status)}</td>
                  <td className="pt-muted">{j.createdBy}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function CapacityReport({ cap }: { cap: CapacityAnalysis }) {
  const s = cap.summary;
  const safe = cap.recommendedSafeCapacity;
  return (
    <div>
      <dl className="pt-kv">
        <dt>Overall status</dt><dd>{cap.overallStatus.replace(/_/g, " ")}</dd>
        <dt>Assessment</dt><dd>{cap.reason}</dd>
        <dt>Observed bottleneck</dt>
        <dd>{cap.observedBottleneck.observed ? cap.observedBottleneck.detail : "None observed within the tested range."}</dd>
        <dt>Recommended safe capacity</dt>
        <dd>{safe.value == null ? "Not available" : `${safe.value} concurrent virtual students`} — {safe.reason}</dd>
        <dt>Total requests</dt><dd>{num(s.totalRequests)}</dd>
        <dt>Success rate</dt><dd>{s.successRatePct == null ? "Not available" : `${s.successRatePct}%`}</dd>
        <dt>p95 / p99</dt><dd>{num(s.p95Ms, " ms")} / {num(s.p99Ms, " ms")}</dd>
        <dt>Peak concurrency</dt><dd>{num(s.peakConcurrency)}</dd>
        <dt>429 / 503 / 5xx / network</dt>
        <dd>{s.rateLimited429} / {s.overloaded503} / {s.serverErrors5xx} / {s.networkErrors}</dd>
      </dl>
      <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-3)" }}>
        Safe capacity is derived from the observed stable region of this run, not a preset number.
        Thresholds used: ≥{cap.criteria.healthySuccessPct}% window success, p95 ≤ {cap.criteria.warnP95Ms} ms.
      </p>
    </div>
  );
}

function TelemetryBlocks({ telemetry }: { telemetry: MetricsResponse["telemetry"] }) {
  const oa = telemetry.providers.openai;
  const el = telemetry.providers.elevenlabs;
  const srv = telemetry.infrastructure.server as Record<string, unknown>;
  const pool = telemetry.infrastructure.dbPool as Record<string, unknown>;
  const cpu = srv.available ? `${srv.cpu_percent}%` : "Not available";
  const mem = srv.available ? `${srv.process_memory_mb} MB` : "Not available";
  return (
    <div className="pt-traffic-grid" style={{ marginTop: "var(--space-3)" }}>
      <div className="pt-card">
        <div className="pt-tc-head"><h3 className="pt-h2" style={{ margin: 0 }}>Provider Activity</h3></div>
        <dl className="pt-kv">
          <dt>OpenAI req/min</dt><dd>{num((oa.requests_per_minute as number) ?? null)}</dd>
          <dt>OpenAI success</dt><dd>{oa.success_rate == null ? "Not available" : `${((oa.success_rate as number) * 100).toFixed(1)}%`}</dd>
          <dt>ElevenLabs req/min</dt><dd>{num((el.requests_per_minute as number) ?? null)}</dd>
        </dl>
        <p className="pt-muted" style={{ fontSize: "0.75rem" }}>From the backend's own real provider telemetry.</p>
      </div>
      <div className="pt-card">
        <div className="pt-tc-head"><h3 className="pt-h2" style={{ margin: 0 }}>Infrastructure Health</h3></div>
        <dl className="pt-kv">
          <dt>CPU</dt><dd>{cpu}</dd>
          <dt>Process memory</dt><dd>{mem}</dd>
          <dt>Uptime</dt><dd>{srv.uptime_seconds != null ? `${srv.uptime_seconds}s` : "Not available"}</dd>
          <dt>DB pool</dt><dd>{pool.applicable ? `${pool.checked_out}/${pool.size} in use` : String(pool.note ?? "Not applicable")}</dd>
          <dt>HTTP in flight</dt><dd>{telemetry.infrastructure.httpInFlight}</dd>
        </dl>
      </div>
    </div>
  );
}
