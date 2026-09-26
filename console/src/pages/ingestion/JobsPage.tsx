import { useState } from 'react';
import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Paper,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  Title,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconDatabaseImport, IconPlayerPlay, IconPencil } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtBytes, fmtDuration, fmtNumber, fmtTime } from '../../api/tasks';
import type { IngestionJob, IngestionRun } from '../../api/types';
import { StatusBadge } from '../../components/StatusBadge';
import { canEngineer } from '../connections/ConnectionsPage';

function useRunNow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/ingestion/jobs/${id}/run`, { method: 'POST' }),
    onSuccess: () => {
      notifications.show({ message: 'Run queued' });
      setTimeout(() => qc.invalidateQueries(), 1500);
    },
    onError: (e) => notifications.show({ color: 'red', message: (e as Error).message }),
  });
}

function schedule(j: IngestionJob) {
  const s = j.spec.schedule;
  if (s.type === 'cron') return `cron ${s.cron}`;
  if (s.type === 'interval') return `every ${(s.interval_seconds ?? 0) / 60} min`;
  return 'manual';
}

export function JobsPage() {
  const { user } = useAuth();
  const jobs = useQuery({ queryKey: ['ingestion-jobs'], queryFn: () => api<IngestionJob[]>('/api/ingestion/jobs'), refetchInterval: 10_000 });
  const run = useRunNow();
  const engineer = canEngineer(user?.roles);

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconDatabaseImport size={28} stroke={1.5} />
          <Title order={2}>Ingestion jobs</Title>
        </Group>
        {engineer && (
          <Button component={Link} to="/ingestion/new">
            New ingestion job
          </Button>
        )}
      </Group>
      {jobs.isLoading ? (
        <Loader />
      ) : (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Job</Table.Th>
              <Table.Th>Source</Table.Th>
              <Table.Th>Target</Table.Th>
              <Table.Th>Mode</Table.Th>
              <Table.Th>Schedule</Table.Th>
              <Table.Th>Last run</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {jobs.data?.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={7}>
                  <Text c="dimmed">No ingestion jobs yet.</Text>
                </Table.Td>
              </Table.Tr>
            )}
            {jobs.data?.map((j) => (
              <Table.Tr key={j.id}>
                <Table.Td>
                  <Anchor component={Link} to={`/ingestion/${j.id}`} fw={500}>
                    {j.name}
                  </Anchor>
                  {!j.enabled && (
                    <Badge ml={6} size="xs" color="gray">
                      disabled
                    </Badge>
                  )}
                </Table.Td>
                <Table.Td>
                  <Text size="sm">{j.connection_name}</Text>
                  <Text size="xs" c="dimmed" ff="monospace">
                    {j.spec.source.query ? 'SQL query' : j.spec.source.path_template ?? j.spec.source.object}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Code>{j.target}</Code>
                </Table.Td>
                <Table.Td>{j.spec.load_mode}</Table.Td>
                <Table.Td>
                  <Text size="sm">{schedule(j)}</Text>
                  {j.next_run_at && (
                    <Text size="xs" c="dimmed">
                      next {fmtTime(j.next_run_at)}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  {j.last_run ? (
                    <Group gap={6}>
                      <StatusBadge status={j.last_run.status} />
                      <Text size="xs" c="dimmed">
                        {fmtTime(j.last_run.started_at)}
                      </Text>
                    </Group>
                  ) : (
                    <Text size="sm" c="dimmed">
                      never
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  {engineer && (
                    <Button
                      size="xs"
                      variant="light"
                      leftSection={<IconPlayerPlay size={14} />}
                      onClick={() => run.mutate(j.id)}
                      aria-label={`Run ${j.name} now`}
                    >
                      Run
                    </Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

export function JobDetailPage() {
  const { jobId } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const job = useQuery({ queryKey: ['ingestion-job', jobId], queryFn: () => api<IngestionJob>(`/api/ingestion/jobs/${jobId}`), refetchInterval: 5000 });
  const runs = useQuery({
    queryKey: ['ingestion-runs', jobId],
    queryFn: () => api<IngestionRun[]>(`/api/ingestion/runs?job_id=${jobId}&limit=50`),
    refetchInterval: 5000,
  });
  const versions = useQuery({
    queryKey: ['ingestion-versions', jobId],
    queryFn: () => api<{ version: number; spec: unknown; created_by: string; created_at: string }[]>(`/api/ingestion/jobs/${jobId}/versions`),
  });
  const run = useRunNow();
  const engineer = canEngineer(user?.roles);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api(`/api/ingestion/jobs/${jobId}`, { method: 'PUT', body: json({ enabled }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ingestion-job', jobId] }),
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/ingestion/jobs/${jobId}`, { method: 'DELETE' }),
    onSuccess: () => navigate('/ingestion'),
  });
  const reset = useMutation({
    mutationFn: () => api(`/api/ingestion/jobs/${jobId}/reset-state`, { method: 'POST' }),
    onSuccess: () => {
      notifications.show({ message: 'Watermark and file history cleared; the next run starts from scratch.' });
      qc.invalidateQueries({ queryKey: ['ingestion-job', jobId] });
    },
  });

  if (job.isLoading) return <Loader />;
  if (!job.data) return <Alert color="red">Job not found.</Alert>;
  const j = job.data;

  return (
    <Stack>
      <Group justify="space-between">
        <div>
          <Title order={2}>{j.name}</Title>
          <Text c="dimmed">{j.description}</Text>
        </div>
        {engineer && (
          <Group>
            <Switch label="Enabled" checked={j.enabled} onChange={(e) => toggle.mutate(e.currentTarget.checked)} />
            <Button variant="default" leftSection={<IconPencil size={16} />} component={Link} to={`/ingestion/${j.id}/edit`}>
              Edit
            </Button>
            <Button leftSection={<IconPlayerPlay size={16} />} onClick={() => run.mutate(j.id)} loading={run.isPending}>
              Run now
            </Button>
          </Group>
        )}
      </Group>

      <SimpleGrid cols={{ base: 2, md: 4 }}>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Source</Text>
          <Text size="sm">{j.connection_name} ({j.connection_type})</Text>
          <Text size="xs" ff="monospace">{j.spec.source.query ?? j.spec.source.path_template ?? j.spec.source.object}</Text>
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Target</Text>
          <Code>{j.target}</Code>
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Load mode</Text>
          <Text size="sm">
            {j.spec.load_mode}
            {j.spec.watermark_column && ` on ${j.spec.watermark_column}`}
          </Text>
          {j.watermark && <Text size="xs" c="dimmed">watermark {j.watermark}</Text>}
        </Paper>
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed">Schedule · version</Text>
          <Text size="sm">
            {schedule(j)} · v{j.version}
          </Text>
          {j.next_run_at && <Text size="xs" c="dimmed">next {fmtTime(j.next_run_at)}</Text>}
        </Paper>
      </SimpleGrid>

      <Tabs defaultValue="runs">
        <Tabs.List>
          <Tabs.Tab value="runs">Runs</Tabs.Tab>
          <Tabs.Tab value="versions">Versions</Tabs.Tab>
          {engineer && <Tabs.Tab value="danger">Maintenance</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="runs" pt="sm">
          <Accordion variant="separated">
            {runs.data?.map((r) => (
              <Accordion.Item key={r.id} value={r.id}>
                <Accordion.Control>
                  <Group gap="md" wrap="nowrap">
                    <StatusBadge status={r.status} />
                    <Text size="sm" w={180}>{fmtTime(r.started_at)}</Text>
                    <Text size="sm" w={110}>{fmtNumber(r.rows_written)} rows</Text>
                    <Text size="sm" w={90}>{fmtDuration(r.duration_ms)}</Text>
                    <Text size="xs" c="dimmed">{r.trigger} · v{r.job_version}</Text>
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  <Stack gap="xs">
                    {r.error && <Alert color="red">{r.error}</Alert>}
                    <Text size="sm">
                      Read {fmtNumber(r.rows_read)} rows ({fmtBytes(r.bytes_read)}) from {r.files} file(s); Delta version{' '}
                      {r.table_version ?? '—'}; batch <Code>{r.batch_id}</Code>
                    </Text>
                    {!!r.details.schema_changes?.length && (
                      <Alert color="yellow" title="Schema changed">
                        {r.details.schema_changes.map((c) => (
                          <div key={c.column}>
                            {c.change} <Code>{c.column}</Code> {c.type ?? `${c.from} → ${c.to}`}
                          </div>
                        ))}
                      </Alert>
                    )}
                    {!!r.details.files?.length && (
                      <Table fz="xs">
                        <Table.Thead>
                          <Table.Tr>
                            <Table.Th>File</Table.Th>
                            <Table.Th>Rows</Table.Th>
                            <Table.Th>Size</Table.Th>
                            <Table.Th>SHA-256</Table.Th>
                          </Table.Tr>
                        </Table.Thead>
                        <Table.Tbody>
                          {r.details.files.map((f) => (
                            <Table.Tr key={f.path}>
                              <Table.Td ff="monospace">{f.path}</Table.Td>
                              <Table.Td>{f.rows}</Table.Td>
                              <Table.Td>{fmtBytes(f.size)}</Table.Td>
                              <Table.Td ff="monospace">{f.sha256.slice(0, 16)}…</Table.Td>
                            </Table.Tr>
                          ))}
                        </Table.Tbody>
                      </Table>
                    )}
                    {r.dataset_id && (
                      <Anchor component={Link} to={`/catalog/${r.dataset_id}`} size="sm">
                        Open {j.target} in the catalog
                      </Anchor>
                    )}
                  </Stack>
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
          {runs.data?.length === 0 && <Text c="dimmed">No runs yet.</Text>}
        </Tabs.Panel>
        <Tabs.Panel value="versions" pt="sm">
          <Stack>
            {versions.data?.map((v) => (
              <Paper key={v.version} withBorder p="sm">
                <Text size="sm" fw={500}>
                  v{v.version} · {v.created_by} · {fmtTime(v.created_at)}
                </Text>
                <Code block>{JSON.stringify(v.spec, null, 2)}</Code>
              </Paper>
            ))}
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="danger" pt="sm">
          <Stack maw={560}>
            <Text size="sm">Forget the watermark and the list of loaded files, so the next run reads everything again.</Text>
            <Button variant="light" color="orange" onClick={() => reset.mutate()} w={220}>
              Reset incremental state
            </Button>
            <Text size="sm">Delete the job. The bronze table and its catalog entry stay.</Text>
            {confirmDelete ? (
              <Group>
                <Button color="red" onClick={() => remove.mutate()}>
                  Yes, delete {j.name}
                </Button>
                <Button variant="default" onClick={() => setConfirmDelete(false)}>
                  Cancel
                </Button>
              </Group>
            ) : (
              <Button variant="light" color="red" onClick={() => setConfirmDelete(true)} w={220}>
                Delete job
              </Button>
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
