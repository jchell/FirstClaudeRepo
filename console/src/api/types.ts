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

// ---------------------------------------------------------------- Phase 1

export interface ConnectionType {
  type: string;
  label: string;
  category: 'file' | 'database' | 'nosql' | 'api' | 'event';
  secret_fields: string[];
  config_schema: JsonSchema;
}

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  default?: unknown;
  enum?: unknown[];
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  format?: string;
  writeOnly?: boolean;
  'x-secret'?: boolean;
  'x-secret-fields'?: string[];
  additionalProperties?: unknown;
}

export interface Connection {
  id: string;
  name: string;
  type: string;
  category: string;
  description: string;
  service_account: string | null;
  config: Record<string, unknown>;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface Task<R = Record<string, unknown>> {
  task_id: number;
  kind: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  result: R | null;
  error: string | null;
}

export interface SourceObject {
  name: string;
  kind: string;
  columns: { name: string; type: string }[];
  size: number | null;
  modified: string | null;
}

export interface Preview {
  columns: { name: string; type: string }[];
  rows: Record<string, unknown>[];
  files?: string[];
}

export interface JobSpec {
  source: {
    object?: string | null;
    query?: string | null;
    path_template?: string | null;
    format?: string | null;
    format_options?: Record<string, unknown>;
    options?: Record<string, unknown>;
  };
  load_mode: 'full' | 'incremental' | 'append';
  watermark_column?: string | null;
  target: { layer: 'bronze'; dataset: string };
  schedule: { type: 'none' | 'cron' | 'interval'; cron?: string | null; interval_seconds?: number | null };
  raw_vault?: unknown;
  promote_to_silver?: boolean;
}

export interface IngestionRun {
  id: string;
  job_id: string;
  job_name: string | null;
  job_version: number;
  batch_id: string;
  trigger: string;
  status: 'running' | 'succeeded' | 'failed';
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  rows_read: number;
  rows_written: number;
  bytes_read: number;
  files: number;
  table_version: number | null;
  dataset_id: string | null;
  details: {
    files?: { path: string; rows: number; size: number; sha256: string }[];
    schema_changes?: { change: string; column: string; type?: string; from?: string; to?: string }[];
  };
  error: string | null;
}

export interface IngestionJob {
  id: string;
  name: string;
  description: string;
  connection_id: string;
  connection_name: string | null;
  connection_type: string | null;
  spec: JobSpec;
  version: number;
  enabled: boolean;
  target: string;
  watermark: string | null;
  last_run: IngestionRun | null;
  next_run_at: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface Dataset {
  id: string;
  layer: string;
  name: string;
  uri: string;
  format: string;
  description: string;
  owner: string | null;
  row_count: number | null;
  size_bytes: number | null;
  table_version: number | null;
  last_loaded_at: string | null;
  source_job_id: string | null;
  columns: number;
}

export interface ColumnProfile {
  name: string;
  type: string;
  nulls: number;
  null_pct: number;
  distinct: number;
  distinct_pct: number;
  min?: unknown;
  max?: unknown;
  mean?: number | null;
  stddev?: number | null;
  min_length?: number | null;
  max_length?: number | null;
  patterns?: Record<string, number>;
  top_values?: { value: string; count: number }[];
}

export interface DatasetDetail extends Dataset {
  column_list: {
    name: string;
    ordinal: number;
    data_type: string;
    nullable: boolean;
    description: string;
    is_audit: boolean;
    removed_at: string | null;
  }[];
  schema_changes: { ts: string; run_id: string | null; changes: { change: string; column: string; type?: string; from?: string; to?: string }[] }[];
  profile: { ts: string; row_count: number; columns: ColumnProfile[] } | null;
  source_job: string | null;
}

export interface LineageNode {
  id: string;
  type: 'dataset' | 'job' | 'app';
  label: string;
  layer: string;
  namespace?: string;
  dataset_id?: string;
  row_count?: number | null;
  last_loaded_at?: string | null;
  last_run?: string;
  sql?: string | null;
  url?: string;
  file?: { size: number; sha256: string; rows: number };
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: { source: string; target: string }[];
}

export interface OpsSummary {
  since: string;
  totals: {
    runs: number;
    succeeded: number;
    failed: number;
    running: number;
    rows_written: number;
    bytes_read: number;
    files: number;
    p50_duration_ms: number | null;
    max_duration_ms: number | null;
  };
  series: { t: string; succeeded: number; failed: number; running: number; rows: number }[];
  jobs: { job: string; job_id: string; last_status: string; last_run: string; rows_written: number; duration_ms: number | null }[];
  recent_failures: { run_id: string; job: string; started_at: string; error: string }[];
  datasets: number;
}

export interface PortalApp {
  id: string;
  name: string;
  url: string;
  description: string;
  category: 'report' | 'dashboard' | 'app' | 'notebook';
  owner: string | null;
  datasets: string[];
  created_by: string | null;
  created_at: string;
}
