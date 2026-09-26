/**
 * Console login and session handling.
 *
 * Talks to the platform API's auth endpoints (see docs/PLATFORM_PLAN.md, Phase 0):
 *   POST /api/auth/login    { username, password } -> LoginResponse, sets the refresh cookie
 *   POST /api/auth/refresh  (refresh cookie)       -> LoginResponse
 *   POST /api/auth/logout   (refresh cookie)       -> 204, clears the refresh cookie
 *
 * The access token (a JWT the API signs via Vault transit) is kept in memory only,
 * never in localStorage or sessionStorage, so injected scripts can't read it from
 * storage. The refresh token lives in an httpOnly cookie set by the API, which is
 * how a page reload recovers the session.
 */

export interface User {
  id: string;
  username: string;
  roles: string[];
}

interface LoginResponse {
  access_token: string;
  token_type: 'bearer';
  /** Access token lifetime in seconds. */
  expires_in: number;
  user: User;
}

interface Session {
  accessToken: string;
  /** Epoch milliseconds after which the access token must not be used. */
  expiresAt: number;
  user: User;
}

export type LoginErrorReason =
  | 'invalid_input'
  | 'invalid_credentials'
  | 'account_locked'
  | 'rate_limited'
  | 'network'
  | 'server';

export class LoginError extends Error {
  constructor(
    readonly reason: LoginErrorReason,
    message: string,
  ) {
    super(message);
    this.name = 'LoginError';
  }
}

const API_BASE = '/api/auth';

/** Refresh this long before expiry so requests in flight don't carry a dead token. */
const EXPIRY_SKEW_MS = 30_000;

let session: Session | null = null;
let refreshInFlight: Promise<Session | null> | null = null;
const listeners = new Set<(user: User | null) => void>();

function setSession(next: Session | null): void {
  session = next;
  const user = next?.user ?? null;
  listeners.forEach((listener) => listener(user));
}

function toSession(body: LoginResponse): Session {
  if (!body.access_token || typeof body.expires_in !== 'number' || !body.user) {
    throw new LoginError('server', 'The server returned an invalid login response.');
  }
  return {
    accessToken: body.access_token,
    expiresAt: Date.now() + body.expires_in * 1000,
    user: body.user,
  };
}

async function postAuth(path: string, body?: unknown): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      credentials: 'include', // send/receive the httpOnly refresh cookie
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new LoginError('network', 'Could not reach the server. Check your connection.');
  }
}

/**
 * Signs in with a username and password. On success the session is stored in
 * memory and the signed-in user is returned; on failure a LoginError is thrown.
 * The error messages are deliberately generic so they don't reveal whether a
 * username exists.
 */
export async function login(username: string, password: string): Promise<User> {
  const name = username.trim();
  // Passwords are sent exactly as typed; trimming would silently change them.
  if (!name || !password) {
    throw new LoginError('invalid_input', 'Enter your username and password.');
  }

  const res = await postAuth('/login', { username: name, password });

  if (res.status === 401) {
    throw new LoginError('invalid_credentials', 'Incorrect username or password.');
  }
  if (res.status === 423) {
    throw new LoginError('account_locked', 'This account is locked. Contact an administrator.');
  }
  if (res.status === 429) {
    throw new LoginError('rate_limited', 'Too many sign-in attempts. Try again in a few minutes.');
  }
  if (!res.ok) {
    throw new LoginError('server', 'Sign-in failed due to a server error. Try again later.');
  }

  const next = toSession((await res.json()) as LoginResponse);
  setSession(next);
  return next.user;
}

/**
 * Exchanges the refresh cookie for a new access token. Concurrent callers share
 * one request. Resolves to null (and clears the session) if the refresh token
 * is missing, expired or revoked.
 */
export function refreshSession(): Promise<Session | null> {
  refreshInFlight ??= (async () => {
    try {
      const res = await postAuth('/refresh');
      if (!res.ok) {
        setSession(null);
        return null;
      }
      const next = toSession((await res.json()) as LoginResponse);
      setSession(next);
      return next;
    } catch (err) {
      // A network blip shouldn't sign the user out; keep the session as is.
      if (err instanceof LoginError && err.reason === 'network') return session;
      setSession(null);
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/**
 * Returns a usable access token for an Authorization header, refreshing it if
 * it is missing or about to expire. Returns null when the user is signed out.
 */
export async function getAccessToken(): Promise<string | null> {
  if (session && Date.now() < session.expiresAt - EXPIRY_SKEW_MS) {
    return session.accessToken;
  }
  const refreshed = await refreshSession();
  return refreshed && Date.now() < refreshed.expiresAt ? refreshed.accessToken : null;
}

/** Signs out locally and asks the API to revoke the refresh token. */
export async function logout(): Promise<void> {
  setSession(null);
  try {
    await postAuth('/logout');
  } catch {
    // Already signed out locally; the refresh token will expire on its own.
  }
}

export function getCurrentUser(): User | null {
  return session?.user ?? null;
}

export function hasRole(role: string): boolean {
  return session?.user.roles.includes(role) ?? false;
}

/** Subscribes to sign-in/sign-out changes. Returns an unsubscribe function. */
export function onAuthChange(listener: (user: User | null) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
