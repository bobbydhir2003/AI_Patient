import { useId } from "react";
import type { PavingProfile } from "../../types/case";

interface PavingRadarProps {
  profile: PavingProfile;
  patientName: string;
  /** Larger geometry (labels/markers) for the enlarged modal. */
  large?: boolean;
  /** Instructional example mode: adds Center / Higher-range labels and three
   * high-contrast dashed arrows pointing to real chart elements. Used only by
   * the info modal with the fixed DEFAULT_PAVING_EXAMPLE. */
  annotated?: boolean;
}

const R = 150; // data radius
const RINGS = 5; // grid rings at 5, 10, 15, 20, 25

const FILL = "rgba(92, 200, 255, 0.28)";
const STROKE = "#5cc8ff";
const GRID = "rgba(255, 255, 255, 0.16)";
const AXIS = "rgba(255, 255, 255, 0.12)";
const SCALE_TEXT = "rgba(255, 255, 255, 0.55)";
const ARROW = "#e6e8ec"; // high-contrast light gray for leader lines/arrowheads

export function PavingRadar({ profile, patientName, large = false, annotated = false }: PavingRadarProps) {
  const uid = useId();
  const cats = profile.categories;
  const n = cats.length || 12;
  const max = profile.maxValue || 25;
  const step = 360 / n;
  const angleFor = (i: number) => -90 + i * step;

  // Annotated mode uses a wider canvas so the callout boxes sit outside the
  // chart and arrows stay inside one coordinate space (always aligned, never
  // overflowing regardless of screen size).
  const VBW = annotated ? 660 : 460;
  const VBH = annotated ? 560 : 460;
  const CX = annotated ? 300 : 230;
  const CY = annotated ? 250 : 230;

  const polar = (angleDeg: number, radius: number): [number, number] => {
    const a = (angleDeg * Math.PI) / 180;
    return [CX + radius * Math.cos(a), CY + radius * Math.sin(a)];
  };

  const points = cats.map((c, i) => {
    const v = c.value ?? 0;
    const r = (Math.max(0, Math.min(max, v)) / max) * R;
    return polar(angleFor(i), r);
  });
  const polygon = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  const summary =
    (annotated ? "Example PAVING Wheel" : `${patientName}'s PAVING Wheel`) +
    `, each area scored out of ${max}: ` +
    cats.map((c) => `${c.label} ${c.value ?? "not available"}${c.value == null ? "" : ` out of ${max}`}`).join(", ") + ".";

  const labelRadius = R + (large ? 26 : 22);
  const markerRadius = R + (large ? 12 : 10);

  // Arrow targets (annotated only): a plotted point near the outer ring, an
  // inner point near the center, and a vertex on the blue polygon.
  const higherTarget = polar(-60, R); // outer ring, upper-right
  const lowerTarget = polar(150, R * 0.3); // inner, lower-left (near center)
  const goalsIdx = cats.findIndex((c) => c.key === "goals");
  const shapeTarget = points[goalsIdx >= 0 ? goalsIdx : Math.floor(n / 2)]; // polygon vertex, lower-right

  return (
    <figure
      role="group"
      aria-labelledby={`${uid}-title`}
      style={{ margin: 0, width: "100%", aspectRatio: annotated ? undefined : "1 / 1", maxWidth: large ? 620 : annotated ? 560 : 460 }}
    >
      <span id={`${uid}-title`} style={srOnly}>
        {annotated ? "Example PAVING wellness radar chart" : `${patientName}'s PAVING wellness radar chart`}
      </span>
      <p style={srOnly}>{summary}</p>
      <svg
        viewBox={`0 0 ${VBW} ${VBH}`}
        width="100%"
        height="100%"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={summary}
        style={{ display: "block", overflow: "visible" }}
      >
        {annotated && (
          <defs>
            <marker id={`${uid}-arrow`} markerWidth="12" markerHeight="12" refX="8.5" refY="4"
              orient="auto" markerUnits="userSpaceOnUse">
              <path d="M0,0 L10,4 L0,8 Z" fill={ARROW} />
            </marker>
          </defs>
        )}

        {/* circular grid rings */}
        {Array.from({ length: RINGS }, (_, k) => (
          <circle key={k} cx={CX} cy={CY} r={((k + 1) / RINGS) * R} fill="none" stroke={GRID} strokeWidth={1} />
        ))}
        {/* axis spokes */}
        {cats.map((c, i) => {
          const [x, y] = polar(angleFor(i), R);
          return <line key={c.key} x1={CX} y1={CY} x2={x} y2={y} stroke={AXIS} strokeWidth={1} />;
        })}
        {/* scale numbers up the vertical axis */}
        {Array.from({ length: RINGS }, (_, k) => {
          const rr = ((k + 1) / RINGS) * R;
          return (
            <text key={`s${k}`} x={CX + 6} y={CY - rr + 4} fill={SCALE_TEXT} fontSize={11} textAnchor="start">
              {(k + 1) * (max / RINGS)}
            </text>
          );
        })}
        {/* data polygon */}
        <polygon points={polygon} fill={FILL} stroke={STROKE} strokeWidth={2.5} strokeLinejoin="round" />
        {/* point markers */}
        {cats.map((c, i) =>
          c.value == null ? null : (
            <circle key={`m${c.key}`} cx={points[i][0]} cy={points[i][1]} r={large ? 5 : 4}
              fill="#eaf7ff" stroke={STROKE} strokeWidth={2} />
          ),
        )}
        {/* colored category labels */}
        {cats.map((c, i) => {
          const [tx, ty] = polar(angleFor(i), markerRadius);
          const [lx, ly] = polar(angleFor(i), labelRadius);
          const cos = Math.cos((angleFor(i) * Math.PI) / 180);
          const sin = Math.sin((angleFor(i) * Math.PI) / 180);
          const anchor = cos > 0.35 ? "start" : cos < -0.35 ? "end" : "middle";
          const dy = sin > 0.5 ? 12 : sin < -0.5 ? -6 : 4;
          return (
            <g key={`l${c.key}`}>
              <circle cx={tx} cy={ty} r={3} fill={c.labelColor} />
              <text x={lx} y={ly + dy} fill={c.labelColor} fontSize={large ? 13 : 12} fontWeight={600} textAnchor={anchor}>
                {c.label}
              </text>
            </g>
          );
        })}

        {annotated && (
          <g>
            {/* Center + Higher-range labels */}
            <text x={CX} y={CY + 4} fill="rgba(255,255,255,0.85)" fontSize={12} fontWeight={600} textAnchor="middle">Center</text>
            <text x={CX} y={VBH - 44} fill={SCALE_TEXT} fontSize={12} textAnchor="middle">Higher range</text>
            <text x={CX} y={VBH - 26} fill={SCALE_TEXT} fontSize={12} textAnchor="middle">{max}</text>

            {/* target dots */}
            <circle cx={higherTarget[0]} cy={higherTarget[1]} r={5} fill="none" stroke={ARROW} strokeWidth={1.5} />
            <circle cx={lowerTarget[0]} cy={lowerTarget[1]} r={5} fill="none" stroke={ARROW} strokeWidth={1.5} />
            <circle cx={shapeTarget[0]} cy={shapeTarget[1]} r={5} fill="none" stroke={ARROW} strokeWidth={1.5} />

            {/* leader lines with solid arrowheads */}
            <line x1={476} y1={92} x2={higherTarget[0] + 6} y2={higherTarget[1] - 4} stroke={ARROW}
              strokeWidth={1.6} strokeDasharray="5 4" markerEnd={`url(#${uid}-arrow)`} />
            <line x1={190} y1={438} x2={lowerTarget[0] - 6} y2={lowerTarget[1] + 6} stroke={ARROW}
              strokeWidth={1.6} strokeDasharray="5 4" markerEnd={`url(#${uid}-arrow)`} />
            <line x1={476} y1={478} x2={shapeTarget[0] + 6} y2={shapeTarget[1] + 4} stroke={ARROW}
              strokeWidth={1.6} strokeDasharray="5 4" markerEnd={`url(#${uid}-arrow)`} />

            {/* callout boxes */}
            {calloutBox(474, 52, 176, "Higher result", "Farther from center")}
            {calloutBox(12, 430, 176, "Lower result", "Closer to center")}
            {calloutBox(474, 470, 182, "Connected shape", "Overall pattern")}
          </g>
        )}
      </svg>
    </figure>
  );
}

function calloutBox(x: number, y: number, w: number, title: string, sub: string) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={44} rx={6} fill="#131313" stroke="#3a3a3a" strokeWidth={1} />
      <text x={x + 12} y={y + 19} fill="#f5f5f5" fontSize={12.5} fontWeight={700}>{title}</text>
      <text x={x + 12} y={y + 35} fill="#9a9a9a" fontSize={11}>{sub}</text>
    </g>
  );
}

const srOnly: React.CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0,0,0,0)",
  whiteSpace: "nowrap",
  border: 0,
};
