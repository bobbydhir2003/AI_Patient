import {
  createContext,
  useContext,
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import type { ConversationMessage } from "../types/interview";

const STORAGE_KEY = "ptai-app-state";

/** The backend session the UI is currently bound to. caseId and sessionId are
 * stored together so a session can never be reused for another patient. */
export interface ActiveInterview {
  caseId: string;
  sessionId: string;
  startedAt: number;
}

/** Only identity + the active session pointer are persisted. The transcript
 * itself is NEVER persisted locally - the backend is the source of truth and
 * messages are restored from GET /api/sessions/{id}. */
interface StoredState {
  studentName: string;
  studentId: string;
  activeInterview: ActiveInterview | null;
}

interface AppContextValue extends StoredState {
  messages: ConversationMessage[];
  setStudentName: (name: string) => void;
  setStudentId: (id: string) => void;
  setActiveInterview: (interview: ActiveInterview | null) => void;
  /** Accepts either a full array or a functional updater (the streaming
   * transcript path updates a growing patient message in place). */
  setMessages: Dispatch<SetStateAction<ConversationMessage[]>>;
  addMessage: (message: ConversationMessage) => void;
  clearInterview: () => void;
}

const defaultStored: StoredState = {
  studentName: "",
  studentId: "",
  activeInterview: null,
};

function loadStoredState(): StoredState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultStored;
    const parsed = JSON.parse(raw) as Partial<StoredState>;
    return {
      studentName: typeof parsed.studentName === "string" ? parsed.studentName : "",
      studentId: typeof parsed.studentId === "string" ? parsed.studentId : "",
      activeInterview:
        parsed.activeInterview &&
        typeof parsed.activeInterview.caseId === "string" &&
        typeof parsed.activeInterview.sessionId === "string" &&
        typeof parsed.activeInterview.startedAt === "number"
          ? parsed.activeInterview
          : null,
    };
  } catch {
    return defaultStored;
  }
}

const AppContext = createContext<AppContextValue | undefined>(undefined);

export function AppProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState<StoredState>(loadStoredState);
  // Transcript lives in memory only; per-interview, reset on case change.
  const [messages, setMessages] = useState<ConversationMessage[]>([]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    } catch {
      // localStorage unavailable; ignore
    }
  }, [stored]);

  const value: AppContextValue = {
    ...stored,
    messages,
    setStudentName: (name) => setStored((s) => ({ ...s, studentName: name })),
    setStudentId: (id) => setStored((s) => ({ ...s, studentId: id })),
    setActiveInterview: (interview) =>
      setStored((s) => ({ ...s, activeInterview: interview })),
    setMessages,
    addMessage: (message) => setMessages((m) => [...m, message]),
    clearInterview: () => {
      setStored((s) => ({ ...s, activeInterview: null }));
      setMessages([]);
    },
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useAppContext(): AppContextValue {
  const context = useContext(AppContext);
  if (!context) {
    throw new Error("useAppContext must be used within an AppProvider");
  }
  return context;
}
