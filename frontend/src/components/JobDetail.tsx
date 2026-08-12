import { useState } from 'react';
import type { JobResponse } from '../api/client';
import { api } from '../api/client';
import { formatDate, downloadJobFile } from '../lib/jobUtils';
import StatusBadge from './StatusBadge';

interface Props {
  job: JobResponse;
  onDeleted: () => void;
}

export default function JobDetail({ job, onDeleted }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    setDeleting(true);
    try {
      await api.deleteJob(job.job_id);
      onDeleted();
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div>
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-ink truncate">{job.input_filename}</h1>
          <p className="text-xs text-muted mt-1">{formatDate(job.created_at)}</p>
        </div>
        <StatusBadge status={job.status} />
      </div>

      {(job.status === 'pending' || job.status === 'processing') && (
        <div className="flex items-center gap-3 text-sm text-ink-secondary bg-surface border border-border rounded-lg px-4 py-3 mb-6">
          <div className="w-4 h-4 border-2 border-ink/20 border-t-ink rounded-full animate-spin shrink-0" />
          Fayl hazırlanır, bir az gözləyin...
        </div>
      )}

      {job.status === 'completed' && job.available_formats && (
        <div className="flex gap-2 mb-6">
          {job.available_formats.map(fmt => (
            <button
              key={fmt}
              onClick={() => downloadJobFile(job.job_id, fmt, job.input_filename)}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-secondary border border-border rounded-lg px-4 py-2 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
              {fmt.toUpperCase()} yüklə
            </button>
          ))}
        </div>
      )}

      {job.status === 'failed' && job.error_message && (
        <div className="text-sm text-error/90 bg-error/5 border border-error/20 rounded-lg px-4 py-3 mb-6">
          <span className="font-medium">Xəta: </span>
          {job.error_message.length > 200
            ? 'Emal zamanı texniki xəta baş verdi. Yenidən cəhd edin.'
            : job.error_message}
        </div>
      )}

      <div className="pt-4 border-t border-border">
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-error transition-colors cursor-pointer"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
            </svg>
            Sil
          </button>
        ) : (
          <div className="flex items-center gap-3 text-sm">
            <span className="text-ink-secondary">Silinsin? Bu geri qaytarıla bilməz.</span>
            <button
              onClick={handleDelete}
              disabled={deleting}
              className="text-error font-medium cursor-pointer hover:underline disabled:opacity-50"
            >
              {deleting ? 'Silinir...' : 'Bəli, sil'}
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="text-muted cursor-pointer hover:underline"
            >
              Ləğv et
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
