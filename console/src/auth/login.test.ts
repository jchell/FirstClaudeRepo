import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const user = { id: '1', username: 'ann', roles: ['admin'] };

function tokenResponse(token: string, expiresIn: number) {
  return new Response(JSON.stringify({ access_token: token, token_type: 'bearer', expires_in: expiresIn, user }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('auth/login', () => {
  let calls: string[];
  let auth: typeof import('./login');

  beforeEach(async () => {
    vi.resetModules();
    calls = [];
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push(url);
      if (url.endsWith('/login')) {
        const body = JSON.parse(String(init.body));
        if (body.password === 'locked') return new Response('', { status: 423 });
        if (body.password !== 'pw ') return new Response('', { status: 401 });
        return tokenResponse('t1', 10);
      }
      if (url.endsWith('/refresh')) return tokenResponse('t2', 3600);
      return new Response(null, { status: 204 });
    });
    auth = await import('./login');
  });

  afterEach(() => vi.unstubAllGlobals());

  it('maps failures to reasons without contacting the server for empty input', async () => {
    await expect(auth.login('ann', 'bad')).rejects.toMatchObject({ reason: 'invalid_credentials' });
    await expect(auth.login('ann', 'locked')).rejects.toMatchObject({ reason: 'account_locked' });
    const before = calls.length;
    await expect(auth.login('  ', 'x')).rejects.toMatchObject({ reason: 'invalid_input' });
    expect(calls.length).toBe(before);
  });

  it('trims the username but sends the password exactly as typed', async () => {
    expect((await auth.login(' ann ', 'pw ')).username).toBe('ann');
    expect(auth.hasRole('admin')).toBe(true);
  });

  it('refreshes a nearly expired token once for concurrent callers', async () => {
    await auth.login('ann', 'pw ');
    calls = [];
    const [a, b] = await Promise.all([auth.getAccessToken(), auth.getAccessToken()]);
    expect([a, b]).toEqual(['t2', 't2']);
    expect(calls.filter((c) => c.endsWith('/refresh'))).toHaveLength(1);
    expect(await auth.getAccessToken()).toBe('t2');
  });

  it('notifies listeners and clears the session on logout', async () => {
    const seen: (string | null)[] = [];
    auth.onAuthChange((u) => seen.push(u?.username ?? null));
    await auth.login('ann', 'pw ');
    await auth.logout();
    expect(auth.getCurrentUser()).toBeNull();
    expect(seen).toEqual(['ann', null]);
  });
});
