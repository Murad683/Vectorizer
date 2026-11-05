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
          error === null && IMAGE_EXTENSIONS.includes(extensionOf(file))
            ? URL.createObjectURL(file)
            : null,
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

  async function startUpload() {
    const queue = items.filter(i => !i.invalid);
    if (!queue.length || sending) return;
    setSending(true);
    setNotice('');
    let sent = 0;
    let firstJobId: string | null = null;
    let stoppedByLimit = false;

    for (const entry of queue) {
      patchItem(entry.id, { uploading: true, error: '' });
      try {
        const res = await api.uploadFile(entry.file);
        firstJobId ??= res.job_id;
        sent += 1;
        if (entry.preview) URL.revokeObjectURL(entry.preview);
        setItems(prev => prev.filter(i => i.id !== entry.id));
        onProgress?.();
      } catch (err) {
        patchItem(entry.id, { uploading: false, error: describeError(err) });
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
            {items.map(item => (
              <li key={item.id} className="flex items-center gap-3 px-3 py-2.5">
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
                  <button
                    onClick={() => removeItem(item.id)}
                    disabled={sending}
                    aria-label={`${item.file.name} faylını siyahıdan sil`}
                    title="Sil"
                    className="shrink-0 p-1.5 rounded-lg text-muted hover:text-ink hover:bg-surface transition-colors disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.75}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
                    </svg>
                  </button>
                )}
              </li>
            ))}
          </ul>

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
