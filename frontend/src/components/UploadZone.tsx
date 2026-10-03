import { useCallback, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { api, ApiError } from '../api/client';

const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.png', '.jpg', '.jpeg'];
const MAX_SIZE_MB = 30;

interface Props {
  onUploaded: (jobId: string) => void;
}

export default function UploadZone({ onUploaded }: Props) {
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const validate = useCallback((file: File): string | null => {
    const ext = '.' + file.name.split('.').pop()?.toLowerCase();
    if (!ACCEPTED_EXTENSIONS.includes(ext)) {
      return `Dəstəklənməyən format: ${ext}. Yalnız ${ACCEPTED_EXTENSIONS.join(', ')} qəbul olunur.`;
    }
    if (file.size > MAX_SIZE_MB * 1024 * 1024) {
      return `Fayl çox böyükdür. Maksimum həcm: ${MAX_SIZE_MB} MB.`;
    }
    return null;
  }, []);

  const upload = useCallback(async (file: File) => {
    const validationError = validate(file);
    if (validationError) {
      setError(validationError);
      return;
    }
    setError('');
    setUploading(true);
    try {
      const res = await api.uploadFile(file);
      onUploaded(res.job_id);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === 'file_too_large') {
          setError(`Fayl çox böyükdür. Maksimum həcm: ${MAX_SIZE_MB} MB.`);
        } else if (err.code === 'unsupported_format') {
          setError(`Dəstəklənməyən format. Yalnız ${ACCEPTED_EXTENSIONS.join(', ')} qəbul olunur.`);
        } else {
          setError(err.message);
        }
      } else {
        setError('Yükləmə zamanı xəta baş verdi.');
      }
    } finally {
      setUploading(false);
    }
  }, [validate, onUploaded]);

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) upload(file);
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) upload(file);
    e.target.value = '';
  }

  return (
    <div className="mb-8">
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className={`
          border-2 border-dashed rounded-xl px-6 py-10 text-center cursor-pointer transition-all
          ${dragging
            ? 'border-accent bg-accent/5'
            : 'border-border hover:border-border-strong hover:bg-surface/50'
          }
          ${uploading ? 'pointer-events-none opacity-60' : ''}
        `}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx,.png,.jpg,.jpeg"
          onChange={handleFileChange}
          className="hidden"
        />

        {uploading ? (
          <>
            <div className="w-6 h-6 border-2 border-ink/20 border-t-ink rounded-full animate-spin mx-auto mb-3" />
            <p className="text-sm text-ink-secondary">Yüklənir...</p>
          </>
        ) : (
          <>
            <div className="mb-3">
              <svg className="w-8 h-8 mx-auto text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z" />
              </svg>
            </div>
            <p className="text-sm text-ink-secondary mb-1">
              Faylı bura sürükləyin və ya <span className="text-ink font-medium underline underline-offset-2">seçin</span>
            </p>
            <p className="text-xs text-muted">
              PDF, DOCX, PNG, JPEG &middot; maks. {MAX_SIZE_MB} MB
            </p>
          </>
        )}
      </div>

      {error && (
        <p className="mt-3 text-sm text-error">{error}</p>
      )}
    </div>
  );
}
