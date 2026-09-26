import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Center, Loader } from '@mantine/core';

import { getCurrentUser, onAuthChange, refreshSession, type User } from './login';

interface AuthState {
  user: User | null;
  /** False until the initial session restore (refresh cookie) has finished. */
  ready: boolean;
}

const AuthContext = createContext<AuthState>({ user: null, ready: false });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ user: getCurrentUser(), ready: false });

  useEffect(() => {
    const unsubscribe = onAuthChange((user) => setState({ user, ready: true }));
    // Restore the session after a reload from the httpOnly refresh cookie.
    refreshSession().finally(() => setState({ user: getCurrentUser(), ready: true }));
    return () => {
      unsubscribe();
    };
  }, []);

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);

export function RequireAuth({ children, roles }: { children: ReactNode; roles?: string[] }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (roles?.length && !user.roles.includes('admin') && !roles.some((r) => user.roles.includes(r))) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
