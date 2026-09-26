import '@xyflow/react/dist/style.css';

import { useEffect, useMemo, useState } from 'react';
import {
  Accordion,
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  MultiSelect,
  Paper,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconArrowBack,
  IconCircleCheck,
  IconCircleX,
  IconClock,
  IconDownload,
  IconHierarchy3,
  IconLoader2,
  IconRoute,
  IconSearch,
  IconTargetArrow,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import { Link, useSearchParams } from 'react-router-dom';

import { api } from '../../api/client';
import { fmtDuration, fmtNumber, fmtTime } from '../../api/tasks';
import type {
  BatchTrace,
  ColumnNode,
  ColumnTrace,
  DatasetColumns,
  ImpactResult,
  LineageGraph,
  LineageNode,
} from '../../api/types';

const elk = new ELK();
const NODE_W = 210;
const NODE_H = 54;

// Layer identity. Each node also carries its layer as text, so color is never the only cue.
export const LAYER: Record<string, { color: string; label: string }> = {
  source: { color: '#2a78d6', label: 'Source' },
  job: { color: '#7a7a74', label: 'Job' },
  bronze: { color: '#c46f2c', label: 'Bronze' },
  vault: { color: '#1f8a70', label: 'Vault' },
  silver: { color: '#8c8c8c', label: 'Silver' },
  gold: { color: '#b8900b', label: 'Gold' },
  serving: { color: '#3d5a80', label: 'Serving DB' },
  report: { color: '#8a4fd1', label: 'Report' },
};

// Live overlay states: always icon + label, never color alone.
const STATUS: Record<string, { color: string; icon: typeof IconCircleX; label: string }> = {
  failed: { color: 'red', icon: IconCircleX, label: 'failed' },
  late: { color: 'orange', icon: IconClock, label: 'late' },
  alert: { color: 'orange', icon: IconAlertTriangle, label: 'alert' },
  running: { color: 'blue', icon: IconLoader2, label: 'running' },
  stalled: { color: 'orange', icon: IconClock, label: 'stalled' },
  paused: { color: 'gray', icon: IconClock, label: 'paused' },
};

export function StatusMark({ status }: { status?: string }) {
  const st = status ? STATUS[status] : undefined;
  if (!st) return null;
  return (
    <Badge size="xs" color={st.color} variant="light" leftSection={<st.icon size={10} aria-hidden />}>
      {st.label}
    </Badge>
  );
}

type FlowData = { n: LineageNode; dim?: boolean };

function LineageNodeView({ data, selected }: NodeProps<Node<FlowData>>) {
  const n = data.n;
  const style = LAYER[n.layer] ?? LAYER.source;
  const bad = n.status === 'failed';
  return (
    <div
      style={{
        width: NODE_W,
        minHeight: NODE_H,
        borderRadius: 8,
        border: `2px ${bad ? 'dashed' : 'solid'} ${
          selected ? 'var(--mantine-color-blue-6)' : bad ? 'var(--mantine-color-red-6)' : 'var(--mantine-color-default-border)'
        }`,
        borderLeft: `6px solid ${style.color}`,
        background: 'var(--mantine-color-body)',
        padding: '6px 8px',
        opacity: data.dim ? 0.35 : 1,
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Group justify="space-between" gap={4} wrap="nowrap">
        <Text size="10px" c="dimmed" tt="uppercase" fw={600}>
          {style.label}
          {n.type === 'dataset' && n.layer === 'source' && n.namespace ? ` · ${n.namespace.split('://')[0]}` : ''}
        </Text>
        <StatusMark status={n.status} />
      </Group>
      <Text size="xs" fw={600} lineClamp={2} style={{ wordBreak: 'break-all' }}>
        {n.label}
      </Text>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { lineage: LineageNodeView };

async function layout<T extends { id: string }>(
  items: T[],
  links: { source: string; target: string }[],
  height = NODE_H + 8,
): Promise<Map<string, { x: number; y: number }>> {
  const res = await elk.layout({
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.layered.spacing.nodeNodeBetweenLayers': '70',
      'elk.spacing.nodeNode': '24',
    },
    children: items.map((n) => ({ id: n.id, width: NODE_W, height })),
    edges: links.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
  });
  return new Map(res.children?.map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]));
}

/** Drops nodes outside the chosen layers (jobs stay while they connect visible datasets). */
function filterLayers(g: LineageGraph, layers: string[]): LineageGraph {
  if (!layers.length) return g;
  const keep = new Set(g.nodes.filter((n) => n.type !== 'job' && layers.includes(n.layer)).map((n) => n.id));
  for (const n of g.nodes) {
    if (n.type === 'job' && g.edges.some((e) => (e.source === n.id && keep.has(e.target)) || (e.target === n.id && keep.has(e.source)))) {
      keep.add(n.id);
    }
  }
  return { nodes: g.nodes.filter((n) => keep.has(n.id)), edges: g.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) };
}

function csv(rows: (string | number | null | undefined)[][]): string {
  return rows.map((r) => r.map((v) => `"${String(v ?? '').replace(/"/g, '""')}"`).join(',')).join('\n');
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------- table-level graph

export function LineageView({ node, height = 600, asOf }: { node?: string | null; height?: number; asOf?: string | null }) {
  const [direction, setDirection] = useState<'both' | 'upstream' | 'downstream'>('both');
  const [layers, setLayers] = useState<string[]>([]);
  const [selected, setSelected] = useState<LineageNode | null>(null);
  const [trace, setTrace] = useState<{ column: ColumnNode; direction: 'upstream' | 'downstream' } | null>(null);
  const [impactOf, setImpactOf] = useState<{ id: string; label: string } | null>(null);
  const [flow, setFlow] = useState<{ nodes: Node<FlowData>[]; edges: Edge[] } | null>(null);
  const params = new URLSearchParams();
  if (node) {
    params.set('node', node);
    params.set('direction', direction);
  }
  if (asOf) params.set('as_of', new Date(asOf).toISOString());
  const graph = useQuery({
    queryKey: ['lineage', node, direction, asOf ?? null],
    queryFn: () => api<LineageGraph>(`/api/lineage/graph?${params}`),
    retry: false,
    // Live overlay: failed/late nodes and stream lag refresh while you look.
    refetchInterval: asOf ? false : 10_000,
  });
  const shown = useMemo(() => (graph.data ? filterLayers(graph.data, layers) : null), [graph.data, layers]);

  useEffect(() => {
    if (!shown) return;
    layout(shown.nodes, shown.edges).then((pos) =>
      setFlow({
        nodes: shown.nodes.map((n) => ({ id: n.id, type: 'lineage', position: pos.get(n.id) ?? { x: 0, y: 0 }, data: { n } })),
        edges: shown.edges.map((e, i) => ({ id: `e${i}`, source: e.source, target: e.target })),
      }),
    );
  }, [shown]);

  // keep the details panel in sync with live refreshes
  const current = selected ? (graph.data?.nodes.find((n) => n.id === selected.id) ?? selected) : null;

  if (graph.isLoading || (graph.data && !flow)) return <Loader />;
  if (graph.isError) return <Alert color="gray">No lineage recorded for this yet.</Alert>;
  if (!graph.data?.nodes.length) return <Text c="dimmed">No lineage yet: it appears after the first successful run.</Text>;

  if (trace) {
    return (
      <ColumnTraceView
        column={trace.column}
        direction={trace.direction}
        asOf={asOf}
        height={height}
        onBack={() => setTrace(null)}
      />
    );
  }

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Group gap="sm">
          {node && (
            <SegmentedControl
              size="xs"
              value={direction}
              onChange={(v) => setDirection(v as typeof direction)}
              data={[
                { label: 'Upstream and downstream', value: 'both' },
                { label: 'Upstream', value: 'upstream' },
                { label: 'Downstream (impact)', value: 'downstream' },
              ]}
            />
          )}
          <MultiSelect
            size="xs"
            placeholder="All layers"
            aria-label="Filter layers"
            data={Object.entries(LAYER)
              .filter(([k]) => k !== 'job')
              .map(([value, l]) => ({ value, label: l.label }))}
            value={layers}
            onChange={setLayers}
            clearable
            w={280}
          />
        </Group>
        <Group gap="sm">
          {Object.values(LAYER).map((l) => (
            <Group key={l.label} gap={4}>
              <span style={{ width: 10, height: 10, borderRadius: 2, background: l.color, display: 'inline-block' }} />
              <Text size="xs">{l.label}</Text>
            </Group>
          ))}
        </Group>
      </Group>
      <Group align="stretch" wrap="nowrap">
        <Paper withBorder style={{ flex: 1, height }}>
          <ReactFlow
            // re-fit when the details panel opens or closes (the canvas changes width)
            key={current ? 'with-details' : 'full'}
            nodes={(flow?.nodes ?? []).map((n) => ({ ...n, selected: n.id === (current?.id ?? node) }))}
            edges={flow?.edges ?? []}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.2}
            nodesDraggable={false}
            onNodeClick={(_, n) => setSelected((n.data as FlowData).n)}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={24} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </Paper>
        {current && (
          <NodeDetails
            n={current}
            asOf={asOf}
            onTrace={(column, dir) => setTrace({ column, direction: dir })}
            onImpact={(id, label) => setImpactOf({ id, label })}
          />
        )}
      </Group>
      <ImpactModal target={impactOf} onClose={() => setImpactOf(null)} />
    </Stack>
  );
}

function NodeDetails({
  n,
  asOf,
  onTrace,
  onImpact,
}: {
  n: LineageNode;
  asOf?: string | null;
  onTrace: (c: ColumnNode, direction: 'upstream' | 'downstream') => void;
  onImpact: (id: string, label: string) => void;
}) {
  const cols = useQuery({
    queryKey: ['lineage-columns', n.id, asOf ?? null],
    queryFn: () =>
      api<DatasetColumns>(
        `/api/lineage/columns?${new URLSearchParams({ dataset: n.id, ...(asOf ? { as_of: new Date(asOf).toISOString() } : {}) })}`,
      ),
    enabled: n.type === 'dataset',
  });
  return (
    <Paper withBorder p="sm" w={340}>
      <ScrollArea.Autosize mah={620}>
        <Stack gap={6}>
          <Group gap={6}>
            <Badge variant="light">{LAYER[n.layer]?.label ?? n.layer}</Badge>
            <StatusMark status={n.status} />
          </Group>
          <Text fw={600} style={{ wordBreak: 'break-all' }}>
            {n.label}
          </Text>
          {n.namespace && <Code>{n.namespace}</Code>}
          {n.row_count != null && <Text size="sm">{fmtNumber(n.row_count)} rows</Text>}
          {n.last_loaded_at && <Text size="sm">Loaded {fmtTime(n.last_loaded_at)}</Text>}
          {n.last_run && <Text size="sm">Last run {fmtTime(n.last_run)}</Text>}
          {n.alert && (
            <Alert color="orange" p="xs" icon={<IconClock size={14} />}>
              {n.alert}
            </Alert>
          )}
          {n.last_error && (
            <Alert color="red" p="xs" icon={<IconCircleX size={14} />}>
              <Text size="xs">{n.last_error}</Text>
            </Alert>
          )}
          {n.lag != null && (
            <Text size="sm">
              Stream lag {fmtNumber(n.lag)} · p95 latency {fmtDuration(n.latency_p95_ms ?? null)}
            </Text>
          )}
          {n.file && (
            <Text size="xs" ff="monospace">
              {n.file.rows} rows · sha256 {n.file.sha256.slice(0, 12)}…
            </Text>
          )}
          {n.sql && (
            <>
              <Text size="xs" c="dimmed">
                Transformation
              </Text>
              <Code block style={{ whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>
                {n.sql}
              </Code>
            </>
          )}
          {n.type !== 'job' && (
            <Button size="xs" variant="light" leftSection={<IconTargetArrow size={14} />} onClick={() => onImpact(n.id, n.label)}>
              Impact analysis
            </Button>
          )}
          {n.type === 'dataset' && (
            <>
              <Text size="xs" c="dimmed" mt="xs">
                Columns
              </Text>
              {cols.isLoading && <Loader size="xs" />}
              {cols.data && !cols.data.columns.length && (
                <Text size="xs" c="dimmed">
                  No column-level lineage recorded.
                </Text>
              )}
              {cols.data?.columns.map((c) => (
                <Group key={c.id} justify="space-between" gap={4} wrap="nowrap">
                  <Text size="xs" ff="monospace" style={{ wordBreak: 'break-all' }}>
                    {c.label}
                  </Text>
                  <Group gap={2} wrap="nowrap">
                    <Tooltip label="How does it get its value? (trace upstream)">
                      <ActionIcon size="sm" variant="subtle" aria-label={`Trace ${c.label} upstream`} onClick={() => onTrace(c, 'upstream')}>
                        <IconRoute size={14} />
                      </ActionIcon>
                    </Tooltip>
                    <Tooltip label="Where does it flow? (trace downstream)">
                      <ActionIcon
                        size="sm"
                        variant="subtle"
                        aria-label={`Trace ${c.label} downstream`}
                        onClick={() => onTrace(c, 'downstream')}
                      >
                        <IconRoute size={14} style={{ transform: 'scaleX(-1)' }} />
                      </ActionIcon>
                    </Tooltip>
                    <Tooltip label="Impact if this column changes">
                      <ActionIcon size="sm" variant="subtle" aria-label={`Impact of ${c.label}`} onClick={() => onImpact(c.id, `${n.label}.${c.label}`)}>
                        <IconTargetArrow size={14} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                </Group>
              ))}
            </>
          )}
          {n.dataset_id && (
            <Anchor component={Link} to={`/catalog/${n.dataset_id}`} size="sm">
              Open in catalog
            </Anchor>
          )}
          {n.url && (
            <Anchor href={n.url} target="_blank" rel="noreferrer" size="sm">
              Open app
            </Anchor>
          )}
        </Stack>
      </ScrollArea.Autosize>
    </Paper>
  );
}

// ---------------------------------------------------------------- column trace

type ColFlow = { c: ColumnNode; focus: boolean };

function ColumnNodeView({ data }: NodeProps<Node<ColFlow>>) {
  const style = LAYER[data.c.layer] ?? LAYER.source;
  return (
    <div
      style={{
        width: NODE_W,
        borderRadius: 8,
        border: `2px solid ${data.focus ? 'var(--mantine-color-blue-6)' : 'var(--mantine-color-default-border)'}`,
        borderLeft: `6px solid ${style.color}`,
        background: 'var(--mantine-color-body)',
        padding: '4px 8px',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Text size="10px" c="dimmed" tt="uppercase" fw={600} lineClamp={1} style={{ wordBreak: 'break-all' }}>
        {style.label} · {data.c.dataset}
      </Text>
      <Text size="xs" fw={600} ff="monospace">
        {data.c.label}
      </Text>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const columnTypes = { column: ColumnNodeView };

function ColumnTraceView({
  column,
  direction,
  asOf,
  height,
  onBack,
}: {
  column: ColumnNode;
  direction: 'upstream' | 'downstream';
  asOf?: string | null;
  height: number;
  onBack: () => void;
}) {
  const q = new URLSearchParams({ column: column.id, direction });
  if (asOf) q.set('as_of', new Date(asOf).toISOString());
  const tr = useQuery({ queryKey: ['trace', column.id, direction, asOf ?? null], queryFn: () => api<ColumnTrace>(`/api/lineage/trace?${q}`) });
  const [flow, setFlow] = useState<{ nodes: Node<ColFlow>[]; edges: Edge[] } | null>(null);
  useEffect(() => {
    if (!tr.data) return;
    const d = tr.data;
    layout(d.nodes, d.edges, 46).then((pos) =>
      setFlow({
        nodes: d.nodes.map((c) => ({
          id: c.id,
          type: 'column',
          position: pos.get(c.id) ?? { x: 0, y: 0 },
          data: { c, focus: c.id === column.id },
        })),
        // Jobs are listed under the graph; labels on these short edges would cover the nodes.
        edges: d.edges.map((e, i) => ({ id: `c${i}`, source: e.source, target: e.target })),
      }),
    );
  }, [tr.data, column.id]);
  const label = (id: string) => {
    const c = tr.data?.nodes.find((n) => n.id === id);
    return c ? `${c.dataset}.${c.label}` : id;
  };
  const jobs = useMemo(() => {
    const m = new Map<string, { sql: string | null; steps: string[] }>();
    for (const s of tr.data?.steps ?? []) {
      const j = m.get(s.job) ?? { sql: s.sql, steps: [] };
      j.steps.push(`${label(s.from)} → ${label(s.to)}`);
      m.set(s.job, j);
    }
    return [...m.entries()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tr.data]);

  return (
    <Stack gap="xs">
      <Group>
        <Button size="xs" variant="default" leftSection={<IconArrowBack size={14} />} onClick={onBack}>
          Table-level graph
        </Button>
        <Text fw={600}>
          {direction === 'upstream' ? 'How ' : 'Where '}
          <Code>
            {column.dataset}.{column.label}
          </Code>
          {direction === 'upstream' ? ' gets its value' : ' flows'}
        </Text>
        <Badge variant="light">column level</Badge>
      </Group>
      {!flow ? (
        <Loader />
      ) : (
        <Paper withBorder style={{ height: Math.min(height, 520) }}>
          <ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={columnTypes} fitView minZoom={0.2} nodesDraggable={false} proOptions={{ hideAttribution: true }}>
            <Background gap={24} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </Paper>
      )}
      <Text size="sm" fw={600}>
        Transformations along the path
      </Text>
      <Accordion variant="contained" multiple>
        {jobs.map(([job, info]) => (
          <Accordion.Item key={job} value={job}>
            <Accordion.Control>
              <Text size="sm" ff="monospace">
                {job}
              </Text>
              <Text size="xs" c="dimmed">
                {info.steps.length} column mapping{info.steps.length === 1 ? '' : 's'}
              </Text>
            </Accordion.Control>
            <Accordion.Panel>
              <Stack gap={4}>
                {info.steps.map((s) => (
                  <Text key={s} size="xs" ff="monospace">
                    {s}
                  </Text>
                ))}
                {info.sql && (
                  <Code block style={{ whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>
                    {info.sql}
                  </Code>
                )}
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        ))}
      </Accordion>
    </Stack>
  );
}

// ---------------------------------------------------------------- impact analysis

function ImpactModal({ target, onClose }: { target: { id: string; label: string } | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ['impact', target?.id],
    queryFn: () => api<ImpactResult>(`/api/lineage/impact?${new URLSearchParams({ node: target!.id })}`),
    enabled: !!target,
  });
  const exportCsv = () => {
    if (!q.data || !target) return;
    download(
      `impact-${target.label.replace(/[^a-z0-9._-]+/gi, '_')}.csv`,
      csv([
        ['type', 'name', 'layer', 'status'],
        ...q.data.affected.map((a) => [a.type, a.name, a.layer, a.status]),
        ...q.data.columns.map(([ds, col]) => ['column', `${ds}.${col}`, '', '']),
      ]),
    );
  };
  return (
    <Modal opened={!!target} onClose={onClose} title={`Impact of changes to ${target?.label ?? ''}`} size="lg">
      {q.isLoading && <Loader />}
      {q.isError && <Alert color="red">{(q.error as Error).message}</Alert>}
      {q.data && (
        <Stack>
          <Group justify="space-between">
            <Text size="sm">
              {q.data.affected.length} affected pipelines, datasets and reports
              {q.data.columns.length ? `, ${q.data.columns.length} downstream columns` : ''}.
            </Text>
            <Button size="xs" variant="light" leftSection={<IconDownload size={14} />} onClick={exportCsv}>
              Export CSV
            </Button>
          </Group>
          <Table striped withTableBorder fz="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Type</Table.Th>
                <Table.Th>Name</Table.Th>
                <Table.Th>Layer</Table.Th>
                <Table.Th>Status</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data.affected.map((a) => (
                <Table.Tr key={a.id}>
                  <Table.Td>{a.type === 'app' ? 'report / app' : a.type}</Table.Td>
                  <Table.Td ff="monospace">{a.name}</Table.Td>
                  <Table.Td>{a.layer ? (LAYER[a.layer]?.label ?? a.layer) : '—'}</Table.Td>
                  <Table.Td>{a.status && STATUS[a.status] ? <StatusMark status={a.status} /> : (a.status ?? '—')}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {!!q.data.columns.length && (
            <Text size="xs" c="dimmed">
              Columns: {q.data.columns.map(([ds, c]) => `${ds}.${c}`).join(', ')}
            </Text>
          )}
        </Stack>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- batch trace

function BatchTraceView() {
  const [params, setParams] = useSearchParams();
  const [input, setInput] = useState(params.get('batch') ?? '');
  const batch = params.get('batch');
  const q = useQuery({
    queryKey: ['batch', batch],
    queryFn: () => api<BatchTrace>(`/api/lineage/batch/${batch}`),
    enabled: !!batch && /^[0-9a-f]{32}$/.test(batch),
  });
  return (
    <Stack>
      <Text size="sm" c="dimmed" maw={760}>
        Every row carries the <Code>_batch_id</Code> of the ingestion run that brought it in, through bronze, the vault and
        the gold models built from it. Paste a batch id from any row to see the run and files it came from and every table
        holding its rows.
      </Text>
      <Group>
        <TextInput
          placeholder="32-character batch id"
          value={input}
          onChange={(e) => setInput(e.currentTarget.value.trim())}
          w={380}
          ff="monospace"
          aria-label="Batch id"
        />
        <Button leftSection={<IconSearch size={14} />} onClick={() => setParams({ tab: 'batch', batch: input })} disabled={!/^[0-9a-f]{32}$/.test(input)}>
          Trace batch
        </Button>
      </Group>
      {q.isLoading && <Loader />}
      {q.data && (
        <Stack>
          {q.data.origin ? (
            <Paper withBorder p="sm">
              <Group gap={6}>
                <IconCircleCheck size={16} aria-hidden />
                <Text fw={600}>
                  Ingestion run of {q.data.origin.job} into {q.data.origin.target}
                </Text>
                <Badge variant="light">{q.data.origin.status}</Badge>
              </Group>
              <Text size="sm">
                {fmtTime(q.data.origin.started_at)} · {fmtNumber(q.data.origin.rows_written)} rows
              </Text>
              {q.data.origin.files.map((f) => (
                <Text key={f.path} size="xs" ff="monospace">
                  {f.path} · {f.rows} rows · sha256 {f.sha256?.slice(0, 12)}…
                </Text>
              ))}
            </Paper>
          ) : (
            <Alert color="gray">No ingestion run has this batch id.</Alert>
          )}
          <Table striped withTableBorder fz="sm" maw={700}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Dataset</Table.Th>
                <Table.Th>Layer</Table.Th>
                <Table.Th>Rows from this batch</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data.datasets.map((d) => (
                <Table.Tr key={d.dataset}>
                  <Table.Td>
                    <Anchor component={Link} to={`/catalog/${d.dataset_id}`} ff="monospace" size="sm">
                      {d.dataset}
                    </Anchor>
                  </Table.Td>
                  <Table.Td>{LAYER[d.layer]?.label ?? d.layer}</Table.Td>
                  <Table.Td>{fmtNumber(d.rows)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {!!q.data.apps.length && <Text size="sm">Reports reading these datasets: {q.data.apps.join(', ')}</Text>}
        </Stack>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------- page

export function LineagePage() {
  const [params, setParams] = useSearchParams();
  const [asOf, setAsOf] = useState<string>('');
  const all = useQuery({ queryKey: ['lineage', 'all'], queryFn: () => api<LineageGraph>('/api/lineage/graph') });
  const node = params.get('node');
  const tab = params.get('tab') ?? 'graph';
  const options = useMemo(
    () =>
      (all.data?.nodes ?? [])
        .filter((n) => n.type !== 'job')
        .map((n) => ({ value: n.id, label: `${LAYER[n.layer]?.label ?? n.layer}: ${n.label}` })),
    [all.data],
  );

  return (
    <Stack>
      <Group>
        <IconHierarchy3 size={28} stroke={1.5} />
        <Title order={2}>Lineage Explorer</Title>
      </Group>
      <Tabs value={tab} onChange={(v) => setParams(v === 'batch' ? { tab: 'batch' } : node ? { node } : {})}>
        <Tabs.List>
          <Tabs.Tab value="graph">Graph</Tabs.Tab>
          <Tabs.Tab value="batch">Trace a batch</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="graph" pt="sm">
          <Stack>
            <Group align="end">
              <Select
                placeholder="Focus on a dataset, file or report (empty: whole graph)"
                searchable
                clearable
                data={options}
                value={node}
                onChange={(v) => setParams(v ? { node: v } : {})}
                w={520}
                aria-label="Focus node"
              />
              <TextInput
                type="datetime-local"
                label="As of (time travel)"
                value={asOf}
                onChange={(e) => setAsOf(e.currentTarget.value)}
                w={230}
              />
              {asOf && (
                <Button variant="subtle" size="xs" onClick={() => setAsOf('')}>
                  Back to now (live)
                </Button>
              )}
            </Group>
            <LineageView node={node} height={640} asOf={asOf || null} />
            <Text size="xs" c="dimmed">
              Built from the OpenLineage events every ingestion, vault load and model run emits. Click a dataset to trace its
              columns back to the source or forward to reports; failed and late nodes update live.
            </Text>
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="batch" pt="sm">
          <BatchTraceView />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
