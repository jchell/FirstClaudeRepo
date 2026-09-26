import '@xyflow/react/dist/style.css';

import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Anchor,
  Badge,
  Code,
  Group,
  Loader,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { IconHierarchy3 } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import { Link, useSearchParams } from 'react-router-dom';

import { api } from '../../api/client';
import { fmtNumber, fmtTime } from '../../api/tasks';
import type { LineageGraph, LineageNode } from '../../api/types';

const elk = new ELK();
const NODE_W = 210;
const NODE_H = 54;

// Layer identity. Each node also carries its layer as text, so color is never the only cue.
const LAYER: Record<string, { color: string; label: string }> = {
  source: { color: '#2a78d6', label: 'Source' },
  job: { color: '#7a7a74', label: 'Job' },
  bronze: { color: '#c46f2c', label: 'Bronze' },
  silver: { color: '#8c8c8c', label: 'Silver' },
  gold: { color: '#b8900b', label: 'Gold' },
  report: { color: '#8a4fd1', label: 'Report' },
};

function LineageNodeView({ data, selected }: NodeProps<Node<{ n: LineageNode }>>) {
  const n = data.n;
  const style = LAYER[n.layer] ?? LAYER.source;
  return (
    <div
      style={{
        width: NODE_W,
        minHeight: NODE_H,
        borderRadius: 8,
        border: `2px solid ${selected ? 'var(--mantine-color-blue-6)' : 'var(--mantine-color-default-border)'}`,
        borderLeft: `6px solid ${style.color}`,
        background: 'var(--mantine-color-body)',
        padding: '6px 8px',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Text size="10px" c="dimmed" tt="uppercase" fw={600}>
        {style.label}
        {n.type === 'dataset' && n.layer === 'source' && n.namespace ? ` · ${n.namespace.split('://')[0]}` : ''}
      </Text>
      <Text size="xs" fw={600} lineClamp={2} style={{ wordBreak: 'break-all' }}>
        {n.label}
      </Text>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { lineage: LineageNodeView };

async function layout(g: LineageGraph): Promise<{ nodes: Node<{ n: LineageNode }>[]; edges: Edge[] }> {
  const res = await elk.layout({
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.layered.spacing.nodeNodeBetweenLayers': '70',
      'elk.spacing.nodeNode': '24',
    },
    children: g.nodes.map((n) => ({ id: n.id, width: NODE_W, height: NODE_H + 8 })),
    edges: g.edges.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
  });
  const pos = new Map(res.children?.map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]));
  return {
    nodes: g.nodes.map((n) => ({ id: n.id, type: 'lineage', position: pos.get(n.id) ?? { x: 0, y: 0 }, data: { n } })),
    edges: g.edges.map((e, i) => ({ id: `e${i}`, source: e.source, target: e.target, animated: false })),
  };
}

export function LineageView({ node, height = 600 }: { node?: string | null; height?: number }) {
  const [direction, setDirection] = useState<'both' | 'upstream' | 'downstream'>('both');
  const [selected, setSelected] = useState<LineageNode | null>(null);
  const [flow, setFlow] = useState<{ nodes: Node<{ n: LineageNode }>[]; edges: Edge[] } | null>(null);
  const graph = useQuery({
    queryKey: ['lineage', node, direction],
    queryFn: () =>
      api<LineageGraph>(`/api/lineage/graph?${new URLSearchParams(node ? { node, direction } : {})}`),
    retry: false,
  });

  useEffect(() => {
    if (graph.data) layout(graph.data).then(setFlow);
  }, [graph.data]);

  if (graph.isLoading || (graph.data && !flow)) return <Loader />;
  if (graph.isError) return <Alert color="gray">No lineage recorded for this yet.</Alert>;
  if (!graph.data?.nodes.length) return <Text c="dimmed">No lineage yet: it appears after the first successful run.</Text>;

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        {node ? (
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
        ) : (
          <span />
        )}
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
            nodes={(flow?.nodes ?? []).map((n) => ({ ...n, selected: n.id === (selected?.id ?? node) }))}
            edges={flow?.edges ?? []}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.2}
            nodesDraggable={false}
            onNodeClick={(_, n) => setSelected((n.data as { n: LineageNode }).n)}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={24} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </Paper>
        {selected && <NodeDetails n={selected} />}
      </Group>
    </Stack>
  );
}

function NodeDetails({ n }: { n: LineageNode }) {
  return (
    <Paper withBorder p="sm" w={300}>
      <Stack gap={6}>
        <Badge variant="light">{LAYER[n.layer]?.label ?? n.layer}</Badge>
        <Text fw={600} style={{ wordBreak: 'break-all' }}>
          {n.label}
        </Text>
        {n.namespace && <Code>{n.namespace}</Code>}
        {n.row_count != null && <Text size="sm">{fmtNumber(n.row_count)} rows</Text>}
        {n.last_loaded_at && <Text size="sm">Loaded {fmtTime(n.last_loaded_at)}</Text>}
        {n.last_run && <Text size="sm">Last run {fmtTime(n.last_run)}</Text>}
        {n.file && (
          <Text size="xs" ff="monospace">
            {n.file.rows} rows · sha256 {n.file.sha256.slice(0, 12)}…
          </Text>
        )}
        {n.sql && (
          <>
            <Text size="xs" c="dimmed">Query</Text>
            <Code block style={{ whiteSpace: 'pre-wrap' }}>{n.sql}</Code>
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
    </Paper>
  );
}

export function LineagePage() {
  const [params, setParams] = useSearchParams();
  const all = useQuery({ queryKey: ['lineage', 'all'], queryFn: () => api<LineageGraph>('/api/lineage/graph') });
  const node = params.get('node');
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
        <Badge variant="light">table level</Badge>
      </Group>
      <Select
        placeholder="Focus on a dataset, file or report (empty: whole graph)"
        searchable
        clearable
        data={options}
        value={node}
        onChange={(v) => setParams(v ? { node: v } : {})}
        maw={560}
      />
      <LineageView node={node} height={640} />
      <Text size="xs" c="dimmed">
        Built from the OpenLineage events every run emits. Column-level lineage and full impact analysis arrive in Phase 2.
      </Text>
    </Stack>
  );
}
