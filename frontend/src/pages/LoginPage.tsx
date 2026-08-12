import { useState } from 'react';
import type { FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { ApiError } from '../api/client';

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
    <div className="min-h-svh flex items-center justify-center bg-dark px-4">
      <div className="w-full max-w-sm">
        {/* Signature element: pixelated shape morphing to clean vector */}
        <div className="flex justify-center mb-10">
          <div className="relative w-16 h-16">
            <div className="vectorize-shape absolute inset-0 bg-accent" />
          </div>
        </div>

        <h1 className="text-white text-2xl font-semibold tracking-tight text-center mb-1">
          Vectorizer
        </h1>
        <p className="text-muted text-sm text-center mb-8">
          Test materiallarını vektor formatına çevirin
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="text-sm text-error bg-error/10 rounded-lg px-4 py-3">
              {error}
            </div>
          )}

          <div>
            <label htmlFor="email" className="block text-sm text-gray-400 mb-1.5">
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
              className="w-full rounded-lg bg-white/5 border border-white/10 px-4 py-2.5 text-white text-sm placeholder:text-gray-600 outline-none focus:border-accent/50 focus:ring-1 focus:ring-accent/30 transition-colors"
              placeholder="admin@vectorizer.local"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm text-gray-400 mb-1.5">
              Şifrə
            </label>
            <input
              id="password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full rounded-lg bg-white/5 border border-white/10 px-4 py-2.5 text-white text-sm placeholder:text-gray-600 outline-none focus:border-accent/50 focus:ring-1 focus:ring-accent/30 transition-colors"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent text-dark font-medium text-sm rounded-lg px-4 py-2.5 hover:brightness-110 active:brightness-95 transition-all disabled:opacity-50 cursor-pointer disabled:cursor-wait"
          >
            {loading ? 'Daxil olunur...' : 'Daxil ol'}
          </button>
        </form>
      </div>
    </div>
  );
}
