import '@xyflow/react/dist/style.css';

import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Code,
  Drawer,
  Group,
  Loader,
  Modal,
  MultiSelect,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  TagsInput,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconBuildingWarehouse, IconPlayerPlay, IconPlus, IconTrash } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import { Link } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtDuration, fmtNumber, fmtTime } from '../../api/tasks';
import type { Dataset, DatasetDetail, VaultDiagram, VaultKind, VaultObject, VaultObjectDetail } from '../../api/types';
import { canEngineer } from '../connections/ConnectionsPage';

// Kind identity: every node and badge also shows the kind as text.
const KIND: Record<string, { color: string; label: string; mantine: string }> = {
  hub: { color: '#2a78d6', label: 'Hub', mantine: 'blue' },
  link: { color: '#eb6834', label: 'Link', mantine: 'orange' },
  sat: { color: '#1f8a70', label: 'Satellite', mantine: 'teal' },
  pit: { color: '#8a4fd1', label: 'PIT', mantine: 'grape' },
  bridge: { color: '#b8900b', label: 'Bridge', mantine: 'yellow' },
  source: { color: '#7a7a74', label: 'Source', mantine: 'gray' },
};

export function KindBadge({ kind }: { kind: string }) {
  const k = KIND[kind] ?? KIND.source;
  return (
    <Badge variant="light" color={k.mantine}>
      {k.label}
    </Badge>
  );
}

// ---------------------------------------------------------------- diagram

const elk = new ELK();
const W = 190;
const H = 44;

function VaultNode({ data }: NodeProps<Node<{ id: string; kind: string; rows?: number | null }>>) {
  const k = KIND[data.kind] ?? KIND.source;
  return (
    <div
      style={{
        width: W,
        borderRadius: data.kind === 'hub' ? 22 : 6,
        border: '1px solid var(--mantine-color-default-border)',
        borderLeft: `6px solid ${k.color}`,
        background: 'var(--mantine-color-body)',
        padding: '4px 10px',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Text size="10px" c="dimmed" tt="uppercase" fw={600}>
        {k.label}
        {data.rows != null ? ` · ${fmtNumber(data.rows)} rows` : ''}
      </Text>
      <Text size="xs" fw={600} style={{ wordBreak: 'break-all' }}>
        {data.id}
      </Text>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { vault: VaultNode };

function Diagram({ objects, onSelect }: { objects: VaultObject[]; onSelect: (name: string) => void }) {
  const d = useQuery({ queryKey: ['vault-diagram'], queryFn: () => api<VaultDiagram>('/api/vault/diagram') });
  const [flow, setFlow] = useState<{ nodes: Node[]; edges: Edge[] } | null>(null);
  const rows = useMemo(() => new Map(objects.map((o) => [o.name, o.rows])), [objects]);
  useEffect(() => {
    if (!d.data) return;
    // Arrows point from data sources into the vault, and from dependents to what they describe.
    const edges = d.data.edges.map((e) =>
      e.kind === 'loads' ? e : { ...e, source: e.target, target: e.source },
    );
    elk
      .layout({
        id: 'root',
        layoutOptions: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.spacing.nodeNode': '20' },
        children: d.data.nodes.map((n) => ({ id: n.id, width: W, height: H + 6 })),
        edges: edges.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
      })
      .then((res) => {
        const pos = new Map(res.children?.map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]));
        setFlow({
          nodes: d.data!.nodes.map((n) => ({
            id: n.id,
            type: 'vault',
            position: pos.get(n.id) ?? { x: 0, y: 0 },
            data: { id: n.id, kind: n.kind, rows: rows.get(n.id) },
          })),
          edges: edges.map((e, i) => ({
            id: `e${i}`,
            source: e.source,
            target: e.target,
            label: e.kind === 'loads' ? undefined : e.kind,
            labelStyle: { fontSize: 9 },
            markerEnd: { type: MarkerType.ArrowClosed },
            style: e.kind === 'loads' ? { strokeDasharray: '4 3' } : undefined,
          })),
        });
      });
  }, [d.data, rows]);
  if (!flow) return <Loader />;
  if (!flow.nodes.length) return <Text c="dimmed">No vault objects yet.</Text>;
  return (
    <Paper withBorder style={{ height: 560 }}>
      <ReactFlow
        nodes={flow.nodes}
        edges={flow.edges}
        nodeTypes={nodeTypes}
        fitView
        nodesDraggable={false}
        minZoom={0.2}
        onNodeClick={(_, n) => (n.data as { kind: string }).kind !== 'source' && onSelect(n.id)}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={24} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </Paper>
  );
}

// ---------------------------------------------------------------- create object

function NewObject({ opened, onClose, objects }: { opened: boolean; onClose: () => void; objects: VaultObject[] }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<VaultKind>('hub');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [keys, setKeys] = useState<string[]>([]);
  const [hubs, setHubs] = useState<string[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [attributes, setAttributes] = useState<string[]>([]);
  const [mak, setMak] = useState<string[]>([]);
  const [status, setStatus] = useState(false);
  const [members, setMembers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = (k: string) => objects.filter((o) => o.kind === k).map((o) => o.name);
  const prefix = { hub: 'hub_', link: 'link_', sat: 'sat_', pit: 'pit_', bridge: 'bridge_' }[kind];

  const definition = (): Record<string, unknown> => {
    switch (kind) {
      case 'hub':
        return { business_keys: keys };
      case 'link':
        return { hubs: hubs.map((hub) => ({ hub })) };
      case 'sat':
        return { parent, attributes, multi_active_key: mak, status };
      case 'pit':
        return { hub: parent, satellites: members };
      case 'bridge':
        return { hub: parent, links: members };
    }
  };

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api('/api/vault/objects', { method: 'POST', body: json({ kind, name, description, definition: definition() }) });
      notifications.show({ message: `${name} created` });
      qc.invalidateQueries({ queryKey: ['vault'] });
      qc.invalidateQueries({ queryKey: ['vault-diagram'] });
      onClose();
      setName('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} title="New vault object" size="lg">
      <Stack>
        <Select
          label="Kind"
          value={kind}
          onChange={(v) => {
            setKind(v as VaultKind);
            setName(`${{ hub: 'hub_', link: 'link_', sat: 'sat_', pit: 'pit_', bridge: 'bridge_' }[v as VaultKind]}`);
            setParent(null);
            setMembers([]);
          }}
          data={[
            { value: 'hub', label: 'Hub: a business entity, identified by business keys' },
            { value: 'link', label: 'Link: a relationship between hubs' },
            { value: 'sat', label: 'Satellite: descriptive attributes of a hub or link, with history' },
            { value: 'pit', label: 'PIT table: point-in-time snapshots over a hub’s satellites' },
            { value: 'bridge', label: 'Bridge table: hub keys joined along a path of links' },
          ]}
          allowDeselect={false}
        />
        <TextInput
          label="Name"
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          description={`Starts with ${prefix}; lower case letters, digits and _`}
          error={name && !new RegExp(`^${prefix}[a-z0-9_]+$`).test(name) ? `Use ${prefix}<name>` : undefined}
        />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        {kind === 'hub' && (
          <TagsInput label="Business keys" description="Type a key name and press Enter" value={keys} onChange={setKeys} />
        )}
        {kind === 'link' && <MultiSelect label="Hubs" data={names('hub')} value={hubs} onChange={setHubs} />}
        {kind === 'sat' && (
          <>
            <Select label="Parent hub or link" data={[...names('hub'), ...names('link')]} value={parent} onChange={setParent} searchable />
            <Checkbox
              label="Status-tracking satellite (records deletes; no attributes)"
              checked={status}
              onChange={(e) => setStatus(e.currentTarget.checked)}
            />
            {!status && (
              <>
                <TagsInput label="Attributes" value={attributes} onChange={setAttributes} />
                <MultiSelect
                  label="Multi-active key (optional)"
                  description="Attributes that tell several current rows per key apart, e.g. phone_type"
                  data={attributes}
                  value={mak}
                  onChange={setMak}
                />
              </>
            )}
          </>
        )}
        {(kind === 'pit' || kind === 'bridge') && (
          <>
            <Select label="Hub" data={names('hub')} value={parent} onChange={setParent} searchable />
            <MultiSelect
              label={kind === 'pit' ? 'Satellites' : 'Links (in path order)'}
              data={
                kind === 'pit'
                  ? objects.filter((o) => o.kind === 'sat' && o.definition.parent === parent).map((o) => o.name)
                  : names('link')
              }
              value={members}
              onChange={setMembers}
            />
          </>
        )}
        {error && <Alert color="red">{error}</Alert>}
        <Group justify="flex-end">
          <Button loading={busy} onClick={save} disabled={!name}>
            Create
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ---------------------------------------------------------------- mappings

function AddMapping({ obj, onDone }: { obj: VaultObjectDetail; onDone: () => void }) {
  const [source, setSource] = useState<string | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [attrs, setAttrs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const datasets = useQuery({ queryKey: ['catalog', 'mappable'], queryFn: () => api<Dataset[]>('/api/catalog/datasets') });
  const ds = datasets.data?.find((d) => `${d.layer}.${d.name}` === source);
  const detail = useQuery({
    queryKey: ['catalog', ds?.id],
    queryFn: () => api<DatasetDetail>(`/api/catalog/datasets/${ds!.id}`),
    enabled: !!ds,
  });
  const cols = (detail.data?.column_list ?? []).filter((c) => !c.removed_at && !c.is_audit).map((c) => c.name);
  const attributes: string[] = obj.kind === 'sat' && !obj.definition.status ? obj.definition.attributes : [];

  async function save() {
    const [layer, name] = source!.split('.');
    // attributes named like a source column default to it
    const defaults = Object.fromEntries(attributes.filter((a) => cols.includes(a)).map((a) => [a, a]));
    setBusy(true);
    try {
      await api('/api/vault/mappings', {
        method: 'POST',
        body: json({ source_layer: layer, source_dataset: name, target: obj.name, keys, attributes: { ...defaults, ...attrs } }),
      });
      notifications.show({ message: `${source} now loads ${obj.name}` });
      onDone();
    } catch (e) {
      notifications.show({ color: 'red', message: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Paper withBorder p="sm">
      <Stack gap="xs">
        <Text fw={600} size="sm">
          Map a source
        </Text>
        <Select
          label="Source dataset"
          data={(datasets.data ?? []).filter((d) => d.layer === 'bronze' || d.layer === 'silver').map((d) => `${d.layer}.${d.name}`)}
          value={source}
          onChange={(v) => {
            setSource(v);
            setKeys({});
            setAttrs({});
          }}
          searchable
        />
        {source && (
          <SimpleGrid cols={2}>
            {obj.required_keys.map((k) => (
              <Select
                key={k}
                label={`Key ${k}`}
                data={cols}
                value={keys[k] ?? null}
                onChange={(v) => setKeys({ ...keys, [k]: v ?? '' })}
                searchable
              />
            ))}
            {attributes.map((a) => (
              <Select
                key={a}
                label={`Attribute ${a}`}
                data={cols}
                value={attrs[a] ?? (cols.includes(a) ? a : null)}
                onChange={(v) => setAttrs({ ...attrs, [a]: v ?? '' })}
                searchable
              />
            ))}
          </SimpleGrid>
        )}
        <Button
          size="xs"
          w="fit-content"
          loading={busy}
          disabled={!source}
          onClick={save}
        >
          Save mapping
        </Button>
      </Stack>
    </Paper>
  );
}

// ---------------------------------------------------------------- object details

function ObjectDrawer({ name, onClose, engineer }: { name: string | null; onClose: () => void; engineer: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ['vault', 'object', name],
    queryFn: () => api<VaultObjectDetail>(`/api/vault/objects/${name}`),
    enabled: !!name,
    refetchInterval: 5000,
  });
  const o = q.data;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['vault'] });
    qc.invalidateQueries({ queryKey: ['vault-diagram'] });
  };

  async function act(path: string, body: unknown, ok: string) {
    try {
      const r = await api<{ queued: boolean; detail?: string }>(path, { method: 'POST', body: json(body) });
      notifications.show({ message: r.queued ? ok : (r.detail ?? 'already queued') });
    } catch (e) {
      notifications.show({ color: 'red', message: (e as Error).message });
    }
  }

  return (
    <Drawer opened={!!name} onClose={onClose} position="right" size="xl" title="Vault object">
      {!o ? (
        <Loader />
      ) : (
        <Stack>
          <Group>
            <KindBadge kind={o.kind} />
            <Title order={3}>{o.name}</Title>
          </Group>
          {o.description && <Text>{o.description}</Text>}
          <SimpleGrid cols={3}>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Rows</Text>
              <Text fw={600}>{fmtNumber(o.rows)}</Text>
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Last loaded</Text>
              <Text size="sm">{fmtTime(o.last_loaded_at)}</Text>
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Used by</Text>
              <Text size="sm">{o.used_by.join(', ') || '—'}</Text>
            </Paper>
          </SimpleGrid>
          <div>
            <Text size="xs" c="dimmed">
              Columns
            </Text>
            <Group gap={4}>
              {o.columns.map((c) => (
                <Code key={c}>{c}</Code>
              ))}
            </Group>
          </div>
          <Code block>{JSON.stringify(o.definition, null, 2)}</Code>
          <Group>
            {o.dataset_id && (
              <Anchor component={Link} to={`/catalog/${o.dataset_id}`} size="sm">
                Open in catalog
              </Anchor>
            )}
            {o.dataset_id && (
              <Anchor component={Link} to={`/lineage?node=${encodeURIComponent(`dataset:s3://vault|${o.name}`)}`} size="sm">
                Lineage
              </Anchor>
            )}
            {engineer && (o.kind === 'pit' || o.kind === 'bridge') && (
              <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={() => act(`/api/vault/objects/${o.name}/build`, {}, 'Build queued')}>
                Build now
              </Button>
            )}
          </Group>

          {(o.kind === 'hub' || o.kind === 'link' || o.kind === 'sat') && (
            <>
              <Title order={5}>Source mappings</Title>
              {!o.mappings.length && <Text c="dimmed" size="sm">Not loaded from any source yet.</Text>}
              {!!o.mappings.length && (
                <Table fz="xs" withTableBorder>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Source</Table.Th>
                      <Table.Th>Columns</Table.Th>
                      <Table.Th>Loaded up to</Table.Th>
                      <Table.Th>Last rows</Table.Th>
                      <Table.Th />
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {o.mappings.map((m) => (
                      <Table.Tr key={m.id}>
                        <Table.Td ff="monospace">{m.source}</Table.Td>
                        <Table.Td ff="monospace">
                          {Object.entries({ ...m.keys, ...m.attributes })
                            .map(([k, v]) => (k === v ? k : `${k}←${v}`))
                            .join(', ')}
                        </Table.Td>
                        <Table.Td>{fmtTime(m.high_water)}</Table.Td>
                        <Table.Td>{fmtNumber(m.last_rows)}</Table.Td>
                        <Table.Td>
                          {engineer && (
                            <Group gap={2} wrap="nowrap">
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                onClick={() => {
                                  const [layer, dataset] = m.source.split('.');
                                  act('/api/vault/load', { source_layer: layer, source_dataset: dataset }, 'Load queued');
                                }}
                              >
                                Load now
                              </Button>
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                color="red"
                                aria-label="Remove mapping"
                                onClick={async () => {
                                  await api(`/api/vault/mappings/${m.id}`, { method: 'DELETE' });
                                  q.refetch();
                                  refresh();
                                }}
                              >
                                <IconTrash size={14} />
                              </Button>
                            </Group>
                          )}
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
              {engineer && (
                <AddMapping
                  obj={o}
                  onDone={() => {
                    q.refetch();
                    refresh();
                  }}
                />
              )}
            </>
          )}

          <Title order={5}>Recent loads</Title>
          {!o.runs.length ? (
            <Text c="dimmed" size="sm">No loads yet.</Text>
          ) : (
            <Table fz="xs" withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Started</Table.Th>
                  <Table.Th>From</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Rows added</Table.Th>
                  <Table.Th>Duration</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {o.runs.map((r) => (
                  <Table.Tr key={r.id}>
                    <Table.Td>{fmtTime(r.started_at)}</Table.Td>
                    <Table.Td ff="monospace">{r.source}</Table.Td>
                    <Table.Td>
                      <Badge size="xs" variant="light" color={r.status === 'failed' ? 'red' : r.status === 'succeeded' ? 'green' : 'gray'}>
                        {r.status}
                      </Badge>
                      {r.error && (
                        <Text size="xs" c="red">
                          {r.error}
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>{fmtNumber(r.rows)}</Table.Td>
                    <Table.Td>{fmtDuration(r.duration_ms)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          {engineer && !o.used_by.length && (
            <Button
              color="red"
              variant="subtle"
              w="fit-content"
              onClick={async () => {
                if (!window.confirm(`Delete ${o.name} and its mappings? The Delta table is kept.`)) return;
                try {
                  await api(`/api/vault/objects/${o.name}`, { method: 'DELETE' });
                  refresh();
                  onClose();
                } catch (e) {
                  notifications.show({ color: 'red', message: (e as Error).message });
                }
              }}
            >
              Delete object
            </Button>
          )}
        </Stack>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- page

export function DataVaultPage() {
  const { user } = useAuth();
  const engineer = canEngineer(user?.roles);
  const objects = useQuery({ queryKey: ['vault', 'objects'], queryFn: () => api<VaultObject[]>('/api/vault/objects'), refetchInterval: 10_000 });
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconBuildingWarehouse size={28} stroke={1.5} />
          <Title order={2}>Data Vault</Title>
        </Group>
        {engineer && (
          <Button leftSection={<IconPlus size={16} />} onClick={() => setCreating(true)}>
            New object
          </Button>
        )}
      </Group>
      <Text c="dimmed" maw={820}>
        The raw vault keeps every version of every business entity, insert-only: hubs hold business keys, links hold
        relationships, satellites hold descriptive history. Loads run after each ingestion (and each streaming micro-batch)
        for sources mapped here or in the ingestion wizard. PIT and bridge tables are rebuilt when their inputs change.
      </Text>
      {objects.isLoading ? (
        <Loader />
      ) : (
        // keepMounted=false: the diagram must lay out when visible, not in a hidden tab
        <Tabs defaultValue="objects" keepMounted={false}>
          <Tabs.List>
            <Tabs.Tab value="objects">Objects</Tabs.Tab>
            <Tabs.Tab value="diagram">Model diagram</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="objects" pt="sm">
            {!objects.data?.length ? (
              <Alert variant="light">
                No vault objects yet. Create a hub here, or tick <b>Add to Raw Vault</b> in the ingestion wizard.
              </Alert>
            ) : (
              <Table striped highlightOnHover withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Kind</Table.Th>
                    <Table.Th>Name</Table.Th>
                    <Table.Th>Keys / parent</Table.Th>
                    <Table.Th>Rows</Table.Th>
                    <Table.Th>Sources</Table.Th>
                    <Table.Th>Last loaded</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {objects.data.map((o) => (
                    <Table.Tr key={o.id} data-testid={`vault-${o.name}`}>
                      <Table.Td>
                        <KindBadge kind={o.kind} />
                      </Table.Td>
                      <Table.Td>
                        <Anchor onClick={() => setSelected(o.name)} ff="monospace" fw={500}>
                          {o.name}
                        </Anchor>
                        {o.description && (
                          <Text size="xs" c="dimmed">
                            {o.description}
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td ff="monospace" fz="xs">
                        {o.kind === 'hub'
                          ? o.definition.business_keys.join(', ')
                          : o.kind === 'link'
                            ? o.definition.hubs.map((h: { hub: string }) => h.hub).join(' ↔ ')
                            : o.kind === 'sat'
                              ? `${o.definition.parent}${o.definition.status ? ' (status)' : ''}`
                              : o.definition.hub}
                      </Table.Td>
                      <Table.Td>{fmtNumber(o.rows)}</Table.Td>
                      <Table.Td>{o.kind === 'pit' || o.kind === 'bridge' ? 'built' : o.mappings}</Table.Td>
                      <Table.Td>{fmtTime(o.last_loaded_at)}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
          </Tabs.Panel>
          <Tabs.Panel value="diagram" pt="sm">
            <Diagram objects={objects.data ?? []} onSelect={setSelected} />
          </Tabs.Panel>
        </Tabs>
      )}
      <NewObject opened={creating} onClose={() => setCreating(false)} objects={objects.data ?? []} />
      <ObjectDrawer name={selected} onClose={() => setSelected(null)} engineer={engineer} />
    </Stack>
  );
}
