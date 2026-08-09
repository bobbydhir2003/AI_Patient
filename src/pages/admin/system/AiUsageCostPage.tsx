import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import { ErrorState, LoadingState } from "../../../portal/ui";
import {
  fetchUsageSessions,
  fetchUsageSummary,
  fetchUsageTimeseries,
  type UsageRange,
  type UsageSessions,
  type UsageSummary,
  type UsageTimeseries,
} from "../../../services/usageApi";
import styles from "./AiUsageCostPage.module.css";

const RANGES: UsageRange[] = ["live", "5m", "15m", "1h", "6h", "24h", "7d", "30d"];
const REFRESH_MS = 3000;

function fmtInt(n: number): string {
  return (n ?? 0).toLocaleString();
}
function fmtUsd(n: number, digits = 2): string {
  return `$${(n ?? 0).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}
function agoLabel(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

/** Minimal SVG line chart (matches the Traffic dashboard's hand-rolled style). */
function LineChart({ ts }: { ts: UsageTimeseries | null }) {
  const points = ts?.points ?? [];
  const hasData = points.some((p) => p.total_tokens > 0);
  if (!hasData) {
    return <div className={styles.emptyChart}>No usage recorded for this period.</div>;
  }
  const W = 720;
  const H = 240;
  const pad = 8;
  const max = Math.max(1, ...points.map((p) => p.total_tokens));
  const x = (i: number) => (points.length <= 1 ? pad : pad + (i * (W - pad * 2)) / (points.length - 1));
  const y = (v: number) => H - pad - (v / max) * (H - pad * 2);
  const path = (key: "input_tokens" | "output_tokens" | "total_tokens") =>
    points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Live token usage chart">
      <path d={path("input_tokens")} fill="none" stroke="#4ade80" strokeWidth={2} />
      <path d={path("output_tokens")} fill="none" stroke="#c084fc" strokeWidth={2} />
      <path d={path("total_tokens")} fill="none" stroke="#ff5056" strokeWidth={2} />
    </svg>
  );
}

/** SVG donut for the cost-by-provider split. Only rendered with real cost > 0. */
function Donut({ summary }: { summary: UsageSummary }) {
  const { openai_usd, elevenlabs_usd, openai_pct, elevenlabs_pct } = summary.provider_split;
  const total = summary.total_cost_usd;
  if (total <= 0) {
    return <div className={styles.emptyChart}>No cost recorded for this period.</div>;
  }
  const R = 60;
  const C = 2 * Math.PI * R;
  const openaiLen = (openai_pct / 100) * C;
  return (
    <div className={styles.donutWrap}>
      <svg viewBox="0 0 160 160" width="150" height="150" role="img" aria-label="Cost by provider">
        <circle cx="80" cy="80" r={R} fill="none" stroke="#7c3aed" strokeWidth="18" />
        <circle
          cx="80" cy="80" r={R} fill="none" stroke="#22c55e" strokeWidth="18"
          strokeDasharray={`${openaiLen} ${C - openaiLen}`} strokeDashoffset={C / 4} transform="rotate(-90 80 80)"
        />
        <text x="80" y="76" textAnchor="middle" className={styles.donutValue}>{fmtUsd(total)}</text>
        <text x="80" y="94" textAnchor="middle" className={styles.donutSub}>Total Spend</text>
      </svg>
      <div className={styles.donutLegend}>
        <div><span className={styles.dotGreen} /> OpenAI <strong>{fmtUsd(openai_usd)}</strong> <em>({openai_pct}%)</em></div>
        <div><span className={styles.dotPurple} /> ElevenLabs <strong>{fmtUsd(elevenlabs_usd)}</strong> <em>({elevenlabs_pct}%)</em></div>
      </div>
    </div>
  );
}

function Card({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className={`${styles.card} ${tone ? styles[tone] : ""}`}>
      <span className={styles.cardLabel}>{label}</span>
      <span className={styles.cardValue}>{value}</span>
      {hint && <span className={styles.cardHint}>{hint}</span>}
    </div>
  );
}

export function AiUsageCostPage() {
  const { token } = useAuth();
  const [range, setRange] = useState<UsageRange>("today");
  const rangeRef = useRef(range);
  rangeRef.current = range;
  const [autoRefresh, setAutoRefresh] = useState(true);

  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [ts, setTs] = useState<UsageTimeseries | null>(null);
  const [sessions, setSessions] = useState<UsageSessions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [, forceTick] = useState(0);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const r = rangeRef.current;
      const [s, t, se] = await Promise.all([
        fetchUsageSummary(token, r),
        fetchUsageTimeseries(token, r),
        fetchUsageSessions(token, r, 10),
      ]);
      setSummary(s);
      setTs(t);
      setSessions(se);
      setLastUpdated(Date.now());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load usage data.");
    }
  }, [token]);

  useEffect(() => { void load(); }, [load, range]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(id);
  }, [autoRefresh, load]);

  // Re-render the "last updated Xs ago" label every second.
  useEffect(() => {
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (!summary && !error) return <LoadingState label="Loading AI usage…" />;
  if (!summary && error) return <ErrorState message={error} onRetry={() => void load()} />;
  const s = summary!;
  const live = autoRefresh;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>
            Real-Time AI Usage &amp; Cost
            {live && <span className={styles.livePill}><span className={styles.liveDot} /> Live</span>}
          </h1>
          <p className={styles.subtitle}>Streaming live token usage, costs, and provider health.</p>
        </div>
        <div className={styles.headerRight}>
          <div className={styles.ranges} role="tablist" aria-label="Time range">
            {RANGES.map((r) => (
              <button key={r} className={`${styles.rangeBtn} ${range === r ? styles.rangeActive : ""}`}
                onClick={() => setRange(r)} role="tab" aria-selected={range === r}>
                {r === "live" ? "Live" : r}
              </button>
            ))}
          </div>
          <label className={styles.autoRefresh}>
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            Auto-refresh
          </label>
          <span className={styles.updated}>Updated {lastUpdated ? agoLabel(new Date(lastUpdated).toISOString()) : "—"}</span>
        </div>
      </div>

      {error && <p className={styles.inlineError} role="alert">{error}</p>}

      {/* Top cards */}
      <div className={styles.cards}>
        <Card tone="toneGreen" label="OpenAI Input Tokens (Total)" value={fmtInt(s.input_tokens)} hint="Selected range" />
        <Card tone="tonePurple" label="OpenAI Output Tokens (Total)" value={fmtInt(s.output_tokens)} hint="Selected range" />
        <Card tone="toneRed" label="Total OpenAI Tokens" value={fmtInt(s.total_tokens)} hint="Input + Output" />
        <Card tone="toneBlue" label="Avg Tokens / Interview" value={fmtInt(s.avg_tokens_per_interview)} hint={`${s.session_count} interview${s.session_count === 1 ? "" : "s"}`} />
        <Card tone="toneAmber" label="Avg Cost / Interview" value={fmtUsd(s.avg_cost_per_interview_usd, 4)} hint="Est. usage cost" />
        <Card tone="toneGreen" label="Total Cost Today" value={fmtUsd(s.total_cost_today_usd)} hint="Estimated usage cost" />
      </div>

      {/* Formula strip */}
      <div className={styles.formula}>
        <div className={styles.formulaText}>
          <strong>Total OpenAI Tokens = Input + Output</strong>
          <span>The total number of OpenAI tokens used is the sum of input tokens (prompts) and output tokens (model responses).</span>
        </div>
        <div className={styles.formulaCalc}>
          <span><em>Input</em><br />{fmtInt(s.input_tokens)}</span>
          <span className={styles.op}>+</span>
          <span><em>Output</em><br />{fmtInt(s.output_tokens)}</span>
          <span className={styles.op}>=</span>
          <span className={styles.totalCol}><em>Total</em><br />{fmtInt(s.total_tokens)}</span>
        </div>
      </div>

      {/* Middle row */}
      <div className={styles.midGrid}>
        <section className={styles.panel}>
          <div className={styles.panelHead}><h2>Live Token Usage Stream (OpenAI)</h2></div>
          <div className={styles.legend}>
            <span><i className={styles.dotGreen} /> Input</span>
            <span><i className={styles.dotPurple} /> Output</span>
            <span><i className={styles.dotRed} /> Total</span>
          </div>
          <LineChart ts={ts} />
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHead}><h2>Cost by Provider</h2></div>
          <Donut summary={s} />
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHead}><h2>Provider Health</h2></div>
          <ul className={styles.health}>
            <li>
              <span>OpenAI API</span>
              <span className={s.providers.openai.configured ? styles.ok : styles.warn}>
                {s.providers.openai.configured ? "Configured" : "Not configured"}
              </span>
            </li>
            <li>
              <span>ElevenLabs API</span>
              <span className={s.providers.elevenlabs.configured ? styles.ok : styles.warn}>
                {s.providers.elevenlabs.configured ? "Configured" : "Not configured"}
              </span>
            </li>
            <li><span>Last OpenAI event</span><span>{agoLabel(s.providers.openai.last_event_at)}</span></li>
            <li><span>Last ElevenLabs event</span><span>{agoLabel(s.providers.elevenlabs.last_event_at)}</span></li>
            <li className={styles.projRow}>
              <span>Projected Monthly Cost</span>
              <span>{s.projected_monthly.available ? fmtUsd(s.projected_monthly.projected_usd ?? 0) : "—"}</span>
            </li>
            {!s.projected_monthly.available && (
              <li className={styles.projNote}>{s.projected_monthly.message}</li>
            )}
          </ul>
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHead}><h2>Per Interview Usage</h2></div>
          <dl className={styles.avgList}>
            <div><dt>Avg OpenAI Input Tokens</dt><dd>{fmtInt(s.avg_input_tokens_per_interview)}</dd></div>
            <div><dt>Avg OpenAI Output Tokens</dt><dd>{fmtInt(s.avg_output_tokens_per_interview)}</dd></div>
            <div><dt>Avg Total OpenAI Tokens</dt><dd>{fmtInt(s.avg_tokens_per_interview)}</dd></div>
            <div><dt>Avg ElevenLabs Characters</dt><dd>{fmtInt(s.avg_elevenlabs_chars_per_interview)}</dd></div>
            <div><dt>Avg Cost / Interview</dt><dd>{fmtUsd(s.avg_cost_per_interview_usd, 4)}</dd></div>
          </dl>
        </section>
      </div>

      {/* Live per-interview table */}
      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <h2>Live Per-Interview Token Usage</h2>
          <span className={styles.count}>
            {sessions ? `Showing ${sessions.sessions.length} of ${sessions.total} interview${sessions.total === 1 ? "" : "s"}` : ""}
          </span>
        </div>
        {sessions && sessions.sessions.length === 0 ? (
          <div className={styles.emptyChart}>No interviews with usage data yet.</div>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Student</th><th>Patient Case</th><th>Session ID</th>
                  <th className={styles.num}>In Tokens</th><th className={styles.num}>Out Tokens</th>
                  <th className={styles.num}>Total OpenAI</th><th className={styles.num}>EL Characters</th>
                  <th className={styles.num}>OpenAI Cost</th><th className={styles.num}>EL Cost</th>
                  <th className={styles.num}>Total Cost</th><th>Last Updated</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(sessions?.sessions ?? []).map((row) => (
                  <tr key={row.session_id}>
                    <td>{row.student_name}</td>
                    <td>{row.case_id}</td>
                    <td className={styles.mono}>{row.session_id.slice(0, 12)}</td>
                    <td className={styles.num}>{fmtInt(row.input_tokens)}</td>
                    <td className={styles.num}>{fmtInt(row.output_tokens)}</td>
                    <td className={styles.num}>{fmtInt(row.total_tokens)}</td>
                    <td className={styles.num}>{fmtInt(row.elevenlabs_characters)}</td>
                    <td className={styles.num}>{fmtUsd(row.openai_cost_usd, 4)}</td>
                    <td className={styles.num}>{fmtUsd(row.elevenlabs_cost_usd, 4)}</td>
                    <td className={styles.num}>{fmtUsd(row.total_cost_usd, 4)}</td>
                    <td>{agoLabel(row.last_updated)}</td>
                    <td>
                      <span className={row.status === "active" ? styles.badgeLive : styles.badgeDone}>
                        {row.status === "active" ? "Live" : row.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className={styles.footer}>
        Costs are ESTIMATED usage costs from provider-reported usage × pricing v{s.pricing.version}
        {" — "}OpenAI ${s.pricing.openai.default_input_per_1k}/1K input, ${s.pricing.openai.default_output_per_1k}/1K output;
        {" "}ElevenLabs ${s.pricing.elevenlabs.per_character}/character. Not a provider invoice.
      </p>
    </div>
  );
}
