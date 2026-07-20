import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

// ------------------------------------------------------------------ states
export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="pt-state">
      <div className="pt-spinner" />
      <div>{label}</div>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="pt-state">
      <div style={{ fontSize: "1.05rem", marginBottom: 6 }}>{title}</div>
      {hint && <div className="pt-muted">{hint}</div>}
    </div>
  );
}

/** Named alias for the loading state used throughout the admin area. */
export const LoadingState = Spinner;

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="pt-state">
      <div className="pt-error-text" style={{ fontSize: "1rem" }}>{message}</div>
      {onRetry && (
        <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ badges
export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    completed: "pt-badge-green",
    active: "pt-badge-amber",
    archived: "pt-badge-gray",
  };
  return <span className={`pt-badge ${map[status] ?? "pt-badge-gray"}`}>{status}</span>;
}

export function ActiveBadge({ active }: { active: boolean }) {
  return (
    <span className={`pt-badge ${active ? "pt-badge-green" : "pt-badge-gray"}`}>
      {active ? "Active" : "Inactive"}
    </span>
  );
}

const LEVEL_CLASS: Record<string, string> = {
  Advanced: "pt-badge-green",
  Proficient: "pt-badge-green",
  Strong: "pt-badge-green",
  Appropriate: "pt-badge-green",
  Competent: "pt-badge-green",
  Developing: "pt-badge-amber",
  "Needs Improvement": "pt-badge-red",
  "Needs Attention": "pt-badge-red",
  "Needs Review": "pt-badge-amber",
  "Insufficient Evidence": "pt-badge-gray",
  "Not Assessed": "pt-badge-gray",
};

export function LevelBadge({ level }: { level: string | null | undefined }) {
  if (!level) return <span className="pt-badge pt-badge-gray">—</span>;
  return <span className={`pt-badge ${LEVEL_CLASS[level] ?? "pt-badge-gray"}`}>{level}</span>;
}

/** Named alias used across the admin dashboard for qualitative assessment levels. */
export const AssessmentLevelBadge = LevelBadge;

/** Solid colour dot matching a qualitative level (for legends / donut). */
const LEVEL_DOT: Record<string, string> = {
  Advanced: "var(--color-success)",
  Proficient: "#4f8cff",
  Strong: "var(--color-success)",
  Appropriate: "#4f8cff",
  Developing: "var(--color-warning)",
  "Needs Improvement": "var(--color-danger)",
  "Needs Attention": "var(--color-danger)",
  "Needs Review": "var(--color-warning)",
  "Insufficient Evidence": "#8a63d2",
  "Not Assessed": "var(--color-text-muted)",
};
export function levelColor(level: string): string {
  return LEVEL_DOT[level] ?? "var(--color-text-muted)";
}

// ------------------------------------------------------------------ toasts
type Toast = { id: number; kind: "success" | "error"; message: string };
interface ToastCtx {
  success: (m: string) => void;
  error: (m: string) => void;
}
const ToastContext = createContext<ToastCtx | undefined>(undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: "success" | "error", message: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);
  const value: ToastCtx = {
    success: (m) => push("success", m),
    error: (m) => push("error", m),
  };
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pt-toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`pt-toast pt-toast-${t.kind}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastCtx {
  const ctx = useContext(ToastContext);
  if (!ctx) return { success: () => undefined, error: () => undefined };
  return ctx;
}

// ------------------------------------------------------------------ modals
export function ConfirmModal({
  title,
  body,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="pt-modal-backdrop" onClick={busy ? undefined : onCancel}>
      <div className={`pt-modal ${danger ? "danger" : ""}`} onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <div className="pt-sub" style={{ marginBottom: 0 }}>{body}</div>
        <div className="pt-modal-actions">
          <button className="pt-btn pt-btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className={`pt-btn ${danger ? "pt-btn-danger" : ""}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Destructive modal that requires typing DELETE before enabling the action. */
export function TypeToConfirmModal({
  title,
  body,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  body: ReactNode;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const ready = text.trim().toUpperCase() === "DELETE";
  return (
    <div className="pt-modal-backdrop" onClick={busy ? undefined : onCancel}>
      <div className="pt-modal danger" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <div className="pt-sub">{body}</div>
        <div className="pt-field">
          <label>
            Type <strong>DELETE</strong> to confirm
          </label>
          <input
            className="pt-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="DELETE"
            autoFocus
            disabled={busy}
          />
        </div>
        <div className="pt-modal-actions">
          <button className="pt-btn pt-btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className="pt-btn pt-btn-danger" onClick={onConfirm} disabled={!ready || busy}>
            {busy ? "Deleting…" : "Permanently delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Named alias: type-to-confirm destructive modal used by the dashboard. */
export const ConfirmDeleteModal = TypeToConfirmModal;
