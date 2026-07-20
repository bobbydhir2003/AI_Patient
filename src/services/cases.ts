import { useCallback, useEffect, useState } from "react";
import type { CaseCatalog, PatientCase } from "../types/case";
import { fetchCaseCatalog } from "./api";

/**
 * Loads the grouped case catalog from the backend. There is NO offline/mock
 * fallback: if the backend is unreachable, callers must show an error + Retry.
 * The frontend renders sections generically and knows no case ids in advance.
 */
let cachedCatalog: CaseCatalog | null = null;

export async function loadCatalog(force = false): Promise<CaseCatalog> {
  if (cachedCatalog && !force) return cachedCatalog;
  cachedCatalog = await fetchCaseCatalog();
  return cachedCatalog;
}

function flatten(catalog: CaseCatalog | null): PatientCase[] {
  return catalog ? catalog.sections.flatMap((section) => section.cases) : [];
}

interface CatalogState {
  catalog: CaseCatalog | null;
  cases: PatientCase[];
  loading: boolean;
  error: string | null;
  retry: () => void;
}

export function useCaseCatalog(): CatalogState {
  const [catalog, setCatalog] = useState<CaseCatalog | null>(cachedCatalog);
  const [loading, setLoading] = useState(cachedCatalog === null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    loadCatalog(attempt > 0)
      .then((loaded) => {
        if (!cancelled) {
          setCatalog(loaded);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          console.error("Failed to load the case catalog from the backend:", err);
          setError("Could not load patient cases. Check that the backend is running.");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { catalog, cases: flatten(catalog), loading, error, retry };
}

/** Back-compat alias used across pages. */
export function useCases(): CatalogState {
  return useCaseCatalog();
}

interface PatientCaseState {
  patientCase: PatientCase | undefined;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

export function usePatientCase(caseId: string | undefined): PatientCaseState {
  const { cases, loading, error, retry } = useCaseCatalog();
  const patientCase = caseId ? cases.find((c) => c.id === caseId) : undefined;
  return { patientCase, loading, error, retry };
}
