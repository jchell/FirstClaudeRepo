import { api } from './client';
import type { Task } from './types';

/**
 * Source operations (test, discover, preview) run on the worker as the connection's
 * service account, because the API itself can't read connection secrets. This polls
 * the task until it finishes.
 */
export async function runTask<R>(start: Promise<Task<R>>, timeoutMs = 90_000): Promise<Task<R>> {
  const { task_id } = await start;
  const deadline = Date.now() + timeoutMs;
  let delay = 400;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, delay));
    const t = await api<Task<R>>(`/api/tasks/${task_id}`);
    if (t.status === 'succeeded' || t.status === 'failed') return t;
    delay = Math.min(delay * 1.5, 2000);
  }
  throw new Error('The worker did not answer in time. Is the worker running?');
}

export const fmtNumber = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString());

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} m ${Math.round((ms % 60_000) / 1000)} s`;
}

export const fmtTime = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : '—');
