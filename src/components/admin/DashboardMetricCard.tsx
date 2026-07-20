import { useNavigate } from "react-router-dom";
import type { ComponentType, SVGProps } from "react";
import { IconChevronRight } from "./icons";

export type MetricColor = "red" | "blue" | "green" | "orange" | "purple" | "gray";

interface Props {
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  color: MetricColor;
  count: number;
  label: string;
  actionLabel: string;
  to: string;
}

/** Clickable summary card: icon + count + label + action link. */
export function DashboardMetricCard({ icon: Icon, color, count, label, actionLabel, to }: Props) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      className="pt-metric-card"
      onClick={() => navigate(to)}
      aria-label={`${label}: ${count}. ${actionLabel}`}
    >
      <div className="pt-metric-top">
        <span className={`pt-metric-icon pt-icon-${color}`}>
          <Icon width={20} height={20} />
        </span>
        <div>
          <div className="pt-metric-count">{count}</div>
          <div className="pt-metric-label">{label}</div>
        </div>
      </div>
      <span className="pt-metric-link">
        {actionLabel} <IconChevronRight width={13} height={13} style={{ verticalAlign: "-2px" }} />
      </span>
    </button>
  );
}
