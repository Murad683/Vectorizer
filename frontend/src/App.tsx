import { useEffect, useState } from 'react';
import { AuthProvider, useAuth } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import LoginPage from './pages/LoginPage';
import DashboardPage from './pages/DashboardPage';
import UsersPage from './pages/UsersPage';
import JobHistoryPage from './pages/JobHistoryPage';

function LoadingScreen() {
  return (
    <div className="h-svh flex items-center justify-center bg-canvas">
      <div className="relative w-8 h-8">
        <div className="vectorize-shape absolute inset-0 bg-accent animate-pulse" />
      </div>
    </div>
  );
}

function Router() {
  const { user, initializing } = useAuth();
  const [hash, setHash] = useState(window.location.hash);

  useEffect(() => {
    function onHashChange() { setHash(window.location.hash); }
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // Gate on the silent-refresh check so a hard reload doesn't flash the
  // login screen before we know the httpOnly refresh cookie is still good.
  if (initializing) return <LoadingScreen />;

  if (!user) return <LoginPage />;

  if (hash === '#users' && user.role === 'admin') {
    return <UsersPage />;
  }

  if (hash === '#history') {
    return <JobHistoryPage />;
  }

  if (hash.startsWith('#job/')) {
    return <DashboardPage initialJobId={hash.slice('#job/'.length)} />;
  }

  return <DashboardPage />;
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <Router />
      </AuthProvider>
    </ThemeProvider>
  );
}
