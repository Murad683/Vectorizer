const BASE = '/api';

let _token: string | null = null;
let _onUnauthorized: (() => void) | null = null;
// Coalesces concurrent 401s into a single /auth/refresh call instead of
// firing one refresh attempt per in-flight request.
let _refreshPromise: Promise<boolean> | null = null;

export function setToken(token: string | null) {
  _token = token;
}

export function setOnUnauthorized(cb: () => void) {
  _onUnauthorized = cb;
}

/** Silently exchange the httpOnly refresh cookie for a new access token. */
export async function bootstrapAuth(): Promise<string | null> {
  const ok = await refreshAccessToken();
  return ok ? _token : null;
}

function refreshAccessToken(): Promise<boolean> {
  if (!_refreshPromise) {
    _refreshPromise = (async () => {
      try {
        const res = await fetch(`${BASE}/auth/refresh`, {
          method: 'POST',
          credentials: 'include',
        });
        if (!res.ok) return false;
        const body: LoginResponse = await res.json();
        _token = body.access_token;
        return true;
      } catch {
        return false;
      } finally {
        _refreshPromise = null;
      }
    })();
  }
  return _refreshPromise;
}

async function request<T>(path: string, init: RequestInit = {}, _retried = false): Promise<T> {
  const headers = new Headers(init.headers);
  if (_token) {
    headers.set('Authorization', `Bearer ${_token}`);
  }
  if (!(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include' });

  if (res.status === 401) {
    const isAuthEndpoint = path.startsWith('/auth/');
    if (!_retried && !isAuthEndpoint) {
      const refreshed = await refreshAccessToken();
      if (refreshed) {
        return request<T>(path, init, true);
      }
    }
    _onUnauthorized?.();
    throw new ApiError('not_authenticated', 'Sessiya bitib. Yenidən daxil olun.', 401);
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(
      body?.error_code ?? 'unknown',
      body?.message ?? 'Xəta baş verdi.',
      res.status,
    );
  }

  if (res.headers.get('content-type')?.includes('application/json')) {
    return res.json();
  }
  return res as unknown as T;
}

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface JobResponse {
  job_id: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  input_filename: string;
  created_at: string;
  updated_at: string;
  available_formats?: string[];
  error_message?: string;
}

export interface UserResponse {
  id: string;
  email: string;
  role: 'admin' | 'member';
  is_active: boolean;
  created_at: string;
  job_count: number;
}

export interface JobListPage {
  items: JobResponse[];
  total: number;
}

export const api = {
  login(email: string, password: string) {
    return request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
  },

  logout() {
    return request<{ ok: boolean }>('/auth/logout', { method: 'POST' });
  },

  listJobs(params: { limit?: number; offset?: number; search?: string } = {}) {
    const qs = new URLSearchParams();
    if (params.limit !== undefined) qs.set('limit', String(params.limit));
    if (params.offset !== undefined) qs.set('offset', String(params.offset));
    if (params.search) qs.set('search', params.search);
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return request<JobListPage>(`/jobs${suffix}`);
  },

  getJob(id: string) {
    return request<JobResponse>(`/jobs/${id}`);
  },

  uploadFile(file: File) {
    const form = new FormData();
    form.append('file', file);
    return request<{ job_id: string; status: string }>('/jobs', {
      method: 'POST',
      body: form,
    });
  },

  downloadUrl(jobId: string, format: string) {
    return `${BASE}/jobs/${jobId}/download?format=${format}`;
  },

  downloadFile(jobId: string, format: string) {
    return fetch(`${BASE}/jobs/${jobId}/download?format=${format}`, {
      headers: _token ? { Authorization: `Bearer ${_token}` } : {},
    });
  },

  listUsers() {
    return request<UserResponse[]>('/users');
  },

  createUser(email: string, password: string, role: 'admin' | 'member') {
    return request<UserResponse>('/users', {
      method: 'POST',
      body: JSON.stringify({ email, password, role }),
    });
  },

  deleteJob(jobId: string) {
    return request<void>(`/jobs/${jobId}`, { method: 'DELETE' });
  },

  setUserActive(userId: string, isActive: boolean) {
    return request<UserResponse>(`/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active: isActive }),
    });
  },

  deleteUser(userId: string) {
    return request<void>(`/users/${userId}`, { method: 'DELETE' });
  },
};
