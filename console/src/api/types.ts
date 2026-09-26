export interface Role {
  name: string;
  description: string;
}

export interface AdminUser {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  is_active: boolean;
  roles: string[];
  locked: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface Group {
  id: string;
  name: string;
  description: string;
  roles: string[];
  members: string[];
}

export interface ServiceAccount {
  id: string;
  name: string;
  description: string;
  vault_path: string;
  vault_policy: string;
  disabled: boolean;
  created_by: string | null;
  created_at: string;
}

export interface SecretMetadata {
  path: string;
  current_version: number;
  created_time: string;
  updated_time: string;
  versions: number;
  refs: string[];
}

export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  action: string;
  target: string | null;
  outcome: string;
  detail: Record<string, unknown>;
  ip: string | null;
}

export interface ComponentHealth {
  name: string;
  adapter: string | null;
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

export interface JobRun {
  id: number;
  kind: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  attempts: number;
  max_attempts: number;
  service_account: string | null;
  last_error: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  finished_at: string | null;
}
