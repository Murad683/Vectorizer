import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { api, bootstrapAuth, setToken, setOnUnauthorized } from '../api/client';

interface AuthUser {
  email: string;
  role: 'admin' | 'member';
}

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  initializing: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

function parseJwt(token: string): Record<string, unknown> {
  const base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
  return JSON.parse(atob(base64));
}

function userFromToken(token: string): AuthUser {
  const payload = parseJwt(token);
  return {
    email: payload.email as string,
    role: payload.role as 'admin' | 'member',
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(false);
  // True until the initial silent-refresh check (on hard reload) resolves —
  // gates the router so we don't flash the login screen before we know
  // whether the httpOnly refresh cookie still holds a valid session.
  const [initializing, setInitializing] = useState(true);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    api.logout().catch(() => {
      // Best-effort server-side revoke; local state is already cleared.
    });
  }, []);

  useEffect(() => {
    // 401 from any authenticated call (after a failed refresh retry) —
    // just clear local state, no need to hit /auth/logout again.
    setOnUnauthorized(() => {
      setToken(null);
      setUser(null);
    });
  }, []);

  useEffect(() => {
    (async () => {
      const token = await bootstrapAuth();
      if (token) {
        setUser(userFromToken(token));
      }
      setInitializing(false);
    })();
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    setLoading(true);
    try {
      const res = await api.login(email, password);
      setToken(res.access_token);
      setUser(userFromToken(res.access_token));
    } finally {
      setLoading(false);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, initializing, login, logout }),
    [user, loading, initializing, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be inside AuthProvider');
  return ctx;
}
