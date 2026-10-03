import { useState } from 'react';
import type { JobResponse } from '../api/client';
import { STATUS_LABELS, formatRelativeTime, groupJobsByRecency } from '../lib/jobUtils';
import { StatusDot } from './StatusBadge';

interface Props {
  jobs: JobResponse[];
  total: number;
  selectedJobId: string | null;
  onSelect: (jobId: string) => void;
  onNewUpload: () => void;
  onDelete: (jobId: string) => void;
  onViewAll: () => void;
  /** Whether the sidebar is visible — a manual toggle, not tied to
      viewport size, so desktop users can hide it too. */
  open: boolean;
  onClose: () => void;
}

/** Below the `md` breakpoint the sidebar is an overlay drawer — picking
    an item should close it so the selection is actually visible. */
const MOBILE_BREAKPOINT_PX = 768;
function isMobileViewport() {
  return window.innerWidth < MOBILE_BREAKPOINT_PX;
}

function SidebarEntry({
  job,
  isSelected,
  onSelect,
  onDelete,
}: {
  job: JobResponse;
  isSelected: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);

  return (
    <div className={`group relative transition-colors ${isSelected ? 'bg-surface' : 'hover:bg-surface-hover'}`}>
      {/* Selected-item indicator: the one recurring accent role for
          "this is active / this is mine" across the app. */}
      {isSelected && <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-full bg-accent" />}

      <button onClick={onSelect} className="w-full text-left pl-4 pr-5 py-3.5 cursor-pointer">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <p className="text-sm text-ink truncate pr-5">{job.input_filename}</p>
            <div className="flex items-center gap-1.5 mt-1.5">
              <StatusDot status={job.status} />
              <span className="text-xs text-muted">
                {STATUS_LABELS[job.status] ?? job.status} · {formatRelativeTime(job.created_at)}
              </span>
            </div>
          </div>
        </div>
      </button>

      {!confirming ? (
        <button
          onClick={e => {
            e.stopPropagation();
            setConfirming(true);
          }}
          title="Sil"
          className="absolute top-3.5 right-3 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded hover:bg-error/10 text-muted hover:text-error cursor-pointer"
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
          </svg>
        </button>
      ) : (
        <div
          onClick={e => e.stopPropagation()}
          className="absolute inset-0 bg-sidebar/95 flex items-center justify-center gap-2 px-3 text-xs"
        >
          <span className="text-ink-secondary">Silinsin?</span>
          <button
            onClick={onDelete}
            className="text-error font-medium cursor-pointer hover:underline"
          >
            Bəli
          </button>
          <button
            onClick={() => setConfirming(false)}
            className="text-muted cursor-pointer hover:underline"
          >
            Xeyr
          </button>
        </div>
      )}
    </div>
  );
}

export default function Sidebar({
  jobs,
  total,
  selectedJobId,
  onSelect,
  onNewUpload,
  onDelete,
  onViewAll,
  open,
  onClose,
}: Props) {
  // On mobile the sidebar is a full overlay drawer — once the user has
  // picked an action, close it so the result (new upload zone, selected
  // job, history page) is actually visible underneath.
  function closeIfMobile() {
    if (isMobileViewport()) onClose();
  }

  return (
    <>
      {/* Backdrop — mobile-only (md:hidden); desktop hide/show never dims
          the main panel, it just reclaims the width. */}
      {open && (
        <div
          className="fixed inset-0 bg-ink/50 z-30 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`
          fixed md:static inset-y-0 left-0 z-40 h-full w-72 max-w-[85vw] shrink-0
          bg-sidebar border-r border-border flex flex-col overflow-hidden
          transform transition-transform duration-200 ease-in-out
          md:translate-x-0 md:transition-[width,border-color]
          ${open ? 'translate-x-0' : '-translate-x-full'}
          ${!open ? 'md:w-0 md:border-r-0' : ''}
        `}
      >
        <div className="p-3 border-b border-border flex items-center gap-2 shrink-0">
          {/* Secondary action — the center drag-drop area is the intended
              focal point of the home state, so this stays quiet: muted
              text/icon at rest, no fill, only brightening on hover. */}
          <button
            onClick={() => { onNewUpload(); closeIfMobile(); }}
            className="flex-1 min-w-0 flex items-center gap-2 text-sm font-medium text-muted border border-border rounded-lg px-3 py-2 hover:text-ink-secondary hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer"
          >
            <svg className="w-4 h-4 text-accent/70 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            <span className="truncate">Yeni yükləmə</span>
          </button>

          <button
            onClick={onClose}
            title="Bağla"
            aria-label="Sidebar-ı bağla"
            className="md:hidden shrink-0 p-2 rounded-lg text-muted hover:text-ink-secondary hover:bg-surface-hover transition-colors cursor-pointer"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="no-scrollbar flex-1 overflow-y-auto">
          {jobs.length === 0 ? (
            <div className="text-center px-4 py-12">
              <div className="relative w-10 h-10 mx-auto mb-3">
                <div className="vectorize-shape absolute inset-0 bg-border-strong" />
              </div>
              <p className="text-xs text-muted leading-relaxed">
                Yükləmələriniz burada görünəcək — başlamaq üçün "Yeni yükləmə" düyməsini basın
              </p>
            </div>
          ) : (
            groupJobsByRecency(jobs).map(group => (
              <div key={group.label}>
                <p className="px-4 pt-4 pb-1.5 text-[11px] font-medium text-muted uppercase tracking-wider">
                  {group.label}
                </p>
                <div className="divide-y divide-border/60">
                  {group.jobs.map(job => (
                    <SidebarEntry
                      key={job.job_id}
                      job={job}
                      isSelected={job.job_id === selectedJobId}
                      onSelect={() => { onSelect(job.job_id); closeIfMobile(); }}
                      onDelete={() => onDelete(job.job_id)}
                    />
                  ))}
                </div>
              </div>
            ))
          )}
        </div>

        {total > jobs.length && (
          <div className="p-3 border-t border-border shrink-0">
            <button
              onClick={() => { onViewAll(); closeIfMobile(); }}
              className="w-full text-center text-xs font-medium text-muted hover:text-ink-secondary transition-colors cursor-pointer py-1.5"
            >
              Hamısına bax ({total})
            </button>
          </div>
        )}
      </aside>
    </>
  );
}
