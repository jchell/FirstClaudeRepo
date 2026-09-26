import { getAccessToken, logout, refreshSession } from '../auth/login';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function send(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return fetch(path, { ...init, headers });
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join('.')}: ${d.msg}`)
        .join('; ');
    }
  }
  return fallback;
}

/** Calls the platform API with the current access token, refreshing once on 401. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await send(path, init, await getAccessToken());
  if (res.status === 401) {
    const refreshed = await refreshSession();
    if (!refreshed) {
      await logout();
      throw new ApiError(401, 'Your session has expired. Sign in again.');
    }
    res = await send(path, init, refreshed.accessToken);
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => undefined);
  if (!res.ok) throw new ApiError(res.status, errorMessage(body, `Request failed (${res.status})`));
  return body as T;
}

export const json = (value: unknown): RequestInit['body'] => JSON.stringify(value);
