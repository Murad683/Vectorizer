import { STATUS_LABELS } from '../lib/jobUtils';

interface Props {
  status: string;
}

export default function StatusBadge({ status }: Props) {
  const base = 'inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full';
  switch (status) {
    case 'completed':
      return <span className={`${base} bg-success/10 text-success`}>{STATUS_LABELS[status]}</span>;
    case 'failed':
      return <span className={`${base} bg-error/10 text-error`}>{STATUS_LABELS[status]}</span>;
    case 'processing':
      return (
        <span className={`${base} bg-processing/10 text-processing`}>
          <span className="w-1.5 h-1.5 rounded-full bg-processing animate-pulse-slow" />
          {STATUS_LABELS[status]}
        </span>
      );
    default:
      return <span className={`${base} bg-surface text-muted`}>{STATUS_LABELS[status] ?? status}</span>;
  }
}

/** Small dot-only variant for compact contexts like the sidebar. */
export function StatusDot({ status }: Props) {
  const colorClass: Record<string, string> = {
    completed: 'bg-success',
    failed: 'bg-error',
    processing: 'bg-processing animate-pulse-slow',
    pending: 'bg-muted',
  };
  return (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${colorClass[status] ?? 'bg-muted'}`}
    />
  );
}
