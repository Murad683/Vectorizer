import { api } from '../api/client';
import type { JobResponse } from '../api/client';

export const STATUS_LABELS: Record<string, string> = {
  awaiting_selection: 'Seçim gözləyir',
  pending: 'Gözləyir',
  processing: 'Emal olunur',
  completed: 'Hazırdır',
  failed: 'Xəta',
};

/**
 * Reads a page list such as "1-5, 8" into sorted, unique page numbers within
 * 1..max. Returns null when the text is not a valid list (or is empty).
 */
export function parsePageRange(text: string, max: number): number[] | null {
  const pages = new Set<number>();
  for (const raw of text.split(',')) {
    const part = raw.trim();
    if (!part) continue;
    const m = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(part);
    if (!m) return null;
    const first = Number(m[1]);
    const last = m[2] === undefined ? first : Number(m[2]);
    if (first < 1 || last < first || last > max) return null;
    for (let n = first; n <= last; n++) pages.add(n);
  }
  return pages.size ? [...pages].sort((a, b) => a - b) : null;
}

/** File size for a list row: "340 KB", "2.4 MB". */
export function formatSize(bytes: number): string {
  if (bytes <= 0) return '0 KB';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString('az-AZ', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Short relative timestamp for the sidebar ("indi", "5 dəq", "3 gün"). */
export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));

  if (diffSec < 60) return 'indi';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} dəq`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour} saat`;
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 7) return `${diffDay} gün`;
  return new Date(iso).toLocaleDateString('az-AZ', { day: '2-digit', month: '2-digit' });
}

export interface JobGroup {
  label: string;
  jobs: JobResponse[];
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * Groups an already-fetched, already-ordered job list into recency
 * buckets for the sidebar — a pure display transform, no re-fetching.
 * "Bu həftə" is the 6 calendar days before today (a 7-day window
 * together with "Bu gün"); anything older falls into "Daha əvvəl".
 * Empty buckets are omitted rather than rendered with an empty header.
 */
export function groupJobsByRecency(jobs: JobResponse[]): JobGroup[] {
  const todayStart = startOfDay(new Date());
  const weekStart = new Date(todayStart);
  weekStart.setDate(weekStart.getDate() - 6);

  const today: JobResponse[] = [];
  const thisWeek: JobResponse[] = [];
  const older: JobResponse[] = [];

  for (const job of jobs) {
    const createdDayStart = startOfDay(new Date(job.created_at));
    if (createdDayStart.getTime() === todayStart.getTime()) {
      today.push(job);
    } else if (createdDayStart.getTime() >= weekStart.getTime()) {
      thisWeek.push(job);
    } else {
      older.push(job);
    }
  }

  const groups: JobGroup[] = [];
  if (today.length) groups.push({ label: 'Bu gün', jobs: today });
  if (thisWeek.length) groups.push({ label: 'Bu həftə', jobs: thisWeek });
  if (older.length) groups.push({ label: 'Daha əvvəl', jobs: older });
  return groups;
}

export async function downloadJobFile(jobId: string, format: string, filename: string) {
  const res = await api.downloadFile(jobId, format);
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const stem = filename.replace(/\.[^.]+$/, '');
  a.download = `${stem}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
