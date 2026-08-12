import { useEffect, useState } from 'react';
import { AuthProvider, useAuth } from './context/AuthContext';
import LoginPage from './pages/LoginPage';
import DashboardPage from './pages/DashboardPage';
import UsersPage from './pages/UsersPage';

function Router() {
  const { user } = useAuth();
  const [hash, setHash] = useState(window.location.hash);

  useEffect(() => {
    function onHashChange() { setHash(window.location.hash); }
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  if (!user) return <LoginPage />;

  if (hash === '#users' && user.role === 'admin') {
    return <UsersPage />;
  }

  return <DashboardPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <Router />
    </AuthProvider>
  );
}
