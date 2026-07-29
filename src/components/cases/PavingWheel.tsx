import { useEffect, useRef, useState, type ReactNode } from "react";
import type { PavingProfile } from "../../types/case";
import { PavingRadar } from "./PavingRadar";
import { DEFAULT_PAVING_EXAMPLE } from "./pavingExample";
import styles from "./PavingWheel.module.css";

interface PavingWheelProps {
  patientName: string;
  profile: PavingProfile;
}

const DESCRIPTION =
  "The PAVING Wheel shows different areas of the patient's lifestyle and wellness. " +
  "Use this information during the interview to explore strengths, concerns, and areas that may need support.";

const NOTE =
  "These are the patient's initial wellness results. Ask appropriate questions to better understand each area.";

/** Accessible modal: focus trap, Escape to close, restores focus on close,
 * and locks background scroll while open. */
function Modal({
  title,
  onClose,
  children,
  size = "normal",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  size?: "normal" | "wide" | "xwide";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const node = ref.current;
    node?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    // Prevent background scroll while the overlay is open.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const focusable = node.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      previouslyFocused.current?.focus?.();
    };
  }, [onClose]);

  const sizeClass = size === "xwide" ? styles.modalXWide : size === "wide" ? styles.modalWide : "";
  return (
    <div className={styles.overlay} role="presentation" onClick={onClose}>
      <div
        ref={ref}
        className={`${styles.modal} ${sizeClass}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.modalHead}>
          <h3 className={styles.modalTitle}>{title}</h3>
          <button
            type="button"
            className={styles.closeButton}
            onClick={onClose}
            aria-label="Close dialog"
            data-autofocus
          >
            &times;
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function InfoIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

function BookIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  );
}
function PersonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="4" /><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1" />
    </svg>
  );
}
function BulbIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1h6c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2z" />
    </svg>
  );
}

/** Info modal body. This is a purely instructional guide, so the right column
 * ALWAYS renders the fixed DEFAULT_PAVING_EXAMPLE - never the active patient's
 * data - and is identical for every case. */
function InfoModalBody() {
  return (
    <div className={styles.infoGrid}>
      <div className={styles.infoLeft}>
        <p className={styles.infoIntro}>
          The PAVING Wheel gives a visual overview of key wellness areas. It highlights strengths and
          areas that may need more support, and helps guide meaningful interview discussion.
        </p>

        <h4 className={styles.infoSectionHead}>
          <span className={styles.infoBadge} aria-hidden="true"><BookIcon /></span> How to Read It
        </h4>
        <ul className={styles.infoList}>
          <li>Each spoke represents one wellness area.</li>
          <li>Each plotted point shows a result for that area.</li>
          <li>Points closer to the center indicate a lower result.</li>
          <li>Points farther from the center indicate a higher result.</li>
          <li>The connected shape helps compare patterns across areas.</li>
        </ul>

        <div className={styles.infoDivider} />

        <h4 className={styles.infoSectionHead}>
          <span className={styles.infoBadge} aria-hidden="true"><PersonIcon /></span> How to Use It
        </h4>
        <ul className={styles.infoList}>
          <li>Use it to guide interview questions.</li>
          <li>Look for strengths and areas needing support.</li>
          <li>Explore lower or uneven areas before drawing conclusions.</li>
          <li>The wheel is a discussion guide, not a diagnosis or grade.</li>
          <li>Consider the patient&rsquo;s full context.</li>
        </ul>

        <div className={styles.tipBox}>
          <span className={styles.tipIcon} aria-hidden="true"><BulbIcon /></span>
          <p><span className={styles.tipLabel}>Tip:</span> Focus on patterns across wellness areas, not just one score.</p>
        </div>
      </div>

      <div className={styles.infoRight}>
        <h4 className={styles.previewHead}>PAVING Wheel Example</h4>
        <p className={styles.previewSub}>Default guide for all cases</p>

        <div className={styles.exampleChart}>
          <PavingRadar profile={DEFAULT_PAVING_EXAMPLE} patientName="Example" annotated />
        </div>

        <div className={styles.legendStrip}>
          <svg width="34" height="12" viewBox="0 0 34 12" aria-hidden="true">
            <line x1="2" y1="6" x2="32" y2="6" stroke="#5cc8ff" strokeWidth="2.5" />
            <circle cx="17" cy="6" r="4" fill="#eaf7ff" strokeWidth="2" stroke="#5cc8ff" />
          </svg>
          <span>Example only &mdash; use this wheel to guide discussion, not to make a diagnosis.</span>
        </div>
      </div>
    </div>
  );
}

export function PavingWheel({ patientName, profile }: PavingWheelProps) {
  const [enlarged, setEnlarged] = useState(false);
  const [howTo, setHowTo] = useState(false);

  return (
    <div className={`card ${styles.panel}`}>
      <button
        type="button"
        className={styles.infoButton}
        onClick={() => setHowTo(true)}
        aria-label="How to read the PAVING Wheel"
      >
        <InfoIcon />
      </button>

      <div className={styles.intro}>
        <h2 className={styles.title}>PAVING Wheel</h2>
        <p className={styles.subtitle}>{patientName}&rsquo;s Wellness Profile</p>
        <p className={styles.description}>{DESCRIPTION}</p>
        <div className={styles.note}>{NOTE}</div>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => setEnlarged(true)}
          style={{ marginTop: "var(--space-4)", alignSelf: "flex-start" }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="15 3 21 3 21 9" /><polyline points="9 21 3 21 3 15" />
            <line x1="21" y1="3" x2="14" y2="10" /><line x1="3" y1="21" x2="10" y2="14" />
          </svg>
          View Larger
        </button>
      </div>

      <div className={styles.wheelColumn}>
        <PavingRadar profile={profile} patientName={patientName} />
      </div>

      {howTo && (
        <Modal title="Understanding the PAVING Wheel" onClose={() => setHowTo(false)} size="xwide">
          <InfoModalBody />
        </Modal>
      )}

      {enlarged && (
        <Modal title={`${patientName}'s PAVING Wheel`} onClose={() => setEnlarged(false)} size="wide">
          <div className={styles.enlargedChart}>
            <PavingRadar profile={profile} patientName={patientName} large />
          </div>
        </Modal>
      )}
    </div>
  );
}
