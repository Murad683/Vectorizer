import { api } from '../api/client';

export const STATUS_LABELS: Record<string, string> = {
  pending: 'Gözləyir',
  processing: 'Emal olunur',
  completed: 'Hazırdır',
  failed: 'Xəta',
};

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
