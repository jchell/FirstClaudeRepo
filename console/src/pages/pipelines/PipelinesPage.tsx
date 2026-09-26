import '@xyflow/react/dist/style.css';

import { useEffect, useMemo, useState } from 'react';
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Drawer,
  Group,
  Loader,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  ScrollArea,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Tabs,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import {
  IconCircleCheck,
  IconCircleX,
  IconClock,
  IconEye,
  IconLoader2,
  IconPlayerPlay,
  IconPlus,
  IconRoute,
  IconTrash,
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtDuration, fmtNumber, fmtTime, runTask } from '../../api/tasks';
import type { Dataset, Json, ModelKind, ModelPreview, PipelineInfo, Task, TransformModel, TransformRun } from '../../api/types';
import { canEngineer } from '../connections/ConnectionsPage';

const KINDS: Record<ModelKind, string> = {
  sql: 'SQL model',
  scd2_dimension: 'SCD2 dimension',
  fact: 'Fact',
  date_dimension: 'Date dimension',
};

const RUN_STATUS: Record<string, { color: string; icon: typeof IconCircleCheck }> = {
  succeeded: { color: 'green', icon: IconCircleCheck },
  failed: { color: 'red', icon: IconCircleX },
  running: { color: 'blue', icon: IconLoader2 },
  skipped: { color: 'gray', icon: IconClock },
};

export function RunStatus({ status }: { status: string }) {
  const st = RUN_STATUS[status] ?? RUN_STATUS.skipped;
  return (
    <Badge size="sm" variant="light" color={st.color} leftSection={<st.icon size={11} aria-hidden />}>
      {status}
    </Badge>
  );
}

function useDatasets() {
  return useQuery({ queryKey: ['catalog', 'all'], queryFn: () => api<Dataset[]>('/api/catalog/datasets') });
}

async function queue(path: string, ok: string) {
  try {
    const r = await api<{ queued: boolean; detail?: string }>(path, { method: 'POST' });
    notifications.show({ message: r.queued ? ok : (r.detail ?? 'already queued') });
  } catch (e) {
    notifications.show({ color: 'red', message: (e as Error).message });
  }
}

// ---------------------------------------------------------------- DAG

const elk = new ELK();
const LAYER_COLOR: Record<string, string> = { silver: '#8c8c8c', gold: '#b8900b' };

function ModelNode({ data }: NodeProps<Node<{ name: string; layer: string; kind: ModelKind; status?: string }>>) {
  return (
    <div
      style={{
        width: 180,
        borderRadius: 6,
        border: '1px solid var(--mantine-color-default-border)',
        borderLeft: `6px solid ${LAYER_COLOR[data.layer] ?? '#7a7a74'}`,
        background: 'var(--mantine-color-body)',
        padding: '4px 8px',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Group justify="space-between" gap={4} wrap="nowrap">
        <Text size="10px" c="dimmed" tt="uppercase" fw={600}>
          {data.layer} · {KINDS[data.kind]}
        </Text>
      </Group>
      <Text size="xs" fw={600} ff="monospace">
        {data.name}
      </Text>
      {data.status && <RunStatus status={data.status} />}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { model: ModelNode };

function Dag({ p }: { p: PipelineInfo }) {
  const [flow, setFlow] = useState<{ nodes: Node[]; edges: Edge[] } | null>(null);
  useEffect(() => {
    const results = (p.last_run?.details?.models ?? {}) as Record<string, { status: string }>;
    elk
      .layout({
        id: 'root',
        layoutOptions: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT' },
        children: p.order.map((m) => ({ id: m, width: 180, height: 64 })),
        edges: p.edges.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
      })
      .then((res) => {
        const pos = new Map(res.children?.map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]));
        setFlow({
          nodes: p.order.map((m) => ({
            id: m,
            type: 'model',
            position: pos.get(m) ?? { x: 0, y: 0 },
            data: { name: m, layer: p.layers[m], kind: p.kinds[m], status: results[m]?.status },
          })),
          edges: p.edges.map((e, i) => ({ id: `e${i}`, source: e.source, target: e.target, markerEnd: { type: MarkerType.ArrowClosed } })),
        });
      });
  }, [p]);
  if (!flow) return <Loader />;
  return (
    <Paper withBorder style={{ height: 260 }}>
      <ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} proOptions={{ hideAttribution: true }}>
        <Background gap={24} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </Paper>
  );
}

// ---------------------------------------------------------------- models tab

function ModelsTab({ engineer }: { engineer: boolean }) {
  const models = useQuery({ queryKey: ['models'], queryFn: () => api<TransformModel[]>('/api/transform/models'), refetchInterval: 10_000 });
  if (models.isLoading) return <Loader />;
  if (!models.data?.length)
    return (
      <Alert variant="light">
        No models yet. {engineer && <Anchor component={Link} to="/pipelines/models/new">Create one</Anchor>} to build silver and
        gold tables from bronze and the vault.
      </Alert>
    );
  return (
    <Table striped highlightOnHover withTableBorder>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Model</Table.Th>
          <Table.Th>Kind</Table.Th>
          <Table.Th>Reads</Table.Th>
          <Table.Th>Rows</Table.Th>
          <Table.Th>Last build</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {models.data.map((m) => (
          <Table.Tr key={m.id} data-testid={`model-${m.name}`}>
            <Table.Td>
              <Anchor component={Link} to={`/pipelines/models/${m.name}`} ff="monospace" fw={500}>
                {m.layer}.{m.name}
              </Anchor>
              {m.description && (
                <Text size="xs" c="dimmed">
                  {m.description}
                </Text>
              )}
            </Table.Td>
            <Table.Td>
              <Group gap={4}>
                <Text size="sm">{KINDS[m.kind]}</Text>
                {m.config.materialized === 'incremental' && <Badge size="xs" variant="outline">incremental</Badge>}
                {m.config.serve && <Badge size="xs" variant="outline">served</Badge>}
              </Group>
            </Table.Td>
            <Table.Td ff="monospace" fz="xs">
              {m.inputs.join(', ') || '—'}
            </Table.Td>
            <Table.Td>{fmtNumber(m.rows)}</Table.Td>
            <Table.Td>
              {m.last_run ? (
                <Stack gap={0}>
                  <RunStatus status={m.last_run.status} />
                  <Text size="xs" c="dimmed">
                    {fmtTime(m.last_run.started_at)}
                  </Text>
                </Stack>
              ) : (
                '—'
              )}
            </Table.Td>
            <Table.Td>
              {engineer && (
                <ActionIcon variant="subtle" aria-label={`Build ${m.name}`} onClick={() => queue(`/api/transform/models/${m.name}/run`, `${m.name}: build queued`)}>
                  <IconPlayerPlay size={16} />
                </ActionIcon>
              )}
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

// ---------------------------------------------------------------- model editor

const SQL_HELP = `-- Reference inputs with:
--   {{ source('bronze', 'orders') }}   any lake table (bronze, silver, gold, vault)
--   {{ vault('sat_customer_details') }} a vault table
--   {{ ref('stg_orders') }}             another model (adds a dependency)
-- Incremental models can use {% if is_incremental() %} ... {% endif %} and {{ this }}.
select *
from {{ source('bronze', 'orders') }}`;

export function ModelEditor() {
  const { name: routeName } = useParams();
  const isNew = !routeName || routeName === 'new';
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { user } = useAuth();
  const engineer = canEngineer(user?.roles);
  const existing = useQuery({
    queryKey: ['models', routeName],
    queryFn: () => api<TransformModel>(`/api/transform/models/${routeName}`),
    enabled: !isNew,
  });
  const allModels = useQuery({ queryKey: ['models'], queryFn: () => api<TransformModel[]>('/api/transform/models') });
  const datasets = useDatasets();
  const [name, setName] = useState('');
  const [layer, setLayer] = useState<'silver' | 'gold'>('silver');
  const [kind, setKind] = useState<ModelKind>('sql');
  const [sql, setSql] = useState(SQL_HELP);
  const [config, setConfig] = useState<Json>({ materialized: 'table' });
  const [description, setDescription] = useState('');
  const [preview, setPreview] = useState<ModelPreview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const m = existing.data;
    if (!m) return;
    setName(m.name);
    setLayer(m.layer);
    setKind(m.kind);
    setSql(m.sql);
    setConfig(m.config);
    setDescription(m.description);
  }, [existing.data]);

  const set = (patch: Json) => setConfig({ ...config, ...patch });
  const dsOptions = (datasets.data ?? []).map((d) => `${d.layer}.${d.name}`);
  const dims = (allModels.data ?? []).filter((m) => m.kind === 'scd2_dimension').map((m) => m.name);

  async function runPreview() {
    setBusy('preview');
    setError(null);
    setPreview(null);
    try {
      const t = await runTask<ModelPreview>(
        api<Task<ModelPreview>>('/api/transform/preview', {
          method: 'POST',
          body: json({ name: name || 'preview', layer, kind, sql: kind === 'sql' || kind === 'fact' ? sql : '', config }),
        }),
      );
      if (t.status === 'failed') setError(t.error ?? 'Preview failed');
      else if (t.result && !t.result.ok) setError(t.result.error ?? 'Preview failed');
      else setPreview(t.result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function save(andRun: boolean) {
    setBusy('save');
    setError(null);
    const body = { sql: kind === 'sql' || kind === 'fact' ? sql : '', config, description };
    try {
      const m = isNew
        ? await api<TransformModel>('/api/transform/models', { method: 'POST', body: json({ name, layer, kind, ...body }) })
        : await api<TransformModel>(`/api/transform/models/${routeName}`, { method: 'PUT', body: json(body) });
      qc.invalidateQueries({ queryKey: ['models'] });
      notifications.show({ message: `${m.name} saved (version ${m.version})` });
      if (andRun) await queue(`/api/transform/models/${m.name}/run`, `${m.name}: build queued`);
      navigate(`/pipelines/models/${m.name}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!isNew && existing.isLoading) return <Loader />;
  const m = existing.data;

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconRoute size={28} stroke={1.5} />
          <Title order={2}>{isNew ? 'New model' : `${m?.layer}.${m?.name}`}</Title>
          {m && <Badge variant="light">version {m.version}</Badge>}
        </Group>
        <Anchor component={Link} to="/pipelines">
          All models and pipelines
        </Anchor>
      </Group>
      <SimpleGrid cols={{ base: 1, md: 3 }}>
        <TextInput
          label="Name"
          disabled={!isNew}
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          error={name && !/^[a-z][a-z0-9_]{1,62}$/.test(name) ? 'Lower case letters, digits and _' : undefined}
        />
        <Select label="Layer" disabled={!isNew} data={['silver', 'gold']} value={layer} onChange={(v) => setLayer(v as 'silver' | 'gold')} allowDeselect={false} />
        <Select
          label="Kind"
          disabled={!isNew}
          data={Object.entries(KINDS).map(([value, label]) => ({ value, label }))}
          value={kind}
          onChange={(v) => {
            const k = v as ModelKind;
            setKind(k);
            setConfig(
              k === 'scd2_dimension'
                ? { surrogate_key: 'sk', change_ts: 'load_date' }
                : k === 'date_dimension'
                  ? { start: '2020-01-01', end: '2030-12-31' }
                  : { materialized: 'table', ...(k === 'fact' ? { dimensions: [] } : {}) },
            );
          }}
          allowDeselect={false}
        />
      </SimpleGrid>
      <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />

      {(kind === 'sql' || kind === 'fact') && (
        <>
          <Textarea
            label={kind === 'fact' ? 'Fact rows (SQL)' : 'SQL'}
            description="One SELECT, run in a locked-down DuckDB (no files or network). Column lineage is parsed from it."
            value={sql}
            onChange={(e) => setSql(e.currentTarget.value)}
            autosize
            minRows={10}
            styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 13 } }}
            spellCheck={false}
            aria-label="Model SQL"
          />
          <Group align="end">
            <SegmentedControl
              value={config.materialized ?? 'table'}
              onChange={(v) => set({ materialized: v, ...(v === 'table' ? { unique_key: [] } : {}) })}
              data={[
                { label: 'Table (rebuild)', value: 'table' },
                { label: 'Incremental', value: 'incremental' },
              ]}
            />
            {config.materialized === 'incremental' && (
              <TagsInput
                label="Unique key (merge on)"
                description="Empty: new rows are appended"
                value={config.unique_key ?? []}
                onChange={(v) => set({ unique_key: v })}
                w={320}
              />
            )}
          </Group>
        </>
      )}

      {kind === 'fact' && (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Text fw={600} size="sm">
              Dimension lookups
            </Text>
            <Text size="xs" c="dimmed">
              Each fact row gets the dimension’s surrogate key for the version valid at its event time (or the current one),
              and ‘-1’ when there is no match.
            </Text>
            {(config.dimensions ?? []).map((d: Json, i: number) => {
              const upd = (patch: Json) => {
                const next = [...config.dimensions];
                next[i] = { ...d, ...patch };
                set({ dimensions: next });
              };
              const [factCol, dimCol] = Object.entries(d.on ?? {})[0] ?? ['', ''];
              return (
                <Group key={i} align="end" wrap="nowrap">
                  <Select label="Dimension" data={dims} value={d.dimension ?? null} onChange={(v) => upd({ dimension: v })} w={200} />
                  <TextInput label="Fact column" value={factCol} onChange={(e) => upd({ on: { [e.currentTarget.value]: dimCol } })} />
                  <TextInput label="= dimension key" value={dimCol as string} onChange={(e) => upd({ on: { [factCol]: e.currentTarget.value } })} />
                  <TextInput label="As of (event time)" value={d.as_of ?? ''} onChange={(e) => upd({ as_of: e.currentTarget.value || undefined })} />
                  <TextInput label="Key column name" value={d.key_name ?? ''} onChange={(e) => upd({ key_name: e.currentTarget.value || undefined })} />
                  <ActionIcon color="red" variant="subtle" aria-label="Remove lookup" onClick={() => set({ dimensions: config.dimensions.filter((_: Json, j: number) => j !== i) })}>
                    <IconTrash size={16} />
                  </ActionIcon>
                </Group>
              );
            })}
            <Button size="xs" variant="light" w="fit-content" leftSection={<IconPlus size={14} />} onClick={() => set({ dimensions: [...(config.dimensions ?? []), { on: {} }] })}>
              Add dimension lookup
            </Button>
          </Stack>
        </Paper>
      )}

      {kind === 'scd2_dimension' && (
        <Paper withBorder p="sm">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Select
              label="History source"
              description="A vault satellite (business keys come from its hub) or any table with a change timestamp"
              data={dsOptions}
              value={config.source ?? null}
              onChange={(v) => set({ source: v })}
              searchable
            />
            <Select
              label="Deletes from (optional)"
              description="A status satellite: rows with is_deleted close the current version"
              data={dsOptions}
              value={config.deletes_source ?? null}
              onChange={(v) => set({ deletes_source: v ?? undefined })}
              searchable
              clearable
            />
            <TagsInput label="Business key" description="Empty: the hub's business keys" value={config.business_key ?? []} onChange={(v) => set({ business_key: v })} />
            <TagsInput label="Attributes" description="Empty: every descriptive column" value={config.attributes ?? []} onChange={(v) => set({ attributes: v })} />
            <TextInput label="Change timestamp column" value={config.change_ts ?? 'load_date'} onChange={(e) => set({ change_ts: e.currentTarget.value })} />
            <TextInput label="Surrogate key column" value={config.surrogate_key ?? 'sk'} onChange={(e) => set({ surrogate_key: e.currentTarget.value })} />
          </SimpleGrid>
        </Paper>
      )}

      {kind === 'date_dimension' && (
        <Group>
          <TextInput label="From" type="date" value={config.start ?? ''} onChange={(e) => set({ start: e.currentTarget.value })} />
          <TextInput label="To" type="date" value={config.end ?? ''} onChange={(e) => set({ end: e.currentTarget.value })} />
        </Group>
      )}

      <Switch
        label="Replicate to the serving database (Postgres) after each build"
        checked={!!config.serve}
        onChange={(e) => set({ serve: e.currentTarget.checked })}
      />
      {error && (
        <Alert color="red" title="Problem">
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {error}
          </Text>
        </Alert>
      )}
      {engineer && (
        <Group>
          <Button variant="default" leftSection={<IconEye size={16} />} loading={busy === 'preview'} onClick={runPreview}>
            Preview
          </Button>
          <Button variant="light" loading={busy === 'save'} disabled={isNew && !/^[a-z][a-z0-9_]{1,62}$/.test(name)} onClick={() => save(false)}>
            Save
          </Button>
          <Button loading={busy === 'save'} disabled={isNew && !/^[a-z][a-z0-9_]{1,62}$/.test(name)} onClick={() => save(true)}>
            Save and build
          </Button>
        </Group>
      )}
      {preview && <PreviewResult p={preview} />}
      {m && <ModelHistory m={m} />}
    </Stack>
  );
}

function PreviewResult({ p }: { p: ModelPreview }) {
  return (
    <Stack gap="xs">
      <Text fw={600}>Preview</Text>
      {!!p.dependencies?.length && (
        <Text size="sm">
          Depends on: <Code>{p.dependencies.join(', ')}</Code>
        </Text>
      )}
      <ScrollArea>
        <Table fz="xs" withTableBorder striped>
          <Table.Thead>
            <Table.Tr>
              {p.columns?.map((c) => (
                <Table.Th key={c.name}>
                  {c.name}
                  <Text size="10px" c="dimmed">
                    {c.type}
                  </Text>
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {p.rows?.map((r, i) => (
              <Table.Tr key={i}>
                {p.columns?.map((c) => (
                  <Table.Td key={c.name} ff="monospace">
                    {r[c.name] == null ? <Text c="dimmed" size="xs">null</Text> : String(r[c.name])}
                  </Table.Td>
                ))}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </ScrollArea>
      <Text fw={600} size="sm">
        Column lineage
      </Text>
      <Table fz="xs" withTableBorder maw={800}>
        <Table.Tbody>
          {Object.entries(p.lineage ?? {}).map(([col, srcs]) => (
            <Table.Tr key={col}>
              <Table.Td ff="monospace">{col}</Table.Td>
              <Table.Td ff="monospace">{srcs.length ? srcs.join(', ') : <Text c="dimmed" size="xs">computed (no source column)</Text>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

function ModelHistory({ m }: { m: TransformModel }) {
  const runs = useQuery({
    queryKey: ['runs', m.layer, m.name],
    queryFn: () => api<TransformRun[]>(`/api/transform/runs?${new URLSearchParams({ kind: 'model', target: `${m.layer}.${m.name}`, limit: '20' })}`),
    refetchInterval: 5000,
  });
  return (
    <Stack gap="xs">
      <Group>
        <Text fw={600}>Builds</Text>
        {m.dataset_id && (
          <Anchor component={Link} to={`/catalog/${m.dataset_id}`} size="sm">
            Open in catalog
          </Anchor>
        )}
        <Anchor component={Link} to={`/lineage?node=${encodeURIComponent(`dataset:s3://${m.layer}|${m.name}`)}`} size="sm">
          Lineage
        </Anchor>
        {!!m.used_by.length && <Text size="sm">Used by {m.used_by.join(', ')}</Text>}
      </Group>
      <RunsTable runs={runs.data ?? []} />
    </Stack>
  );
}

function RunsTable({ runs }: { runs: TransformRun[] }) {
  if (!runs.length) return <Text c="dimmed" size="sm">No runs yet.</Text>;
  return (
    <Table fz="xs" withTableBorder striped>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Started</Table.Th>
          <Table.Th>What</Table.Th>
          <Table.Th>Trigger</Table.Th>
          <Table.Th>Status</Table.Th>
          <Table.Th>Rows</Table.Th>
          <Table.Th>Duration</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {runs.map((r) => (
          <Table.Tr key={r.id}>
            <Table.Td>{fmtTime(r.started_at)}</Table.Td>
            <Table.Td ff="monospace">
              {r.kind.replace('_', ' ')} · {r.target}
              {r.details?.serving && (
                <Text size="xs" c="dimmed">
                  served ({r.details.serving.mode}, {r.details.serving.rows} rows)
                </Text>
              )}
            </Table.Td>
            <Table.Td>{r.trigger}</Table.Td>
            <Table.Td>
              <RunStatus status={r.status} />
              {r.error && (
                <Text size="xs" c={r.status === 'skipped' ? 'dimmed' : 'red'} maw={420}>
                  {r.error}
                </Text>
              )}
            </Table.Td>
            <Table.Td>{fmtNumber(r.rows_written)}</Table.Td>
            <Table.Td>{fmtDuration(r.duration_ms)}</Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

// ---------------------------------------------------------------- pipelines tab

function PipelineForm({ opened, onClose, edit }: { opened: boolean; onClose: () => void; edit: PipelineInfo | null }) {
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ['models'], queryFn: () => api<TransformModel[]>('/api/transform/models') });
  const datasets = useDatasets();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [triggers, setTriggers] = useState<string[]>([]);
  const [schedule, setSchedule] = useState<PipelineInfo['schedule']>({ type: 'none' });
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setName(edit?.name ?? '');
    setDescription(edit?.description ?? '');
    setSelected(edit?.models ?? []);
    setTriggers(edit?.trigger_datasets ?? []);
    setSchedule(edit?.schedule ?? { type: 'none' });
    setError(null);
  }, [edit, opened]);

  async function save() {
    setError(null);
    const body = { description, models: selected, trigger_datasets: triggers, schedule };
    try {
      if (edit) await api(`/api/transform/pipelines/${edit.id}`, { method: 'PUT', body: json(body) });
      else await api('/api/transform/pipelines', { method: 'POST', body: json({ name, ...body }) });
      qc.invalidateQueries({ queryKey: ['pipelines'] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} title={edit ? `Edit ${edit.name}` : 'New pipeline'} size="lg">
      <Stack>
        <TextInput label="Name" disabled={!!edit} value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        <MultiSelect
          label="Models"
          description="Built in dependency order; a failed model skips the models that read it"
          data={(models.data ?? []).map((m) => ({ value: m.name, label: `${m.layer}.${m.name}` }))}
          value={selected}
          onChange={setSelected}
          searchable
        />
        <MultiSelect
          label="Run when these datasets get new data"
          description="Event-driven: e.g. a vault satellite, so gold follows every load and streaming micro-batch"
          data={(datasets.data ?? []).map((d) => `${d.layer}.${d.name}`)}
          value={triggers}
          onChange={setTriggers}
          searchable
        />
        <SegmentedControl
          value={schedule.type}
          onChange={(v) => setSchedule(v === 'interval' ? { type: 'interval', interval_seconds: 3600 } : v === 'cron' ? { type: 'cron', cron: '0 3 * * *' } : { type: 'none' })}
          data={[
            { label: 'No schedule', value: 'none' },
            { label: 'Cron', value: 'cron' },
            { label: 'Every…', value: 'interval' },
          ]}
        />
        {schedule.type === 'cron' && (
          <TextInput label="Cron (UTC)" ff="monospace" value={schedule.cron ?? ''} onChange={(e) => setSchedule({ type: 'cron', cron: e.currentTarget.value })} />
        )}
        {schedule.type === 'interval' && (
          <NumberInput label="Every (seconds)" min={60} value={schedule.interval_seconds ?? 3600} onChange={(v) => setSchedule({ type: 'interval', interval_seconds: Number(v) })} />
        )}
        {error && <Alert color="red">{error}</Alert>}
        <Group justify="flex-end">
          <Button onClick={save} disabled={!name || !selected.length}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function PipelinesTab({ engineer }: { engineer: boolean }) {
  const pipelines = useQuery({ queryKey: ['pipelines'], queryFn: () => api<PipelineInfo[]>('/api/transform/pipelines'), refetchInterval: 10_000 });
  const [form, setForm] = useState<{ edit: PipelineInfo | null } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const current = pipelines.data?.find((p) => p.id === open) ?? null;
  const runs = useQuery({
    queryKey: ['runs', 'pipeline', open],
    queryFn: () => api<TransformRun[]>(`/api/transform/runs?${new URLSearchParams({ pipeline_id: open!, limit: '30' })}`),
    enabled: !!open,
    refetchInterval: 5000,
  });
  const qc = useQueryClient();
  if (pipelines.isLoading) return <Loader />;
  return (
    <Stack>
      {engineer && (
        <Button w="fit-content" leftSection={<IconPlus size={16} />} onClick={() => setForm({ edit: null })}>
          New pipeline
        </Button>
      )}
      {!pipelines.data?.length ? (
        <Alert variant="light">No pipelines yet.</Alert>
      ) : (
        <Table striped highlightOnHover withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Pipeline</Table.Th>
              <Table.Th>Models (in order)</Table.Th>
              <Table.Th>Runs when</Table.Th>
              <Table.Th>Last run</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {pipelines.data.map((p) => (
              <Table.Tr key={p.id} data-testid={`pipeline-${p.name}`}>
                <Table.Td>
                  <Anchor onClick={() => setOpen(p.id)} fw={500}>
                    {p.name}
                  </Anchor>
                  {p.description && (
                    <Text size="xs" c="dimmed">
                      {p.description}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td ff="monospace" fz="xs">
                  {p.order.join(' → ')}
                </Table.Td>
                <Table.Td fz="xs">
                  {[
                    ...(p.trigger_datasets.length ? [`${p.trigger_datasets.join(', ')} changes`] : []),
                    ...(p.schedule.type === 'cron' ? [`cron ${p.schedule.cron}`] : []),
                    ...(p.schedule.type === 'interval' ? [`every ${p.schedule.interval_seconds} s`] : []),
                  ].join(' · ') || 'manually'}
                </Table.Td>
                <Table.Td>
                  {p.last_run ? (
                    <Stack gap={0}>
                      <RunStatus status={p.last_run.status} />
                      <Text size="xs" c="dimmed">
                        {fmtTime(p.last_run.started_at)}
                      </Text>
                    </Stack>
                  ) : (
                    '—'
                  )}
                </Table.Td>
                <Table.Td>
                  {engineer && (
                    <ActionIcon variant="subtle" aria-label={`Run ${p.name}`} onClick={() => queue(`/api/transform/pipelines/${p.id}/run`, `${p.name}: run queued`)}>
                      <IconPlayerPlay size={16} />
                    </ActionIcon>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <PipelineForm opened={!!form} edit={form?.edit ?? null} onClose={() => setForm(null)} />
      <Drawer opened={!!current} onClose={() => setOpen(null)} position="right" size="xl" title="Pipeline">
        {current && (
          <Stack>
            <Group justify="space-between">
              <Title order={3}>{current.name}</Title>
              {engineer && (
                <Group gap="xs">
                  <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={() => queue(`/api/transform/pipelines/${current.id}/run`, 'Run queued')}>
                    Run now
                  </Button>
                  <Button size="xs" variant="default" onClick={() => setForm({ edit: current })}>
                    Edit
                  </Button>
                  <Button
                    size="xs"
                    color="red"
                    variant="subtle"
                    onClick={async () => {
                      if (!window.confirm(`Delete pipeline ${current.name}? Its models stay.`)) return;
                      await api(`/api/transform/pipelines/${current.id}`, { method: 'DELETE' });
                      qc.invalidateQueries({ queryKey: ['pipelines'] });
                      setOpen(null);
                    }}
                  >
                    Delete
                  </Button>
                </Group>
              )}
            </Group>
            {current.next_run_at && <Text size="sm">Next scheduled run {fmtTime(current.next_run_at)}</Text>}
            <Dag p={current} />
            <Text fw={600}>Runs</Text>
            <RunsTable runs={runs.data ?? []} />
          </Stack>
        )}
      </Drawer>
    </Stack>
  );
}

function RunsTab() {
  const [kind, setKind] = useState<string>('');
  const runs = useQuery({
    queryKey: ['runs', 'all', kind],
    queryFn: () => api<TransformRun[]>(`/api/transform/runs?${new URLSearchParams(kind ? { kind, limit: '100' } : { limit: '100' })}`),
    refetchInterval: 5000,
  });
  const kinds = useMemo(
    () => [
      { value: '', label: 'Everything' },
      { value: 'vault_load', label: 'Vault loads' },
      { value: 'vault_build', label: 'PIT / bridge builds' },
      { value: 'model', label: 'Model builds' },
      { value: 'pipeline', label: 'Pipelines' },
    ],
    [],
  );
  return (
    <Stack>
      <SegmentedControl value={kind} onChange={setKind} data={kinds} w="fit-content" />
      {runs.isLoading ? <Loader /> : <RunsTable runs={runs.data ?? []} />}
    </Stack>
  );
}

export function PipelinesPage() {
  const { user } = useAuth();
  const engineer = canEngineer(user?.roles);
  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconRoute size={28} stroke={1.5} />
          <Title order={2}>Pipelines</Title>
        </Group>
        {engineer && (
          <Button component={Link} to="/pipelines/models/new" leftSection={<IconPlus size={16} />}>
            New model
          </Button>
        )}
      </Group>
      <Text c="dimmed" maw={820}>
        Models turn bronze and vault data into silver and gold tables: SQL models (with <Code>ref()</Code> dependencies),
        SCD2 dimensions, facts and a date dimension. Pipelines build them in dependency order, on a schedule or whenever
        their inputs change, and can replicate results to the serving database.
      </Text>
      <Tabs defaultValue="models">
        <Tabs.List>
          <Tabs.Tab value="models">Models</Tabs.Tab>
          <Tabs.Tab value="pipelines">Pipelines</Tabs.Tab>
          <Tabs.Tab value="runs">Run history</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="models" pt="sm">
          <ModelsTab engineer={engineer} />
        </Tabs.Panel>
        <Tabs.Panel value="pipelines" pt="sm">
          <PipelinesTab engineer={engineer} />
        </Tabs.Panel>
        <Tabs.Panel value="runs" pt="sm">
          <RunsTab />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
