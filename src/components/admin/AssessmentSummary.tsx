import { useNavigate } from "react-router-dom";
import type { AssessmentLevelCount } from "../../services/authApi";
import { levelColor } from "../../portal/ui";
import { EmptyState } from "../../portal/ui";

const R = 52;
const STROKE = 20;
const CIRC = 2 * Math.PI * R;

export function AssessmentSummary({ levels }: { levels: AssessmentLevelCount[] }) {
  const navigate = useNavigate();
  const total = levels.reduce((sum, l) => sum + l.count, 0);

  return (
    <section className="pt-panel" aria-labelledby="assess-title">
      <div className="pt-panel-head">
        <h2 className="pt-panel-title" id="assess-title">
          Assessment Summary
        </h2>
        <button className="pt-panel-link" onClick={() => navigate("/admin/assessments")}>
          View Assessments
        </button>
      </div>

      {total === 0 ? (
        <EmptyState title="No assessments yet" hint="Assessment levels will appear here once sessions are assessed." />
      ) : (
        <div className="pt-assess">
          <div className="pt-donut" style={{ width: 148, height: 148 }}>
            <svg width="148" height="148" viewBox="0 0 148 148" role="img" aria-label={`${total} assessments by level`}>
              <g transform="rotate(-90 74 74)">
                <circle cx="74" cy="74" r={R} fill="none" stroke="var(--color-bg-elevated)" strokeWidth={STROKE} />
                {(() => {
                  let offset = 0;
                  return levels.map((l) => {
                    const len = total > 0 ? (l.count / total) * CIRC : 0;
                    const seg = (
                      <circle
                        key={l.level}
                        cx="74"
                        cy="74"
                        r={R}
                        fill="none"
                        stroke={levelColor(l.level)}
                        strokeWidth={STROKE}
                        strokeDasharray={`${len} ${CIRC - len}`}
                        strokeDashoffset={-offset}
                      >
                        <title>{`${l.level}: ${l.count}`}</title>
                      </circle>
                    );
                    offset += len;
                    return seg;
                  });
                })()}
              </g>
            </svg>
            <div className="pt-donut-center">
              <span className="num">{total}</span>
              <span className="lbl">Total</span>
            </div>
          </div>

          <ul className="pt-legend" aria-label="Assessment levels">
            {levels.map((l) => {
              const pct = total > 0 ? ((l.count / total) * 100).toFixed(1) : "0";
              return (
                <li className="pt-legend-row" key={l.level}>
                  <span className="pt-legend-dot" style={{ background: levelColor(l.level) }} />
                  <span className="nm">{l.level}</span>
                  <span className="ct">
                    {l.count} ({pct}%)
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
