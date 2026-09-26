import { lazy, Suspense, useState } from 'react';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Paper,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconBook2, IconSearch } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtBytes, fmtNumber, fmtTime } from '../../api/tasks';
import type { ColumnProfile, Dataset, DatasetDetail, Preview } from '../../api/types';
import { PreviewTable } from '../ingestion/JobWizard';

const LineageView = lazy(() => import('../lineage/LineagePage').then((m) => ({ default: m.LineageView })));

const LAYER_COLOR: Record<string, string> = { bronze: 'orange', silver: 'gray', gold: 'yellow', vault: 'violet' };

export function CatalogPage() {
  const [q, setQ] = useState('');
  const [layer, setLayer] = useState('all');
  const [debounced] = useDebouncedValue(q, 250);
  const results = useQuery({
    queryKey: ['catalog', debounced, layer],
    queryFn: () =>
      api<Dataset[]>(`/api/catalog/datasets?${new URLSearchParams({ ...(debounced && { q: debounced }), ...(layer !== 'all' && { layer }) })}`),
  });

  return (
    <Stack>
      <Group>
        <IconBook2 size={28} stroke={1.5} />
        <Title order={2}>Catalog</Title>
      </Group>
      <Group>
        <TextInput
          placeholder="Search datasets and columns"
          leftSection={<IconSearch size={16} />}
          w={380}
          value={q}
          onChange={(e) => setQ(e.currentTarget.value)}
          aria-label="Search the catalog"
        />
        <SegmentedControl
          value={layer}
          onChange={setLayer}
          data={['all', 'bronze', 'silver', 'gold'].map((l) => ({ label: l, value: l }))}
          aria-label="Layer"
        />
      </Group>
      {results.isLoading ? (
        <Loader />
      ) : (
        <Table striped withTableBorder highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Dataset</Table.Th>
              <Table.Th>Layer</Table.Th>
              <Table.Th>Rows</Table.Th>
              <Table.Th>Columns</Table.Th>
              <Table.Th>Size</Table.Th>
              <Table.Th>Last loaded</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {results.data?.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={6}>
                  <Text c="dimmed">Nothing found.</Text>
                </Table.Td>
              </Table.Tr>
            )}
            {results.data?.map((d) => (
              <Table.Tr key={d.id}>
                <Table.Td>
                  <Anchor component={Link} to={`/catalog/${d.id}`} fw={500}>
                    {d.name}
                  </Anchor>
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    {d.description}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge color={LAYER_COLOR[d.layer]} variant="light">
                    {d.layer}
                  </Badge>
                </Table.Td>
                <Table.Td>{fmtNumber(d.row_count)}</Table.Td>
                <Table.Td>{d.columns}</Table.Td>
                <Table.Td>{fmtBytes(d.size_bytes)}</Table.Td>
                <Table.Td>{fmtTime(d.last_loaded_at)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

function ProfileCell({ p }: { p: ColumnProfile }) {
  const range = p.min !== undefined && p.min !== null ? `${String(p.min)} … ${String(p.max)}` : null;
  return (
    <Stack gap={2}>
      {range && <Text size="xs">range {range}</Text>}
      {p.mean != null && <Text size="xs">mean {p.mean.toFixed(2)}</Text>}
      {p.min_length != null && <Text size="xs">length {p.min_length}–{p.max_length}</Text>}
      {p.patterns && Object.keys(p.patterns).length > 0 && (
        <Group gap={4}>
          {Object.entries(p.patterns).map(([k, v]) => (
            <Badge key={k} size="xs" variant="outline">
              {k} {v}%
            </Badge>
          ))}
        </Group>
      )}
      {p.top_values && p.top_values.length > 0 && (
        <Tooltip label={p.top_values.map((t) => `${t.value} (${t.count})`).join(', ')} multiline w={320}>
          <Text size="xs" c="dimmed" lineClamp={1}>
            top: {p.top_values.map((t) => t.value).join(', ')}
          </Text>
        </Tooltip>
      )}
    </Stack>
  );
}

export function DatasetPage() {
  const { datasetId } = useParams();
  const { user } = useAuth();
  const qc = useQueryClient();
  const ds = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => api<DatasetDetail>(`/api/catalog/datasets/${datasetId}`) });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [description, setDescription] = useState<string | null>(null);
  const roles = user?.roles ?? [];
  const canPreview = ['admin', 'engineer', 'analyst', 'steward'].some((r) => roles.includes(r));
  const canEdit = ['admin', 'engineer', 'steward'].some((r) => roles.includes(r));

  const loadPreview = useMutation({
    mutationFn: () => api<Preview>(`/api/catalog/datasets/${datasetId}/preview?limit=50`),
    onSuccess: setPreview,
  });
  const saveDescription = useMutation({
    mutationFn: (d: string) => api(`/api/catalog/datasets/${datasetId}`, { method: 'PATCH', body: json({ description: d }) }),
    onSuccess: () => {
      setDescription(null);
      qc.invalidateQueries({ queryKey: ['dataset', datasetId] });
    },
  });

  if (ds.isLoading) return <Loader />;
  if (!ds.data) return <Alert color="red">Dataset not found.</Alert>;
  const d = ds.data;
  const profiles = new Map((d.profile?.columns ?? []).map((c) => [c.name, c]));
  const nodeId = `dataset:${d.uri.slice(0, d.uri.lastIndexOf('/'))}|${d.name}`;

  return (
    <Stack>
      <Group>
        <Title order={2}>{d.name}</Title>
        <Badge color={LAYER_COLOR[d.layer]} variant="light">
          {d.layer}
        </Badge>
      </Group>
      {description === null ? (
        <Group gap="xs">
          <Text c={d.description ? undefined : 'dimmed'}>{d.description || 'No description yet.'}</Text>
          {canEdit && (
            <Button size="compact-xs" variant="subtle" onClick={() => setDescription(d.description)}>
              Edit
            </Button>
          )}
        </Group>
      ) : (
        <Group align="flex-end">
          <Textarea w={520} autosize value={description} onChange={(e) => setDescription(e.currentTarget.value)} aria-label="Description" />
          <Button size="xs" onClick={() => saveDescription.mutate(description)}>
            Save
          </Button>
        </Group>
      )}
      <SimpleGrid cols={{ base: 2, md: 4 }}>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Rows</Text>
          <Text fw={600}>{fmtNumber(d.row_count)}</Text>
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Size · Delta version</Text>
          <Text fw={600}>
            {fmtBytes(d.size_bytes)} · v{d.table_version ?? '—'}
          </Text>
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Loaded by</Text>
          {d.source_job_id ? (
            <Anchor component={Link} to={`/ingestion/${d.source_job_id}`}>
              {d.source_job}
            </Anchor>
          ) : (
            <Text>—</Text>
          )}
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Last loaded</Text>
          <Text fw={600}>{fmtTime(d.last_loaded_at)}</Text>
        </Paper>
      </SimpleGrid>
      <Code>{d.uri}</Code>

      <Tabs defaultValue="columns">
        <Tabs.List>
          <Tabs.Tab value="columns">Columns & profile</Tabs.Tab>
          <Tabs.Tab value="changes">Schema changes ({d.schema_changes.length})</Tabs.Tab>
          {canPreview && <Tabs.Tab value="preview">Preview</Tabs.Tab>}
          <Tabs.Tab value="lineage">Lineage</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="columns" pt="sm">
          {d.profile && (
            <Text size="xs" c="dimmed" mb="xs">
              Profiled {fmtTime(d.profile.ts)} over {fmtNumber(d.profile.row_count)} rows
            </Text>
          )}
          <Table withTableBorder striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Column</Table.Th>
                <Table.Th>Type</Table.Th>
                <Table.Th>Nulls</Table.Th>
                <Table.Th>Distinct</Table.Th>
                <Table.Th>Profile</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {d.column_list
                .filter((c) => !c.removed_at)
                .map((c) => {
                  const p = profiles.get(c.name);
                  return (
                    <Table.Tr key={c.name}>
                      <Table.Td>
                        <Text size="sm" ff="monospace" c={c.is_audit ? 'dimmed' : undefined}>
                          {c.name}
                        </Text>
                        {c.is_audit && <Text size="xs" c="dimmed">audit column</Text>}
                        {c.description && <Text size="xs">{c.description}</Text>}
                      </Table.Td>
                      <Table.Td>
                        <Code>{c.data_type}</Code>
                      </Table.Td>
                      <Table.Td>{p ? `${p.null_pct}%` : '—'}</Table.Td>
                      <Table.Td>{p ? fmtNumber(p.distinct) : '—'}</Table.Td>
                      <Table.Td>{p ? <ProfileCell p={p} /> : null}</Table.Td>
                    </Table.Tr>
                  );
                })}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>
        <Tabs.Panel value="changes" pt="sm">
          <Stack>
            {d.schema_changes.length === 0 && <Text c="dimmed">No schema changes since the first load.</Text>}
            {d.schema_changes.map((sc) => (
              <Paper key={sc.ts} withBorder p="sm">
                <Text size="sm" fw={500}>{fmtTime(sc.ts)}</Text>
                {sc.changes.map((c) => (
                  <Text key={c.column + c.change} size="sm">
                    <Badge size="xs" variant="light" color={c.change === 'removed' ? 'red' : c.change === 'type_changed' ? 'orange' : 'green'}>
                      {c.change}
                    </Badge>{' '}
                    <Code>{c.column}</Code> {c.type ?? `${c.from} → ${c.to}`}
                  </Text>
                ))}
              </Paper>
            ))}
          </Stack>
        </Tabs.Panel>
        {canPreview && (
          <Tabs.Panel value="preview" pt="sm">
            {preview ? (
              <PreviewTable preview={preview} />
            ) : (
              <Button onClick={() => loadPreview.mutate()} loading={loadPreview.isPending}>
                Show the first 50 rows
              </Button>
            )}
            <Text size="xs" c="dimmed" mt="xs">
              Previews are recorded in the audit log.
            </Text>
          </Tabs.Panel>
        )}
        <Tabs.Panel value="lineage" pt="sm">
          <Suspense fallback={<Loader />}>
            <LineageView node={nodeId} height={460} />
          </Suspense>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
