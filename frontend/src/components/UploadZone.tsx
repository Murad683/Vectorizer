import { useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { api, ApiError } from '../api/client';
import { formatSize } from '../lib/jobUtils';

const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.png', '.jpg', '.jpeg'];
const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg'];
const MAX_SIZE_MB = 30;

// The server refuses further jobs for a while: stop sending, keep the rest.
const LIMIT_CODES = ['too_many_active_jobs', 'daily_job_limit'];

interface Props {
  /** Called with the first created job once every chosen file has been sent. */
  onUploaded: (jobId: string) => void;
  /** Called after each file is sent, so the job list can refresh at once. */
  onProgress?: () => void;
}

interface StagedFile {
  id: number;
  file: File;
  /** Set when the file can never be sent (wrong type or too big). */
  invalid: boolean;
  /** Why the file is invalid, or why the last attempt to send it failed. */
  error: string;
  uploading: boolean;
  /** Object URL of a small preview (images only). */
  preview: string | null;
}

function extensionOf(file: File): string {
  return '.' + (file.name.split('.').pop() ?? '').toLowerCase();
}

function isImage(file: File): boolean {
  return IMAGE_EXTENSIONS.includes(extensionOf(file));
}

function fileKey(file: File): string {
  return `${file.name}|${file.size}|${file.lastModified}`;
}

function validate(file: File): string | null {
  const ext = extensionOf(file);
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    return `Dəstəklənməyən format: ${ext}. Yalnız ${ACCEPTED_EXTENSIONS.join(', ')} qəbul olunur.`;
  }
  if (file.size > MAX_SIZE_MB * 1024 * 1024) {
    return `Fayl çox böyükdür. Maksimum həcm: ${MAX_SIZE_MB} MB.`;
  }
  return null;
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'file_too_large') {
      return `Fayl çox böyükdür. Maksimum həcm: ${MAX_SIZE_MB} MB.`;
    }
    if (err.code === 'unsupported_format') {
      return `Dəstəklənməyən format. Yalnız ${ACCEPTED_EXTENSIONS.join(', ')} qəbul olunur.`;
    }
    return err.message;
  }
  return 'Yükləmə zamanı xəta baş verdi.';
}

export default function UploadZone({ onUploaded, onProgress }: Props) {
  const [dragging, setDragging] = useState(false);
  const [items, setItems] = useState<StagedFile[]>([]);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState('');
  // "All images in one document" (only offered when 2+ valid images are listed).
  const [mergeImages, setMergeImages] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);
  const itemsRef = useRef<StagedFile[]>([]);

  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  // Free the preview pictures when the zone goes away.
  useEffect(
    () => () => {
      for (const item of itemsRef.current) {
        if (item.preview) URL.revokeObjectURL(item.preview);
      }
    },
    [],
  );

  function addFiles(files: File[]) {
    if (!files.length) return;
    const known = new Set(items.map(i => fileKey(i.file)));
    const added: StagedFile[] = [];
    let repeated = 0;
    for (const file of files) {
      const key = fileKey(file);
      if (known.has(key)) {
        repeated += 1;
        continue;
      }
      known.add(key);
      const error = validate(file);
      added.push({
        id: nextId.current++,
        file,
        invalid: error !== null,
        error: error ?? '',
        uploading: false,
        preview:
          error === null && isImage(file) ? URL.createObjectURL(file) : null,
      });
    }
    setNotice(repeated ? 'Eyni fayl artıq siyahıdadır.' : '');
    if (added.length) setItems(prev => [...prev, ...added]);
  }

  function removeItem(id: number) {
    const target = items.find(i => i.id === id);
    if (target?.preview) URL.revokeObjectURL(target.preview);
    setItems(prev => prev.filter(i => i.id !== id));
    setNotice('');
  }

  function patchItem(id: number, patch: Partial<StagedFile>) {
    setItems(prev => prev.map(i => (i.id === id ? { ...i, ...patch } : i)));
  }

  // Move an image one place up or down among the listed images (page order).
  function moveImage(id: number, step: -1 | 1) {
    setItems(prev => {
      const slots = prev.flatMap((item, index) =>
        !item.invalid && isImage(item.file) ? [index] : [],
      );
      const at = slots.findIndex(index => prev[index].id === id);
      const to = at + step;
      if (at < 0 || to < 0 || to >= slots.length) return prev;
      const next = [...prev];
      [next[slots[at]], next[slots[to]]] = [next[slots[to]], next[slots[at]]];
      return next;
    });
  }

  async function startUpload() {
    const queue = items.filter(i => !i.invalid);
    if (!queue.length || sending) return;
    setSending(true);
    setNotice('');
    let sent = 0;
    let firstJobId: string | null = null;
    let stoppedByLimit = false;

    // A unit is what becomes one job: all the images together when they are
    // merged, otherwise one file.
    const units: StagedFile[][] = merging
      ? [images, ...queue.filter(i => !images.includes(i)).map(i => [i])]
      : queue.map(i => [i]);

    for (const unit of units) {
      for (const entry of unit) patchItem(entry.id, { uploading: true, error: '' });
      try {
        const res =
          unit.length > 1
            ? await api.uploadMerged(unit.map(i => i.file))
            : await api.uploadFile(unit[0].file);
        firstJobId ??= res.job_id;
        sent += unit.length;
        for (const entry of unit) {
          if (entry.preview) URL.revokeObjectURL(entry.preview);
        }
        const done = new Set(unit.map(i => i.id));
        setItems(prev => prev.filter(i => !done.has(i.id)));
        onProgress?.();
      } catch (err) {
        // A failed merge shows its message once, on the first image of the set.
        unit.forEach((entry, index) =>
          patchItem(entry.id, {
            uploading: false,
            error: index === 0 ? describeError(err) : '',
          }),
        );
        if (err instanceof ApiError && LIMIT_CODES.includes(err.code ?? '')) {
          stoppedByLimit = true;
          break;
        }
      }
    }

    setSending(false);
    const left = queue.length - sent;
    if (left === 0 && firstJobId) {
      onUploaded(firstJobId);
      return;
    }
    if (stoppedByLimit) {
      setNotice(
        sent > 0
          ? `${sent} fayl göndərildi. Qalanları əvvəlki işlər bitəndən sonra «Başla» ilə göndərin.`
          : 'İndi yeni iş göndərmək olmur. Əvvəlki işlər bitəndən sonra «Başla» ilə yenidən cəhd edin.',
      );
    } else if (sent > 0) {
      setNotice(`${sent} fayl göndərildi, ${left} faylda xəta var.`);
    }
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(Array.from(e.dataTransfer.files));
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    addFiles(Array.from(e.target.files ?? []));
    e.target.value = '';
  }

  const ready = items.filter(i => !i.invalid).length;
  const staged = items.length > 0;
  const images = items.filter(i => !i.invalid && isImage(i.file));
  const canMerge = images.length >= 2;
  const merging = mergeImages && canMerge;

  return (
    <div className="mb-8">
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className={`
          border-2 border-dashed rounded-xl px-6 text-center cursor-pointer transition-all
          ${staged ? 'py-5' : 'py-10'}
          ${dragging
            ? 'border-accent bg-accent/5'
            : 'border-border hover:border-border-strong hover:bg-surface/50'
          }
          ${sending ? 'pointer-events-none opacity-60' : ''}
        `}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.png,.jpg,.jpeg"
          onChange={handleFileChange}
          className="hidden"
        />

        {!staged && (
          <div className="mb-3">
            <svg className="w-8 h-8 mx-auto text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z" />
            </svg>
          </div>
        )}
        <p className="text-sm text-ink-secondary mb-1">
          {staged ? 'Daha fayl əlavə edin: ' : 'Faylları bura sürükləyin və ya '}
          <span className="text-ink font-medium underline underline-offset-2">seçin</span>
        </p>
        {!staged && (
          <p className="text-xs text-muted">
            PDF, DOCX, PNG, JPEG &middot; hər fayl maks. {MAX_SIZE_MB} MB
          </p>
        )}
      </div>

      {staged && (
        <>
          <ul className="mt-4 border border-border rounded-xl divide-y divide-border" aria-label="Seçilmiş fayllar">
            {items.map(item => {
              const place = merging ? images.indexOf(item) : -1; // page number - 1
              return (
              <li key={item.id} className="flex items-center gap-3 px-3 py-2.5">
                {place >= 0 && (
                  <span className="w-5 shrink-0 text-center text-xs font-semibold text-ink-secondary">
                    {place + 1}.
                  </span>
                )}
                {item.preview ? (
                  <img
                    src={item.preview}
                    alt=""
                    className="w-10 h-10 rounded object-cover border border-border shrink-0"
                  />
                ) : (
                  <span className="w-10 h-10 rounded bg-surface border border-border shrink-0 flex items-center justify-center text-[10px] font-semibold text-muted">
                    {extensionOf(item.file).slice(1).toUpperCase()}
                  </span>
                )}

                <div className="min-w-0 flex-1">
                  <p className="text-sm text-ink truncate" title={item.file.name}>
                    {item.file.name}
                  </p>
                  {item.error ? (
                    <p className="text-xs text-error">{item.error}</p>
                  ) : (
                    <p className="text-xs text-muted">{formatSize(item.file.size)}</p>
                  )}
                </div>

                {item.uploading ? (
                  <div
                    className="w-4 h-4 border-2 border-ink/20 border-t-ink rounded-full animate-spin shrink-0"
                    role="status"
                    aria-label="Yüklənir"
                  />
                ) : (
                  <div className="flex items-center shrink-0">
                    {place >= 0 && (
                      <>
                        <button
                          onClick={() => moveImage(item.id, -1)}
                          disabled={sending || place === 0}
                          aria-label={`${item.file.name} şəklini yuxarı apar`}
                          title="Yuxarı"
                          className="p-1.5 rounded-lg text-muted hover:text-ink hover:bg-surface transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-not-allowed"
                        >
                          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.75}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 15.75 7.5-7.5 7.5 7.5" />
                          </svg>
                        </button>
                        <button
                          onClick={() => moveImage(item.id, 1)}
                          disabled={sending || place === images.length - 1}
                          aria-label={`${item.file.name} şəklini aşağı apar`}
                          title="Aşağı"
                          className="p-1.5 rounded-lg text-muted hover:text-ink hover:bg-surface transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-not-allowed"
                        >
                          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.75}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
                          </svg>
                        </button>
                      </>
                    )}
                    <button
                      onClick={() => removeItem(item.id)}
                      disabled={sending}
                      aria-label={`${item.file.name} faylını siyahıdan sil`}
                      title="Sil"
                      className="p-1.5 rounded-lg text-muted hover:text-ink hover:bg-surface transition-colors disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.75}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                )}
              </li>
              );
            })}
          </ul>

          {canMerge && (
            <fieldset className="mt-4 flex flex-col gap-2" disabled={sending}>
              <legend className="sr-only">Şəkillərin necə işlənəcəyi</legend>
              <label className="flex items-center gap-2 text-sm text-ink-secondary cursor-pointer">
                <input
                  type="radio"
                  name="image-mode"
                  checked={!merging}
                  onChange={() => setMergeImages(false)}
                  className="w-4 h-4 accent-[#fee500] cursor-pointer"
                />
                Hər biri ayrı sənəd
              </label>
              <label className="flex items-center gap-2 text-sm text-ink-secondary cursor-pointer">
                <input
                  type="radio"
                  name="image-mode"
                  checked={merging}
                  onChange={() => setMergeImages(true)}
                  className="w-4 h-4 accent-[#fee500] cursor-pointer"
                />
                Hamısı bir sənəd ({images.length} şəkil, göstərilən sıra ilə)
              </label>
            </fieldset>
          )}

          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={startUpload}
              disabled={sending || ready === 0}
              className="bg-accent text-accent-ink font-medium text-sm rounded-lg px-5 py-2.5 hover:brightness-110 active:brightness-95 transition-all disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
            >
              {sending ? 'Yüklənir...' : ready > 1 ? `Başla (${ready} fayl)` : 'Başla'}
            </button>
            {ready === 0 && (
              <span className="text-xs text-muted">Göndəriləcək uyğun fayl yoxdur.</span>
            )}
          </div>
        </>
      )}

      {notice && (
        <p className="mt-3 text-sm text-ink-secondary" aria-live="polite">
          {notice}
        </p>
      )}
    </div>
  );
}
