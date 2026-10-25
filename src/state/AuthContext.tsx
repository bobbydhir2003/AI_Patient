import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { LOGIN_ROUTE } from "../services/authRouting";
import {
  apiLogin,
  apiLogout,
  apiMe,
  apiRegister,
  type AuthUser,
  type RegisterResult,
} from "../services/authApi";
import {
  clearLtiSession,
  readLtiSession,
  saveLtiSession,
  type LtiSession,
} from "../services/ltiApi";

const TOKEN_KEY = "ptai-auth-token";

interface AuthContextValue {
  token: string | null;
  user: AuthUser | null;
  loading: boolean;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isStudent: boolean;
  /** Set while this tab runs an AI iEXCEL course launch (course-scoped). */
  lti: LtiSession | null;
  beginLtiSession: (session: LtiSession) => void;
  login: (email: string, password: string) => Promise<AuthUser>;
  register: (input: {
    fullName: string;
    email: string;
    password: string;
    studentNumber: string;
  }) => Promise<RegisterResult>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function readToken(): string | null {
  const lti = readLtiSession();
  if (lti) return lti.token;
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** Where a course session ends up when it expires or the user leaves it. */
const LTI_ENDED_ROUTE = "/lti/ended";

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [token, setToken] = useState<string | null>(readToken);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState<boolean>(!!readToken());
  const [lti, setLti] = useState<LtiSession | null>(readLtiSession);

  const persistToken = useCallback((next: string | null) => {
    setToken(next);
    try {
      if (next) localStorage.setItem(TOKEN_KEY, next);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable */
    }
  }, []);

  /** End a course session only: the direct-login token (if any) is untouched.
   * A full page load (not a client-side navigation) drops every bit of
   * in-memory course state and cannot race the route guards. */
  const endLtiSession = useCallback((reason: "ended" | "expired") => {
    clearLtiSession();
    window.location.replace(`${LTI_ENDED_ROUTE}?reason=${reason}`);
  }, []);

  const beginLtiSession = useCallback((session: LtiSession) => {
    saveLtiSession(session);
    setLti(session);
    setUser(session.user);
    setToken(session.token);
  }, []);

  // A course session ends on its own when its short-lived token expires.
  useEffect(() => {
    if (!lti) return;
    const timer = window.setTimeout(
      () => endLtiSession("expired"),
      Math.max(lti.expiresAt - Date.now(), 0),
    );
    return () => window.clearTimeout(timer);
  }, [lti, endLtiSession]);

  const logout = useCallback(() => {
    const current = token;
    if (lti) {
      if (current) apiLogout(current).catch(() => undefined);
      endLtiSession("ended");
      return;
    }
    persistToken(null);
    setUser(null);
    if (current) apiLogout(current).catch(() => undefined);
    // Single, consistent logout destination for EVERY user type (student, admin,
    // promoted professor, system admin): the main PT AI Patient Simulator
    // home/login page (LOGIN_ROUTE = "/"). There is no separate admin login page.
    navigate(LOGIN_ROUTE, { replace: true });
  }, [token, lti, endLtiSession, persistToken, navigate]);

  // Validate an existing token on first load; drop it if invalid/expired.
  useEffect(() => {
    let cancelled = false;
    if (!token) {
      setLoading(false);
      return;
    }
    setLoading(true);
    apiMe(token)
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch(() => {
        if (cancelled) return;
        if (readLtiSession()) {
          endLtiSession("expired");
          return;
        }
        persistToken(null);
        setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // Only re-run when the token identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await apiLogin(email, password);
      // A direct sign-in in this tab replaces any course session.
      clearLtiSession();
      setLti(null);
      persistToken(res.accessToken);
      setUser(res.user);
      return res.user;
    },
    [persistToken],
  );

  const register = useCallback(
    // D2: registration no longer auto-logs-in; it returns a pending status
    // message. The caller shows it and directs the user to sign in after approval.
    async (input: { fullName: string; email: string; password: string; studentNumber: string }) => {
      return apiRegister(input);
    },
    [],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      loading,
      isAuthenticated: !!user,
      isAdmin: user?.role === "admin",
      isStudent: user?.role === "student",
      lti,
      beginLtiSession,
      login,
      register,
      logout,
    }),
    [token, user, loading, lti, beginLtiSession, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
