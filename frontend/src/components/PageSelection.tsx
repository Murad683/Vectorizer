import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { JobResponse, PageItem, PageKind } from '../api/client';
import { parsePageRange } from '../lib/jobUtils';

interface Props {
  job: JobResponse;
  /** Called with the queued job once the user has started it. */
  onStarted: (job: JobResponse) => void;
}

const KIND_LABELS: Record<PageKind, string> = {
  scan: 'Skan',
  text: 'Mətn',
  blank: 'Boş',
};

const KIND_STYLES: Record<PageKind, string> = {
  scan: 'bg-accent/25 text-ink',
  text: 'bg-surface text-ink-secondary',
  blank: 'bg-surface text-muted',
};

function defaultSelection(pages: PageItem[]): Set<number> {
  return new Set(pages.filter(p => p.kind !== 'blank').map(p => p.n));
}

export default function PageSelection({ job, onStarted }: Props) {
  const [pages, setPages] = useState<PageItem[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [keepUnselected, setKeepUnselected] = useState(true);
  const [rangeText, setRangeText] = useState('');
  const [rangeError, setRangeError] = useState('');
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPages(null);
    setLoadError('');
    api
      .getJobPages(job.job_id)
      .then(res => {
        if (cancelled) return;
        setPages(res.pages);
        setSelected(defaultSelection(res.pages));
      })
      .catch(err => {
        if (cancelled) return;
        setLoadError(
          err instanceof ApiError ? err.message : 'Səhifələr yüklənmədi. Yenidən cəhd edin.',
        );
      });
    return () => {
      cancelled = true;
    };
  }, [job.job_id, attempt]);

  const stats = useMemo(() => {
    const all = pages ?? [];
    const chosen = all.filter(p => selected.has(p.n));
    const rest = all.filter(p => !selected.has(p.n));
    return {
      total: all.length,
      chosen: chosen.length,
      read: chosen.filter(p => p.kind === 'scan').length,
      // Pages not chosen: a blank one is always left out, never kept as a picture.
      others: rest.filter(p => p.kind !== 'blank').length,
      blankLeftOut: rest.filter(p => p.kind === 'blank').length,
      scans: all.filter(p => p.kind === 'scan').length,
    };
  }, [pages, selected]);

  // What happens to the pages that are not chosen, in words.
  const restText = (() => {
    if (!keepUnselected) {
      const left = stats.others + stats.blankLeftOut;
      return left > 0 ? `${left} səhifə atılacaq.` : '';
    }
    const parts: string[] = [];
    if (stats.others > 0) parts.push(`${stats.others} səhifə şəkil kimi qalacaq`);
    if (stats.blankLeftOut > 0) parts.push(`${stats.blankLeftOut} boş səhifə atılacaq`);
    return parts.length ? `${parts.join(', ')}.` : '';
  })();

  function toggle(n: number) {
    setConfirmEmpty(false);
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });
  }

  function choose(next: Set<number>) {
    setConfirmEmpty(false);
    setError('');
    setSelected(next);
  }

  function applyRange() {
    if (!pages) return;
    const parsed = parsePageRange(rangeText, pages.length);
    if (!parsed) {
      setRangeError(`Düzgün aralıq yazın, məsələn: 1-5, 8 (səhifələr 1-dən ${pages.length}-ə qədərdir).`);
      return;
    }
    setRangeError('');
    choose(new Set(parsed));
  }

  async function start(confirm: boolean) {
    if (!pages) return;
    if (selected.size === 0 && !keepUnselected) {
      setError('Heç bir səhifə seçilməyib və seçilməyənlər atılır: emal ediləcək bir şey yoxdur.');
      return;
    }
    if (selected.size === 0 && !confirm) {
      setConfirmEmpty(true);
      return;
    }
    setStarting(true);
    setError('');
    try {
      const started = await api.startJob(job.job_id, {
        pages: [...selected].sort((a, b) => a - b),
        keep_unselected: keepUnselected,
        confirm_empty: confirm,
      });
      onStarted(started);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'selection_empty') {
        setConfirmEmpty(true);
      } else {
        setError(err instanceof ApiError ? err.message : 'Başlatmaq alınmadı. Yenidən cəhd edin.');
      }
    } finally {
      setStarting(false);
    }
  }

  if (loadError) {
    return (
      <div className="text-sm bg-error/5 border border-error/20 rounded-lg px-4 py-3 mb-6">
        <p className="text-error/90 mb-2">{loadError}</p>
        <button
          onClick={() => setAttempt(a => a + 1)}
          className="text-ink-secondary underline underline-offset-2 cursor-pointer"
        >
          Yenidən yüklə
        </button>
      </div>
    );
  }

  if (!pages) {
    return (
      <div className="flex items-center gap-3 text-sm text-ink-secondary bg-surface border border-border rounded-lg px-4 py-3 mb-6">
        <div className="w-4 h-4 border-2 border-ink/20 border-t-ink rounded-full animate-spin shrink-0" />
        Səhifələr hazırlanır...
      </div>
    );
  }

  const buttonClass =
    'text-xs font-medium text-ink-secondary border border-border rounded-lg px-3 py-1.5 hover:bg-surface-hover hover:border-border-strong transition-colors cursor-pointer';

  return (
    <section className="mb-6" aria-label="Səhifə seçimi">
      <h2 className="text-base font-semibold text-ink">Hansı səhifələr oxunsun?</h2>
      <p className="text-sm text-ink-secondary mt-1 mb-4">
        {stats.total} səhifə, {stats.scans} skan. Seçilən səhifələr redaktə oluna bilən formata
        çevrilir. Seçilməyənlər nəticəyə olduğu kimi şəkil kimi daxil edilir (və ya atılır), süni
        intellektə göndərilmir.
      </p>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <button className={buttonClass} onClick={() => choose(defaultSelection(pages))}>
          Hamısını seç
        </button>
        <button className={buttonClass} onClick={() => choose(new Set())}>
          Heç birini
        </button>
        <button
          className={buttonClass}
          onClick={() => choose(new Set(pages.filter(p => p.kind === 'scan').map(p => p.n)))}
        >
          Yalnız skanlar
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-1">
        <input
          value={rangeText}
          onChange={e => setRangeText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') applyRange();
          }}
          placeholder="Aralıq: 1-5, 8"
          aria-label="Səhifə aralığı"
          className="w-44 rounded-lg bg-surface border border-border px-3 py-1.5 text-sm text-ink placeholder:text-muted outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30 transition-colors"
        />
        <button className={buttonClass} onClick={applyRange}>
          Yalnız bunları seç
        </button>
      </div>
      {rangeError && <p className="text-xs text-error mb-2">{rangeError}</p>}

      <ul className="grid grid-cols-2 sm:grid-cols-3 gap-3 my-4">
        {pages.map(p => {
          const on = selected.has(p.n);
          return (
            <li key={p.n}>
              <label
                className={`block rounded-lg border p-2 cursor-pointer transition-colors ${
                  on ? 'border-accent bg-accent/5' : 'border-border hover:border-border-strong'
                }`}
              >
                <div className="flex items-center justify-between gap-2 mb-2">
                  <span className="flex items-center gap-2 text-sm text-ink">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggle(p.n)}
                      aria-label={`Səhifə ${p.n}`}
                      className="w-4 h-4 accent-[#fee500] cursor-pointer"
                    />
                    {p.n}
                  </span>
                  <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${KIND_STYLES[p.kind]}`}>
                    {KIND_LABELS[p.kind]}
                  </span>
                </div>
                <img
                  src={p.thumb}
                  alt={`Səhifə ${p.n}`}
                  loading="lazy"
                  className={`w-full rounded border border-border bg-white ${on ? '' : 'opacity-50'}`}
                />
              </label>
            </li>
          );
        })}
      </ul>

      <label className="flex items-start gap-2 text-sm text-ink-secondary mb-4 cursor-pointer">
        <input
          type="checkbox"
          checked={keepUnselected}
          onChange={e => {
            setKeepUnselected(e.target.checked);
            setConfirmEmpty(false);
            setError('');
          }}
          className="w-4 h-4 mt-0.5 accent-[#fee500] cursor-pointer"
        />
        Seçilməyən səhifələr nəticəyə şəkil kimi daxil edilsin (sənəd tam qalsın)
      </label>

      <p className="text-sm text-ink-secondary mb-4" aria-live="polite">
        {stats.chosen} səhifə seçilib
        {stats.read > 0 ? `, ${stats.read} skan səhifə süni intellektlə oxunacaq` : ''}.{' '}
        {restText}
      </p>

      {confirmEmpty ? (
        <div className="text-sm bg-surface border border-border rounded-lg px-4 py-3 mb-4">
          <p className="text-ink mb-3">
            Heç bir səhifə seçilməyib. Davam etsəniz bütün səhifələr redaktə olunmayan şəkil kimi
            qalacaq. Davam edək?
          </p>
          <div className="flex items-center gap-3">
            <button
              onClick={() => start(true)}
              disabled={starting}
              className="bg-accent text-accent-ink font-medium text-sm rounded-lg px-4 py-2 hover:brightness-110 transition-all disabled:opacity-50 cursor-pointer"
            >
              {starting ? 'Başlayır...' : 'Bəli, davam et'}
            </button>
            <button
              onClick={() => setConfirmEmpty(false)}
              disabled={starting}
              className="text-muted hover:underline cursor-pointer"
            >
              Geri
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => start(false)}
          disabled={starting}
          className="bg-accent text-accent-ink font-medium text-sm rounded-lg px-5 py-2.5 hover:brightness-110 active:brightness-95 transition-all disabled:opacity-50 cursor-pointer disabled:cursor-wait"
        >
          {starting ? 'Başlayır...' : 'Başla'}
        </button>
      )}

      {error && <p className="mt-3 text-sm text-error">{error}</p>}
    </section>
  );
}
