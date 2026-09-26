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
  load_mode: 'full' | 'incremental' | 'append' | 'cdc' | 'stream';
  watermark_column?: string | null;
  target: { layer: 'bronze'; dataset: string };
  schedule: {
    type: 'none' | 'cron' | 'interval' | 'file_arrival';
    cron?: string | null;
    interval_seconds?: number | null;
    poll_seconds?: number | null;
  };
  stream?: { write_mode: 'changelog' | 'mirror'; snapshot?: 'initial' | 'never'; max_records?: number; max_seconds?: number };
  raw_vault?: RawVaultSpec | null;
  promote_to_silver?: boolean;
}

export interface RawVaultSpec {
  hub: { name: string; keys: Record<string, string> };
  attributes: string[];
  satellite?: string | null;
  track_deletes?: boolean;
  links: { name: string; hub: { name: string; keys: Record<string, string> } }[];
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
  freshness_sla_minutes: number | null;
  domain: string | null;
  columns: number;
  /** Access is limited to granted roles/users. */
  restricted: boolean;
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
  tags: { id: number; tag: string; column: string | null; status: 'active' | 'suggested'; source: string; confidence: number | null; reason: string }[];
  glossary: { term_id: string; name: string; column: string | null; status: string }[];
  /** Columns masked for the current user, and how. */
  masked_columns: Record<string, string>;
  row_filtered: boolean;
  dq: { score: number | null; dimensions: Record<string, number>; rules: number; failing: number; unevaluated: number } | null;
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
  /** Live overlay: last run status (jobs), or late/alert (datasets). */
  status?: string;
  last_error?: string | null;
  alert?: string;
  lag?: number | null;
  latency_p95_ms?: number | null;
  tags?: string[];
  dq_score?: number | null;
  restricted?: boolean;
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

// ---------------------------------------------------------------- Phase 1b

export interface StreamInfo {
  job_id: string;
  name: string;
  kind: 'cdc' | 'stream';
  connection: string | null;
  connection_type: string | null;
  source: string | null;
  target: string;
  write_mode: 'changelog' | 'mirror';
  desired: 'running' | 'paused';
  status: 'starting' | 'running' | 'paused' | 'failed' | 'stalled';
  topics: string[];
  connector: { state: string; tasks?: { id: number; state: string; trace: string[] }[] } | null;
  lag: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  records_per_minute: number;
  totals: { records?: number; batches?: number; dlq?: number };
  last_batch_at: string | null;
  heartbeat_at: string | null;
  last_error: string | null;
  key_columns: string[] | null;
}

export interface StreamMetrics {
  stream: StreamInfo;
  lag: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  series: { minute: string; records: number; batches: number; dlq: number; latency_p50_ms: number | null; latency_p95_ms: number | null; lag: number | null }[];
}

export interface AlertInfo {
  id: number;
  kind: string;
  severity: 'warning' | 'serious' | 'critical';
  target: string;
  message: string;
  details: Record<string, unknown>;
  opened_at: string;
  resolved_at: string | null;
}

// ---------------------------------------------------------------- Phase 2: lineage

export interface ColumnNode {
  id: string;
  type: 'column';
  label: string;
  dataset: string;
  namespace: string;
  dataset_node: string;
  layer: string;
}

export interface ColumnEdge {
  source: string;
  target: string;
  job: string;
}

export interface DatasetColumns {
  columns: ColumnNode[];
  upstream: ColumnEdge[];
  downstream: ColumnEdge[];
}

export interface ColumnTrace {
  nodes: ColumnNode[];
  edges: ColumnEdge[];
  steps: { from: string; to: string; job: string; sql: string | null }[];
}

export interface ImpactResult {
  node: string;
  affected: { id: string; type: string; name: string; layer: string | null; status?: string | null }[];
  columns: [string, string][];
}

export interface BatchTrace {
  batch_id: string;
  origin: {
    job: string | null;
    run_id: string;
    status: string;
    started_at: string;
    files: { path: string; rows: number; size: number; sha256: string }[];
    rows_written: number;
    target: string | null;
  } | null;
  datasets: { dataset: string; layer: string; rows: number; dataset_id: string }[];
  apps: string[];
}

// ---------------------------------------------------------------- Phase 2: vault & transform

/** Free-form JSON documents (vault definitions, model configs, run details). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = Record<string, any>;

export type VaultKind = 'hub' | 'link' | 'sat' | 'pit' | 'bridge';

export interface VaultObject {
  id: string;
  kind: VaultKind;
  name: string;
  definition: Json;
  description: string;
  hash_key: string | null;
  required_keys: string[];
  columns: string[];
  rows: number | null;
  last_loaded_at: string | null;
  dataset_id: string | null;
  mappings: number;
  created_by: string | null;
  created_at: string;
}

export interface VaultMapping {
  id: string;
  source: string;
  target: string;
  target_kind: VaultKind;
  keys: Record<string, string>;
  attributes: Record<string, string>;
  record_source: string | null;
  ingestion_job_id: string | null;
  enabled: boolean;
  high_water: string | null;
  last_loaded_at: string | null;
  last_rows: number | null;
}

export interface VaultRun {
  id: string;
  kind: string;
  source: string;
  status: string;
  started_at: string;
  duration_ms: number | null;
  rows: number;
  error: string | null;
}

export interface VaultObjectDetail extends Omit<VaultObject, 'mappings'> {
  mappings: VaultMapping[];
  used_by: string[];
  runs: VaultRun[];
}

export interface VaultDiagram {
  nodes: { id: string; kind: VaultKind | 'source'; label: string }[];
  edges: { source: string; target: string; kind: string }[];
}

export type ModelKind = 'sql' | 'scd2_dimension' | 'fact' | 'date_dimension';

export interface TransformRun {
  id: string;
  kind: string;
  target: string;
  trigger: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  rows_written: number;
  table_version: number | null;
  pipeline_id: string | null;
  parent_run_id: string | null;
  details: Json;
  error: string | null;
}

export interface TransformModel {
  id: string;
  name: string;
  layer: 'silver' | 'gold';
  kind: ModelKind;
  sql: string;
  config: Json;
  description: string;
  version: number;
  enabled: boolean;
  owner: string | null;
  depends_on: string[];
  /** Every dataset the model reads ("<layer>.<name>"). */
  inputs: string[];
  used_by: string[];
  dataset_id: string | null;
  rows: number | null;
  last_run: TransformRun | null;
  updated_at: string;
}

export interface PipelineInfo {
  id: string;
  name: string;
  description: string;
  models: string[];
  order: string[];
  edges: { source: string; target: string }[];
  layers: Record<string, string>;
  kinds: Record<string, ModelKind>;
  schedule: { type: 'none' | 'cron' | 'interval'; cron?: string; interval_seconds?: number };
  trigger_datasets: string[];
  enabled: boolean;
  next_run_at: string | null;
  last_run: TransformRun | null;
  created_by: string | null;
  updated_at: string;
}

export interface ModelPreview {
  ok: boolean;
  error?: string;
  sql?: string;
  columns?: { name: string; type: string }[];
  rows?: Record<string, unknown>[];
  lineage?: Record<string, string[]>;
  dependencies?: string[];
}

// ---------------------------------------------------------------- Phase 3: quality & governance

export type RuleType =
  | 'not_null'
  | 'unique'
  | 'range'
  | 'regex'
  | 'allowed_values'
  | 'referential'
  | 'freshness'
  | 'row_count'
  | 'custom_sql';

export interface DqResultInfo {
  ts: string;
  run_id: string;
  trigger: string;
  rows_checked: number;
  rows_failed: number;
  score: number | null;
  passed: boolean | null;
  error: string | null;
  sample: unknown[];
}

export interface DqRuleInfo {
  id: string;
  name: string;
  dataset_id: string;
  dataset: string;
  column: string | null;
  rule_type: RuleType;
  params: Json;
  dimension: string;
  severity: 'warning' | 'critical';
  threshold: number;
  run_on_load: boolean;
  enabled: boolean;
  description: string;
  owner: string | null;
  last: DqResultInfo | null;
}

export interface Scorecard {
  id: string;
  name: string;
  description: string;
  rule_ids: string[];
  rules: DqRuleInfo[];
  schedule: { type: 'none' | 'cron' | 'interval'; cron?: string; interval_seconds?: number };
  next_run_at: string | null;
  degradation_pct: number;
  baseline_runs: number;
  owner: string | null;
  score: number | null;
  baseline: number | null;
  degraded: boolean;
  dimensions: Record<string, number>;
  last_run_at: string | null;
  history?: { ts: string; score: number | null; baseline: number | null; degraded: boolean; dimensions: Record<string, number> }[];
}

export interface DqSummary {
  rules: number;
  passing: number;
  failing: number;
  unevaluated: number;
  alerts: { id: number; kind: string; severity: string; target: string; message: string; opened_at: string }[];
}

export interface TagInfo {
  name: string;
  category: 'general' | 'classification';
  description: string;
  uses: number;
  masking_policy: string | null;
  builtin: boolean;
}

export interface TagAssignmentInfo {
  id: number;
  tag: string;
  dataset_id: string;
  dataset: string | null;
  column: string | null;
  status: 'active' | 'suggested' | 'rejected';
  source: string;
  confidence: number | null;
  reason: string;
  assigned_by: string | null;
  updated_at: string;
}

export interface GlossaryTermInfo {
  id: string;
  name: string;
  definition: string;
  synonyms: string[];
  domain: string | null;
  owner: string | null;
  status: 'draft' | 'approved' | 'deprecated';
  links: { dataset_id: string; dataset: string; column: string | null }[];
  updated_at: string;
}

export interface AccessGrantInfo {
  id: number;
  target_type: 'dataset' | 'domain';
  target: string;
  target_name: string;
  principal_type: 'role' | 'user';
  principal: string;
  created_by: string | null;
}

export interface MaskingPolicyInfo {
  id: string;
  name: string;
  tag: string;
  method: 'redact' | 'partial' | 'hash' | 'null';
  exempt_roles: string[];
  description: string;
  enabled: boolean;
}

export interface RowFilterInfo {
  id: string;
  name: string;
  dataset_id: string;
  dataset: string | null;
  predicate: string;
  exempt_roles: string[];
  description: string;
  enabled: boolean;
}

export interface NotificationChannelInfo {
  id: string;
  name: string;
  type: 'webhook' | 'email';
  recipients: string[];
  kinds: string[];
  min_severity: 'warning' | 'serious' | 'critical';
  enabled: boolean;
  secret_fields: string[];
  last_sent_at: string | null;
  last_error: string | null;
}

export interface QueryResult {
  ok: boolean;
  error?: string;
  columns?: { name: string; type: string }[];
  rows?: Record<string, unknown>[];
  truncated?: boolean;
  datasets?: string[];
}
