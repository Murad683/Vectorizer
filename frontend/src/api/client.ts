const BASE = '/api';

let _token: string | null = null;
let _onUnauthorized: (() => void) | null = null;

export function setToken(token: string | null) {
  _token = token;
}

export function setOnUnauthorized(cb: () => void) {
  _onUnauthorized = cb;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (_token) {
    headers.set('Authorization', `Bearer ${_token}`);
  }
  if (!(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const res = await fetch(`${BASE}${path}`, { ...init, headers });

  if (res.status === 401) {
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

export const api = {
  login(email: string, password: string) {
    return request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
  },

  listJobs() {
    return request<JobResponse[]>('/jobs');
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
