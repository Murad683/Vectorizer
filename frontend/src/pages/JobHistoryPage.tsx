import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { api } from '../api/client';
import type { JobResponse } from '../api/client';
import { STATUS_LABELS, formatRelativeTime } from '../lib/jobUtils';
import { StatusDot } from '../components/StatusBadge';
import ThemeToggle from '../components/ThemeToggle';

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 400;

function HistoryRow({ job, onOpen }: { job: JobResponse; onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className="w-full text-left flex items-center justify-between gap-3 py-3.5 first:pt-0 last:pb-0 hover:bg-surface-hover -mx-3 px-3 rounded-lg transition-colors cursor-pointer"
    >
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink truncate">{job.input_filename}</p>
        <div className="flex items-center gap-1.5 mt-1">
          <StatusDot status={job.status} />
          <span className="text-xs text-muted">
            {STATUS_LABELS[job.status] ?? job.status} · {formatRelativeTime(job.created_at)}
          </span>
        </div>
      </div>
      <svg className="w-4 h-4 text-muted shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
      </svg>
    </button>
  );
}

export default function JobHistoryPage() {
  const { user, logout } = useAuth();
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<JobResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  // Debounce the search box — don't fire a backend request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchInput.trim());
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchInput]);

  const fetchPage = useCallback(async () => {
    setLoading(true);
    try {
      const page = await api.listJobs({ limit: PAGE_SIZE, offset, search: search || undefined });
      setItems(page.items);
      setTotal(page.total);
    } catch {
      // 401 handled by client's onUnauthorized
    } finally {
      setLoading(false);
    }
  }, [offset, search]);

  useEffect(() => { fetchPage(); }, [fetchPage]);

  function openJob(jobId: string) {
    window.location.hash = `#job/${jobId}`;
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

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
            <span className="text-sm text-muted">Tarixçə</span>
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
            Bütün yükləmələr {total > 0 && `(${total})`}
          </h2>
        </div>

        <div className="relative mb-6">
          <svg className="w-4 h-4 text-muted absolute left-3 top-1/2 -translate-y-1/2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
          </svg>
          <input
            type="text"
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
            placeholder="Fayl adına görə axtar..."
            className="w-full rounded-lg bg-surface border border-border pl-9 pr-3 py-2.5 text-sm text-ink outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
          />
        </div>

        {!loading && items.length === 0 ? (
          <div className="text-center px-4 py-16">
            <div className="relative w-10 h-10 mx-auto mb-3">
              <div className="vectorize-shape absolute inset-0 bg-border-strong" />
            </div>
            <p className="text-sm text-muted">
              {search ? 'Heç bir nəticə tapılmadı.' : 'Hələ heç bir yükləmə yoxdur.'}
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {items.map(job => (
              <HistoryRow key={job.job_id} job={job} onOpen={() => openJob(job.job_id)} />
            ))}
          </div>
        )}

        {pageCount > 1 && (
          <div className="flex items-center justify-between mt-8 pt-4 border-t border-border">
            <button
              onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
              disabled={offset === 0}
              className="text-xs font-medium text-ink-secondary border border-border rounded-lg px-3 py-1.5 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:border-border"
            >
              Əvvəlki
            </button>
            <span className="text-xs text-muted">{page} / {pageCount}</span>
            <button
              onClick={() => setOffset(o => (page < pageCount ? o + PAGE_SIZE : o))}
              disabled={page >= pageCount}
              className="text-xs font-medium text-ink-secondary border border-border rounded-lg px-3 py-1.5 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:border-border"
            >
              Növbəti
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
