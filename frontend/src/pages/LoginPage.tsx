import { useState } from 'react';
import type { FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { ApiError } from '../api/client';
import ThemeToggle from '../components/ThemeToggle';

export default function LoginPage() {
  const { login, loading } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    try {
      await login(email, password);
    } catch (err) {
      if (err instanceof ApiError) {
        setError('E-poçt və ya şifrə yanlışdır.');
      } else {
        setError('Xəta baş verdi. Yenidən cəhd edin.');
      }
    }
  }

  return (
    <div className="min-h-svh flex items-center justify-center bg-canvas px-4 relative">
      <div className="absolute top-4 right-4">
        <ThemeToggle />
      </div>

      <div className="w-full max-w-sm">
        {/* Signature element: pixelated shape morphing to clean vector */}
        <div className="flex justify-center mb-8">
          <div className="relative w-16 h-16">
            <div className="vectorize-shape absolute inset-0 bg-accent" />
          </div>
        </div>

        <h1 className="font-display text-ink text-4xl text-center mb-2 tracking-tight">
          Vectorizer
        </h1>
        <p className="text-muted text-sm text-center mb-9">
          Test materiallarını vektor formatına çevirin
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="text-sm text-error bg-error/10 rounded-lg px-4 py-3">
              {error}
            </div>
          )}

          <div>
            <label htmlFor="email" className="block text-sm text-muted mb-1.5">
              E-poçt
            </label>
            <input
              id="email"
              type="email"
              required
              autoFocus
              autoComplete="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              className="w-full rounded-lg bg-surface border border-border px-4 py-2.5 text-ink text-sm placeholder:text-muted outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
              placeholder="admin@vectorizer.local"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm text-muted mb-1.5">
              Şifrə
            </label>
            <input
              id="password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full rounded-lg bg-surface border border-border px-4 py-2.5 text-ink text-sm placeholder:text-muted outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent text-accent-ink font-medium text-sm rounded-lg px-4 py-2.5 hover:brightness-110 active:brightness-95 transition-all disabled:opacity-50 cursor-pointer disabled:cursor-wait"
          >
            {loading ? 'Daxil olunur...' : 'Daxil ol'}
          </button>
        </form>
      </div>
    </div>
  );
}
