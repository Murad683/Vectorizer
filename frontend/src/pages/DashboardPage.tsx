import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { api } from '../api/client';
import type { JobResponse } from '../api/client';
import UploadZone from '../components/UploadZone';
import Sidebar from '../components/Sidebar';
import JobDetail from '../components/JobDetail';
import HomeGreeting from '../components/HomeGreeting';
import ThemeToggle from '../components/ThemeToggle';

const POLL_INTERVAL = 4000;

export default function DashboardPage() {
  const { user, logout } = useAuth();
  const [jobs, setJobs] = useState<JobResponse[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchJobs = useCallback(async () => {
    try {
      const data = await api.listJobs();
      setJobs(data);
    } catch {
      // 401 handled by client's onUnauthorized
    }
  }, []);

  useEffect(() => {
    fetchJobs();
    pollRef.current = setInterval(fetchJobs, POLL_INTERVAL);
    return () => clearInterval(pollRef.current);
  }, [fetchJobs]);

  const hasActiveJobs = jobs.some(j => j.status === 'pending' || j.status === 'processing');

  useEffect(() => {
    if (hasActiveJobs) {
      clearInterval(pollRef.current);
      pollRef.current = setInterval(fetchJobs, POLL_INTERVAL);
    }
  }, [hasActiveJobs, fetchJobs]);

  function handleUploaded(jobId: string) {
    setSelectedJobId(jobId);
    fetchJobs();
  }

  // Sidebar entries don't call the API themselves — this does the delete
  // and the post-delete cleanup (deselect if it was open, refresh list).
  async function handleDeleteFromSidebar(jobId: string) {
    await api.deleteJob(jobId);
    if (jobId === selectedJobId) {
      setSelectedJobId(null);
    }
    fetchJobs();
  }

  // JobDetail's own delete button already calls the API before invoking
  // this — it only needs the post-delete cleanup, not another delete call.
  function handleJobDeletedInPanel() {
    setSelectedJobId(null);
    fetchJobs();
  }

  const selectedJob = jobs.find(j => j.job_id === selectedJobId) ?? null;

  return (
    <div className="h-svh flex flex-col bg-canvas">
      {/* Top bar */}
      <header className="border-b border-border shrink-0">
        <div className="px-4 sm:px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="relative w-5 h-5">
              <div className="vectorize-shape absolute inset-0 bg-accent" />
            </div>
            <span className="text-sm font-semibold tracking-tight text-ink">Vectorizer</span>
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
          jobs={jobs}
          selectedJobId={selectedJobId}
          onSelect={setSelectedJobId}
          onNewUpload={() => setSelectedJobId(null)}
          onDelete={handleDeleteFromSidebar}
        />

        <main className="flex-1 overflow-y-auto bg-canvas">
          <div className="max-w-2xl mx-auto px-6 py-12">
            {selectedJob ? (
              <JobDetail job={selectedJob} onDeleted={handleJobDeletedInPanel} />
            ) : (
              <>
                <HomeGreeting />
                <UploadZone onUploaded={handleUploaded} />
              </>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
