import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { createAssessment, getAssessmentStatus } from "../services/api";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import styles from "./AssessmentLoadingPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Assessment", "Complete"];

type StatusMode = "not_started" | "pending" | "processing" | "verifying" | "completed" | "failed";
type StageMode = "saving_transcript" | "preparing" | "evaluating" | "building_report" | "completed" | "failed";

export function AssessmentLoadingPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const mountedRef = useRef(true);

  const [status, setStatus] = useState<StatusMode>("not_started");
  const [stage, setStage] = useState<StageMode>("saving_transcript");
  const [isLongRunning, setIsLongRunning] = useState(false);
  const [polling, setPolling] = useState(true);

  // Mark if we've already tried to create it to avoid double-firing in StrictMode
  const hasTriggeredCreate = useRef(false);

  // Time tracking for long-running
  useEffect(() => {
    const timer = setTimeout(() => {
      if (mountedRef.current && status !== "completed" && status !== "failed") {
        setIsLongRunning(true);
      }
    }, 45000);
    return () => clearTimeout(timer);
  }, [status]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const checkStatus = async () => {
    if (!sessionId || !mountedRef.current) return;
    try {
      const res = await getAssessmentStatus(sessionId);
      if (!mountedRef.current) return;

      setStatus(res.status as StatusMode);
      setStage(res.stage as StageMode);

      if (res.status === "completed") {
        setPolling(false);
        navigate(`/assessment/${sessionId}`, { replace: true });
      } else if (res.status === "failed") {
        setPolling(false);
      } else if (res.status === "not_started") {
        // Needs creation
        if (!hasTriggeredCreate.current) {
          hasTriggeredCreate.current = true;
          // Fire and forget
          createAssessment(sessionId).catch(console.error);
        }
      }
    } catch (err) {
      console.error("Failed to check status:", err);
    }
  };

  useEffect(() => {
    if (!polling) return;
    // Initial check immediately
    checkStatus();
    // Then poll every 2.5 seconds
    const interval = setInterval(checkStatus, 2500);
    return () => clearInterval(interval);
  }, [sessionId, polling]);

  const handleRetry = () => {
    if (!sessionId) return;
    hasTriggeredCreate.current = true;
    setStatus("pending");
    setStage("preparing");
    setPolling(true);
    createAssessment(sessionId, true).catch(console.error);
  };

  // Determine UI percentages (workflow stage markers, not time estimates)
  const getProgressInfo = () => {
    switch (stage) {
      case "saving_transcript": return { pct: 20, text: "Saving the final conversation turns..." };
      case "preparing": return { pct: 40, text: "Preparing the locked transcript and assessment context..." };
      case "evaluating": return { pct: 70, text: "Analyzing responses and applying the clinical reasoning framework..." };
      case "building_report": return { pct: 90, text: "Verifying findings and preparing your assessment report..." };
      case "completed": return { pct: 100, text: "Done!" };
      case "failed": return { pct: 0, text: "Failed." };
      default: return { pct: 0, text: "" };
    }
  };

  const { pct, text } = getProgressInfo();

  // Helper for rendering stage list
  const getStageState = (targetStageIndex: number) => {
    const stageOrder = ["saving_transcript", "preparing", "evaluating", "building_report", "completed"];
    const currentIdx = stageOrder.indexOf(stage);
    if (status === "failed") return "pending";
    if (currentIdx > targetStageIndex) return "complete";
    if (currentIdx === targetStageIndex) return "active";
    return "pending";
  };

  const renderIcon = (state: string) => {
    if (state === "complete") return <span className={styles.iconComplete}>✓</span>;
    if (state === "active") return <span className={styles.iconActive}>○</span>;
    return <span className={styles.iconPending}></span>;
  };

  return (
    <div className={styles.page}>
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={2} />

      <div className={styles.container}>
        <div className={styles.header}>
          <h1 className={styles.title}>Generating Your Assessment</h1>
          <p className={styles.subtitle}>
            Please wait while the AI reviews the interview transcript, evaluates clinical reasoning, and prepares your assessment report.
          </p>
        </div>

        {status === "failed" ? (
          <div className={styles.errorCard}>
            <h2 className={styles.errorTitle}>Assessment could not be generated</h2>
            <p className={styles.errorMessage}>
              Your interview was completed and saved, but the AI assessment could not be generated.
            </p>
            <div className={styles.actions} style={{ flexDirection: 'row', justifyContent: 'center' }}>
              <button className="btn btn-secondary" onClick={() => navigate("/cases")}>
                Back to Cases
              </button>
              <button className="btn btn-primary" onClick={handleRetry}>
                Generate Assessment Again
              </button>
            </div>
          </div>
        ) : (
          <div className={styles.content}>
            <div className={styles.leftPanel}>
              <div className={styles.graphicContainer} aria-hidden="true">
                <div className={styles.ring}></div>
                <img className={styles.logoPulse} src="/branding/logo12.png" alt="" />
              </div>

              <div className={styles.stages}>
                <div className={styles.stageRow}>
                  <div className={styles.stageIcon}>{renderIcon(getStageState(0))}</div>
                  <div className={`${styles.stageText} ${styles[`text${getStageState(0).charAt(0).toUpperCase() + getStageState(0).slice(1)}`]}`}>
                    1. Saving interview transcript
                  </div>
                </div>
                <div className={styles.stageRow}>
                  <div className={styles.stageIcon}>{renderIcon(getStageState(1))}</div>
                  <div className={`${styles.stageText} ${styles[`text${getStageState(1).charAt(0).toUpperCase() + getStageState(1).slice(1)}`]}`}>
                    2. Preparing assessment data
                  </div>
                </div>
                <div className={styles.stageRow}>
                  <div className={styles.stageIcon}>{renderIcon(getStageState(2))}</div>
                  <div className={`${styles.stageText} ${styles[`text${getStageState(2).charAt(0).toUpperCase() + getStageState(2).slice(1)}`]}`}>
                    3. Evaluating assessment rubric
                  </div>
                </div>
                <div className={styles.stageRow}>
                  <div className={styles.stageIcon}>{renderIcon(getStageState(3))}</div>
                  <div className={`${styles.stageText} ${styles[`text${getStageState(3).charAt(0).toUpperCase() + getStageState(3).slice(1)}`]}`}>
                    4. Building final report
                  </div>
                </div>
              </div>

              <div className={styles.progressContainer}>
                <div className={styles.progressTrack}>
                  <div className={styles.progressFill} style={{ width: `${pct}%` }}></div>
                </div>
                <div className={styles.progressLabel}>
                  <span>{text}</span>
                  <span>{pct}%</span>
                </div>
              </div>

              <div className={styles.actions}>
                <button className="btn btn-secondary" disabled>
                  ← Back to Cases
                </button>
              </div>
            </div>

            <div className={styles.rightPanel}>
              <div className={styles.statusCard}>
                <div className={styles.statusTitle}>
                  <span style={{ fontSize: '1.2rem' }}>🕒</span> Assessment in Progress
                </div>
                <ul className={styles.statusList}>
                  <li className={styles.statusItem}>This may take a few moments.</li>
                  <li className={styles.statusItem}>Your interview has been saved.</li>
                  <li className={styles.statusItem}>You will be taken to the assessment automatically.</li>
                  {isLongRunning && (
                    <li className={styles.statusItem} style={{ color: '#d12027', marginTop: '0.5rem' }}>
                      This assessment is taking longer than usual, but it is still processing.
                    </li>
                  )}
                </ul>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
