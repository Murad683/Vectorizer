import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { api, ApiError } from '../api/client';
import type { JobResponse } from '../api/client';
import UploadZone from '../components/UploadZone';
import Sidebar from '../components/Sidebar';
import JobDetail from '../components/JobDetail';
import HomeGreeting from '../components/HomeGreeting';
import ThemeToggle from '../components/ThemeToggle';

const POLL_INTERVAL = 4000;
const SIDEBAR_LIMIT = 12;

interface Props {
  /** Set when arriving via a `#job/<id>` link (e.g. from the history page). */
  initialJobId?: string;
}

export default function DashboardPage({ initialJobId }: Props) {
  const { user, logout } = useAuth();
  const [sidebarJobs, setSidebarJobs] = useState<JobResponse[]>([]);
  const [sidebarTotal, setSidebarTotal] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(initialJobId ?? null);
  const [selectedJob, setSelectedJob] = useState<JobResponse | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  // Manual show/hide, independent of viewport — defaults open on desktop
  // (matches the pre-existing always-visible sidebar) and closed on
  // mobile (an overlay drawer shouldn't cover the screen on first load).
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 768);
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined);

  // A #job/<id> link is a one-time entry point — drop it from the URL once
  // consumed so it doesn't stay "sticky" across normal sidebar navigation.
  useEffect(() => {
    if (initialJobId) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchSidebar = useCallback(async () => {
    try {
      const page = await api.listJobs({ limit: SIDEBAR_LIMIT, offset: 0 });
      setSidebarJobs(page.items);
      setSidebarTotal(page.total);
    } catch {
      // 401 handled by client's onUnauthorized
    }
  }, []);

  useEffect(() => {
    fetchSidebar();
    pollRef.current = setInterval(fetchSidebar, POLL_INTERVAL);
    return () => clearInterval(pollRef.current);
  }, [fetchSidebar]);

  // Fetched independently of the (limited) sidebar list, so a job selected
  // from the "view all" history page — which may not be one of the sidebar's
  // most-recent entries — still opens correctly.
  useEffect(() => {
    if (!selectedJobId) {
      setSelectedJob(null);
      return;
    }
    let cancelled = false;
    async function fetchSelected() {
      try {
        const job = await api.getJob(selectedJobId!);
        if (!cancelled) setSelectedJob(job);
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 404) {
          setSelectedJobId(null);
        }
      }
    }
    fetchSelected();
    const interval = setInterval(fetchSelected, POLL_INTERVAL);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selectedJobId]);

  function handleUploaded(jobId: string) {
    setSelectedJobId(jobId);
    fetchSidebar();
  }

  // Sidebar entries don't call the API themselves — this does the delete
  // and the post-delete cleanup (deselect if it was open, refresh list).
  async function handleDeleteFromSidebar(jobId: string) {
    await api.deleteJob(jobId);
    if (jobId === selectedJobId) {
      setSelectedJobId(null);
    }
    fetchSidebar();
  }

  // JobDetail's own delete button already calls the API before invoking
  // this — it only needs the post-delete cleanup, not another delete call.
  function handleJobDeletedInPanel() {
    setSelectedJobId(null);
    fetchSidebar();
  }

  return (
    <div className="h-svh flex flex-col bg-canvas">
      {/* Top bar */}
      <header className="border-b border-border shrink-0">
        <div className="px-4 sm:px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2 sm:gap-3">
            <button
              onClick={() => setSidebarOpen(o => !o)}
              title={sidebarOpen ? 'Sidebar-ı gizlət' : 'Sidebar-ı göstər'}
              aria-label={sidebarOpen ? 'Sidebar-ı gizlət' : 'Sidebar-ı göstər'}
              className="shrink-0 p-1.5 -ml-1.5 rounded-lg text-ink-secondary hover:text-ink hover:bg-surface transition-colors cursor-pointer"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.75}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5M3.75 17.25h16.5" />
              </svg>
            </button>

            <div className="flex items-center gap-2.5">
              <div className="relative w-5 h-5 shrink-0">
                <div className="vectorize-shape absolute inset-0 bg-accent" />
              </div>
              <span className="text-sm font-semibold tracking-tight text-ink whitespace-nowrap">Vectorizer</span>
            </div>
          </div>

          <div className="flex items-center gap-1">
            <ThemeToggle />

            <div className="relative">
              <button
                onClick={() => setMenuOpen(!menuOpen)}
                className="flex items-center gap-2 text-sm text-ink-secondary hover:text-ink transition-colors cursor-pointer pl-2"
              >
                <span className="hidden sm:inline">{user?.email}</span>
                <div className="w-7 h-7 rounded-full bg-surface border border-border-strong flex items-center justify-center text-xs font-medium text-ink-secondary">
                  {user?.email?.[0]?.toUpperCase()}
                </div>
              </button>

              {menuOpen && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                  <div className="absolute right-0 mt-2 z-20 w-48 bg-panel border border-border rounded-xl shadow-md py-1">
                    <div className="px-3 py-2 border-b border-border">
                      <p className="text-xs text-muted truncate">{user?.email}</p>
                      <p className="text-xs text-muted capitalize">{user?.role === 'admin' ? 'Admin' : 'İstifadəçi'}</p>
                    </div>
                    {user?.role === 'admin' && (
                      <button
                        onClick={() => {
                          setMenuOpen(false);
                          window.location.hash = '#users';
                        }}
                        className="w-full text-left px-3 py-2 text-sm text-ink-secondary hover:bg-surface transition-colors cursor-pointer"
                      >
                        İstifadəçiləri idarə et
                      </button>
                    )}
                    <button
                      onClick={() => { setMenuOpen(false); logout(); }}
                      className="w-full text-left px-3 py-2 text-sm text-ink-secondary hover:bg-surface transition-colors cursor-pointer"
                    >
                      Çıxış
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </header>

      {/* Sidebar + main panel */}
      <div className="flex-1 flex overflow-hidden">
        <Sidebar
          jobs={sidebarJobs}
          total={sidebarTotal}
          selectedJobId={selectedJobId}
          onSelect={setSelectedJobId}
          onNewUpload={() => setSelectedJobId(null)}
          onDelete={handleDeleteFromSidebar}
          onViewAll={() => { window.location.hash = '#history'; }}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />

        <main className="flex-1 overflow-y-auto bg-canvas">
          {selectedJob ? (
            <div className="max-w-2xl mx-auto px-4 sm:px-6 py-8 sm:py-12">
              <JobDetail job={selectedJob} onDeleted={handleJobDeletedInPanel} />
            </div>
          ) : (
            // Home/empty state only — vertically centered so the greeting +
            // drop zone read as a balanced composition rather than stacked
            // at the top. min-h-full (not h-full) lets it grow past the
            // viewport and scroll on short screens instead of clipping.
            <div className="min-h-full flex items-center justify-center px-4 sm:px-6 py-8 sm:py-12">
              <div className="max-w-2xl w-full">
                <HomeGreeting />
                <UploadZone onUploaded={handleUploaded} />
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
