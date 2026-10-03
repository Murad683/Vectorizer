import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { api, ApiError } from '../api/client';
import type { UserResponse } from '../api/client';
import ThemeToggle from '../components/ThemeToggle';

function UserRow({
  u,
  isSelf,
  onToggleActive,
  onDelete,
}: {
  u: UserResponse;
  isSelf: boolean;
  onToggleActive: (u: UserResponse) => void;
  onDelete: (u: UserResponse) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const deleteDisabled = isSelf || u.job_count > 0;
  const deleteTitle = isSelf
    ? 'Öz hesabınızı silə bilməzsiniz'
    : u.job_count > 0
      ? `${u.job_count} yükləməyə sahibdir — əvvəlcə deaktiv edin`
      : undefined;

  return (
    <div className="py-3.5 flex flex-wrap items-center justify-between first:pt-0 last:pb-0 gap-2 sm:gap-3">
      <div className="min-w-0">
        <p className="text-sm text-ink truncate">{u.email}</p>
        <p className="text-xs text-muted mt-0.5">
          {new Date(u.created_at).toLocaleDateString('az-AZ')}
          {!u.is_active && <span className="text-error ml-2">Deaktiv</span>}
        </p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span
          className={`text-xs font-medium px-2.5 py-1 rounded-full ${
            u.role === 'admin' ? 'bg-accent/20 text-ink-secondary' : 'bg-surface text-muted'
          }`}
        >
          {u.role === 'admin' ? 'Admin' : 'İstifadəçi'}
        </span>

        <button
          onClick={() => onToggleActive(u)}
          disabled={isSelf}
          title={isSelf ? 'Öz hesabınızı deaktiv edə bilməzsiniz' : undefined}
          className="text-xs font-medium border border-border rounded-lg px-2.5 py-1.5 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:border-border"
        >
          {u.is_active ? 'Deaktiv et' : 'Aktivləşdir'}
        </button>

        {!confirmingDelete ? (
          <button
            onClick={() => setConfirmingDelete(true)}
            disabled={deleteDisabled}
            title={deleteTitle}
            className="text-xs font-medium text-muted hover:text-error transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-muted px-2 py-1.5"
          >
            Sil
          </button>
        ) : (
          <div className="flex items-center gap-1.5 text-xs">
            <button
              onClick={() => onDelete(u)}
              className="text-error font-medium cursor-pointer hover:underline"
            >
              Bəli
            </button>
            <button
              onClick={() => setConfirmingDelete(false)}
              className="text-muted cursor-pointer hover:underline"
            >
              Xeyr
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function UsersPage() {
  const { user, logout } = useAuth();
  const [users, setUsers] = useState<UserResponse[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<'admin' | 'member'>('member');
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const [saving, setSaving] = useState(false);

  const fetchUsers = useCallback(async () => {
    try {
      const data = await api.listUsers();
      setUsers(data);
    } catch {
      // handled by 401 redirect
    }
  }, []);

  useEffect(() => { fetchUsers(); }, [fetchUsers]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setError('');
    setSaving(true);
    try {
      await api.createUser(email, password, role);
      setEmail('');
      setPassword('');
      setRole('member');
      setShowForm(false);
      fetchUsers();
    } catch (err) {
      if (err instanceof ApiError && err.code === 'email_already_exists') {
        setError('Bu e-poçt ünvanı artıq istifadə olunur.');
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError('Xəta baş verdi.');
      }
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleActive(u: UserResponse) {
    setListError('');
    try {
      await api.setUserActive(u.id, !u.is_active);
      fetchUsers();
    } catch (err) {
      setListError(err instanceof ApiError ? err.message : 'Xəta baş verdi.');
    }
  }

  async function handleDelete(u: UserResponse) {
    setListError('');
    try {
      await api.deleteUser(u.id);
      fetchUsers();
    } catch (err) {
      if (err instanceof ApiError && err.code === 'user_owns_jobs') {
        setListError(`${u.email} istifadəçisi hələ də yükləmələrə sahibdir. Silmək əvəzinə deaktiv edin.`);
      } else {
        setListError(err instanceof ApiError ? err.message : 'Xəta baş verdi.');
      }
      fetchUsers();
    }
  }

  return (
    <div className="min-h-svh bg-canvas">
      <header className="border-b border-border">
        <div className="max-w-2xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <button
              onClick={() => { window.location.hash = ''; }}
              className="flex items-center gap-2.5 cursor-pointer"
            >
              <div className="relative w-5 h-5">
                <div className="vectorize-shape absolute inset-0 bg-accent" />
              </div>
              <span className="text-sm font-semibold tracking-tight text-ink">Vectorizer</span>
            </button>
            <span className="text-border-strong">/</span>
            <span className="text-sm text-muted">İstifadəçilər</span>
          </div>

          <div className="flex items-center gap-3">
            <ThemeToggle />
            <span className="text-xs text-muted hidden sm:inline">{user?.email}</span>
            <button
              onClick={logout}
              className="text-xs text-ink-secondary hover:text-ink transition-colors cursor-pointer"
            >
              Çıxış
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-2xl mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-xs font-medium text-muted uppercase tracking-wider">
            İstifadəçilər
          </h2>
          {!showForm && (
            <button
              onClick={() => setShowForm(true)}
              className="text-xs font-medium text-ink-secondary border border-border rounded-lg px-3 py-1.5 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer"
            >
              + Yeni istifadəçi
            </button>
          )}
        </div>

        {showForm && (
          <form onSubmit={handleCreate} className="mb-8 bg-panel border border-border rounded-xl p-5 space-y-4 shadow-sm">
            {error && (
              <div className="text-sm text-error bg-error/10 rounded-lg px-4 py-3">
                {error}
              </div>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="new-email" className="block text-xs text-muted mb-1.5">E-poçt</label>
                <input
                  id="new-email"
                  type="email"
                  required
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  className="w-full rounded-lg bg-surface border border-border px-3 py-2 text-sm text-ink outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
                />
              </div>
              <div>
                <label htmlFor="new-password" className="block text-xs text-muted mb-1.5">Şifrə</label>
                <input
                  id="new-password"
                  type="password"
                  required
                  minLength={4}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  className="w-full rounded-lg bg-surface border border-border px-3 py-2 text-sm text-ink outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
                />
              </div>
            </div>
            <div>
              <label htmlFor="new-role" className="block text-xs text-muted mb-1.5">Rol</label>
              <select
                id="new-role"
                value={role}
                onChange={e => setRole(e.target.value as 'admin' | 'member')}
                className="w-full rounded-lg bg-surface border border-border px-3 py-2 text-sm text-ink outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors cursor-pointer"
              >
                <option value="member">İstifadəçi</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <div className="flex gap-2 justify-end">
              <button
                type="button"
                onClick={() => { setShowForm(false); setError(''); }}
                className="text-sm text-ink-secondary px-4 py-2 rounded-lg hover:bg-surface-hover transition-colors cursor-pointer"
              >
                Ləğv et
              </button>
              <button
                type="submit"
                disabled={saving}
                className="text-sm font-medium bg-accent text-accent-ink px-4 py-2 rounded-lg hover:brightness-110 active:brightness-95 transition-all disabled:opacity-50 cursor-pointer disabled:cursor-wait"
              >
                {saving ? 'Yaradılır...' : 'Yarat'}
              </button>
            </div>
          </form>
        )}

        {listError && (
          <div className="mb-4 text-sm text-error bg-error/10 rounded-lg px-4 py-3">
            {listError}
          </div>
        )}

        <div className="divide-y divide-border">
          {users.map(u => (
            <UserRow
              key={u.id}
              u={u}
              isSelf={u.email === user?.email}
              onToggleActive={handleToggleActive}
              onDelete={handleDelete}
            />
          ))}
        </div>
      </main>
    </div>
  );
}
