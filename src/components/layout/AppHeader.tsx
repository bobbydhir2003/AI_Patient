import { Link } from "react-router-dom";
import { AppImage } from "../common/AppImage";
import styles from "./AppHeader.module.css";

export function AppHeader() {
  return (
    <header className={styles.header}>
      <div className={styles.inner}>
        <Link to="/" className={styles.brand} aria-label="UNMC PT AI Patient Simulator home">
          <AppImage
            src="/branding/unmc-logo.png"
            alt="UNMC logo"
            className={styles.logo}
            loading="eager"
            fallbackSrc={null}
          />
          <span className={styles.brandText}>
            <span className={styles.unmc}>UNMC</span>
            <span className={styles.divider} aria-hidden="true" />
            <span className={styles.title}>PT AI Patient Simulator</span>
          </span>
        </Link>
      </div>
    </header>
  );
}
