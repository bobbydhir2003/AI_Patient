import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  apiLogin,
  apiLogout,
  apiMe,
  apiRegister,
  type AuthUser,
} from "../services/authApi";

const TOKEN_KEY = "ptai-auth-token";

interface AuthContextValue {
  token: string | null;
  user: AuthUser | null;
  loading: boolean;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isStudent: boolean;
  login: (email: string, password: string) => Promise<AuthUser>;
  register: (input: {
    fullName: string;
    email: string;
    password: string;
    studentNumber: string;
  }) => Promise<AuthUser>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(readToken);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState<boolean>(!!readToken());

  const persistToken = useCallback((next: string | null) => {
    setToken(next);
    try {
      if (next) localStorage.setItem(TOKEN_KEY, next);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable */
    }
  }, []);

  const logout = useCallback(() => {
    const current = token;
    persistToken(null);
    setUser(null);
    if (current) apiLogout(current).catch(() => undefined);
  }, [token, persistToken]);

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
        if (!cancelled) {
          persistToken(null);
          setUser(null);
        }
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
      persistToken(res.accessToken);
      setUser(res.user);
      return res.user;
    },
    [persistToken],
  );

  const register = useCallback(
    async (input: { fullName: string; email: string; password: string; studentNumber: string }) => {
      const res = await apiRegister(input);
      persistToken(res.accessToken);
      setUser(res.user);
      return res.user;
    },
    [persistToken],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      loading,
      isAuthenticated: !!user,
      isAdmin: user?.role === "admin",
      isStudent: user?.role === "student",
      login,
      register,
      logout,
    }),
    [token, user, loading, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
