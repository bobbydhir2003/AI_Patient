import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../state/AppContext";
import { AppImage } from "../components/common/AppImage";
import styles from "./WelcomePage.module.css";

export function WelcomePage() {
  const navigate = useNavigate();
  const { studentName, studentId, setStudentName, setStudentId } = useAppContext();
  const [nameInput, setNameInput] = useState(studentName);
  const [idInput, setIdInput] = useState(studentId);
  const [error, setError] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmedName = nameInput.trim();
    if (!trimmedName) {
      setError("Please enter your name to continue.");
      return;
    }
    setStudentName(trimmedName);
    setStudentId(idInput.trim());
    navigate("/cases");
  }

  return (
    <div className={styles.homePage}>
      <main className={styles.hero}>
        <AppImage
          src="/home/pt-interview-hero.webp"
          alt="Physical therapy interview simulation"
          className={styles.heroImage}
          loading="eager"
          fallbackSrc={null}
        />
        <div className={styles.overlay} aria-hidden="true" />

        <div className={styles.heroInner}>
          <section className={styles.content}>
            <span className={styles.eyebrow}>UNMC</span>

            <h1 className={styles.heading}>
              PT AI
              <br />
              Patient Simulator
            </h1>

            <div className={styles.accentLine} aria-hidden="true" />

            <p className={styles.subtitle}>
              Realistic patient interviews and AI-supported student assessment.
            </p>

            <p className={styles.description}>
              Practice conducting professional health-promotion interviews with
              simulated patients. Select a case, interview the patient, and
              receive structured feedback on your communication and clinical
              reasoning.
            </p>

            <form className={styles.form} onSubmit={handleSubmit} noValidate>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="student-name">
                  Student Name
                </label>
                <div className={styles.inputWrap}>
                  <svg
                    className={styles.inputIcon}
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <circle cx="12" cy="8" r="4" />
                    <path d="M4 20c0-3.3 3.6-5.5 8-5.5s8 2.2 8 5.5" />
                  </svg>
                  <input
                    id="student-name"
                    className={styles.input}
                    type="text"
                    value={nameInput}
                    onChange={(event) => setNameInput(event.target.value)}
                    placeholder="Enter your full name"
                    autoComplete="name"
                  />
                </div>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="student-id">
                  Student ID (optional)
                </label>
                <div className={styles.inputWrap}>
                  <svg
                    className={styles.inputIcon}
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <rect x="3" y="5" width="18" height="14" rx="2" />
                    <circle cx="8.5" cy="11" r="2" />
                    <path d="M5.5 16c0-1.4 1.4-2.3 3-2.3s3 .9 3 2.3" />
                    <path d="M14.5 9.5h4M14.5 13h4" />
                  </svg>
                  <input
                    id="student-id"
                    className={styles.input}
                    type="text"
                    value={idInput}
                    onChange={(event) => setIdInput(event.target.value)}
                    placeholder="Enter your student ID"
                  />
                </div>
              </div>
              {error && (
                <p className={styles.errorText} role="alert">
                  {error}
                </p>
              )}
              <div className={styles.actions}>
                <button type="submit" className={`btn btn-primary ${styles.continueButton}`}>
                  Continue
                  <span className={styles.arrow} aria-hidden="true">
                    →
                  </span>
                </button>
                <button
                  type="button"
                  className={`btn btn-secondary ${styles.demoButton}`}
                  title="Instructor Demo Mode is not yet available"
                  aria-disabled="true"
                  onClick={(event) => event.preventDefault()}
                >
                  <svg
                    className={styles.demoIcon}
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M2.5 9.5 12 5l9.5 4.5L12 14 2.5 9.5Z" />
                    <path d="M6.5 11.5v4.2c0 1.3 2.5 2.8 5.5 2.8s5.5-1.5 5.5-2.8v-4.2" />
                    <path d="M21.5 9.5v5" />
                  </svg>
                  Instructor Demo Mode
                </button>
              </div>
            </form>
          </section>
        </div>
      </main>
    </div>
  );
}
