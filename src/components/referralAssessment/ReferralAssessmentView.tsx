import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Assessment, AssessmentTurn, ReferralEvidence } from "../../types/assessment";
import type { PatientCase } from "../../types/case";
import { useAuth } from "../../state/AuthContext";
import { caseHubPath } from "../../services/authRouting";
import styles from "./ReferralAssessmentView.module.css";

interface Props {
  assessment: Assessment;
  patientCase?: PatientCase;
  turns: AssessmentTurn[];
}

const levelClass = (l: string) => {
  if (l === "Strong" || l === "Appropriate") return styles.strong;
  if (l === "Developing") return styles.developing;
  if (l === "Needs Attention") return styles.attention;
  if (l === "Insufficient Evidence") return styles.insufficient;
  if (l === "Not Assessed") return styles.not_assessed;
  if (l === "Needs Review") return styles.needsReview;
  return styles.muted;
};

export function ReferralAssessmentView({ assessment, patientCase, turns }: Props) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const referral = assessment.referral;
  const [selected, setSelected] = useState<ReferralEvidence | null>(null);
  const [showTranscript, setShowTranscript] = useState(false);

  if (!referral) return null;

  const openMoment = (moment: ReferralEvidence) => {
    setSelected(moment);
    setShowTranscript(true);
    requestAnimationFrame(() => {
      document.getElementById(`ref-turn-${moment.turnId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  };

  return (
    <div className={styles.page}>
      <header className={styles.progress}>
        <span>✓ Case Introduction</span><i /><span>✓ Interview</span><i />
        <span className={styles.activeStep}>3 Assessment</span><i /><span>Complete</span>
        <button onClick={() => navigate(caseHubPath(user?.role))}>← Back to Cases</button>
      </header>

      <section className={styles.hero}>
        <div>
          <h1>Referral Assessment <b>★ Advanced</b></h1>
          <p>{patientCase ? `${patientCase.name} · ${patientCase.age} years · ${patientCase.setting || "PT setting"}` : "Referral case"}</p>
          <p>This interview included a concern that may require consultation, referral, coordination, or escalation.</p>
        </div>
        <button className={styles.reportButton}>⇩ Download Report</button>
      </section>

      <div className={styles.notice}>ⓘ This assessment evaluates how you recognized the concern, explored it, respected scope of practice, considered urgency and safety, and communicated next steps.</div>

      <section className={styles.summary}>
        <div className={styles.summaryLead}>
          <h2>Referral Judgment Summary</h2>
          <div className={styles.judgmentRow}>
            <div className={styles.statusRing}>▣</div>
            <div><small>Overall Referral Judgment</small><h3 className={levelClass(referral.overallLevel ?? "")}>{referral.overallLevel ?? "Insufficient Evidence"}</h3><p>{referral.overallSummary}</p></div>
          </div>
        </div>
        <div><h3 className={styles.greenHeading}>Key Strengths</h3>{referral.keyStrengths.map((x) => <p key={x}>✓ {x}</p>)}<h3 className={styles.amberHeading}>Growth Opportunities</h3>{referral.growthOpportunities.map((x) => <p key={x}>△ {x}</p>)}</div>
        <div className={styles.evidenceBox}><h3>Evidence Overview</h3><strong>{referral.keyMoments.length}</strong><span>Key Moments</span><strong>{turns.length}</strong><span>Total Turns Used</span></div>
      </section>

      <section className={styles.domainSection}>
        <h2>Domain Results</h2>
        <div className={styles.domainGrid}>
          {referral.domains.map((domain, index) => (
            <article key={domain.domainId} className={`${styles.domainCard} ${levelClass(domain.level)}`}>
              <div className={styles.domainIcon}>◉</div>
              <h3>{index + 1}. {domain.title}</h3>
              <strong>{domain.level}</strong>
              <p>{domain.summary}</p>
              <button onClick={() => domain.evidence[0] && openMoment(domain.evidence[0])}>View Moments ({domain.evidence.length})</button>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.lowerGrid}>
        <div><h2>Clinical Judgment Timeline</h2>{referral.timeline.map((entry) => <div className={styles.timelineItem} key={`${entry.turnId}-${entry.label}`}><span>{entry.turnLabel}</span><i /><div><strong>{entry.label}</strong><p>{entry.description}</p><small>{entry.excerpt}</small></div></div>)}</div>
        <div><div className={styles.sectionHeader}><h2>Key Transcript Moments</h2><button onClick={() => setShowTranscript(true)}>View Full Transcript</button></div>{referral.keyMoments.slice(0, 6).map((moment) => <button className={styles.moment} key={moment.evidenceId} onClick={() => openMoment(moment)}><b>{moment.turnLabel}</b><span>{moment.speaker === "student" ? "You" : "Patient"}: {moment.studentExcerpt || moment.patientContextExcerpt}</span><em>{moment.domainTitle}</em></button>)}</div>
      </section>

      <details className={styles.hiddenPanel}><summary>AI Clinical Judgment Analysis</summary><p>{referral.activationReason}</p></details>
      <details className={styles.hiddenPanel}><summary>Referral Decision Pathway</summary><p>{referral.timeline.map((x) => x.label).join(" → ") || "Insufficient transcript evidence to build a pathway."}</p></details>
      <details className={styles.hiddenPanel}><summary>How This Assessment Was Built</summary><p>Locked transcript → protected selected-case context → universal referral rubric → AI evidence extraction → AI domain evaluation → independent AI verification.</p></details>

      {showTranscript && <div className={styles.drawer}><div className={styles.drawerHeader}><h2>Transcript Evidence</h2><button onClick={() => setShowTranscript(false)}>Close</button></div>{turns.map((turn) => <article id={`ref-turn-${turn.turnId}`} key={turn.turnId} className={`${styles.turn} ${selected?.turnId === turn.turnId ? styles.highlight : ""}`}><b>{turn.sender === "student" ? "You" : "Patient"}</b><p>{turn.text}</p>{selected?.turnId === turn.turnId && <aside><strong>{selected.domainTitle}</strong><p>{selected.whyItMatters}</p></aside>}</article>)}</div>}
      <footer>This assessment is a learning tool and does not replace instructor evaluation or clinical judgment.</footer>
    </div>
  );
}
