import { useState } from 'react';
import {
  Anchor,
  Badge,
  Card,
  Group,
  Loader,
  Paper,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Text,
  ThemeIcon,
  Title,
} from '@mantine/core';
import { IconAlertTriangle, IconCircleCheck, IconCircleX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { api } from '../api/client';
import { fmtBytes, fmtDuration, fmtNumber, fmtTime } from '../api/tasks';
import type { ComponentHealth, OpsSummary } from '../api/types';
import { RunsChart } from '../components/RunsChart';
import { StatusBadge } from '../components/StatusBadge';

const LABELS: Record<string, string> = {
  secret_store: 'Secret store',
  metadata_store: 'Metadata store',
  object_store: 'Object store',
  query_engine: 'Query engine',
  event_bus: 'Event bus',
  knowledge_graph: 'Knowledge graph',
};

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Paper withBorder p="md" radius="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text fz={26} fw={700} lh={1.2}>
        {value}
      </Text>
      {hint && (
        <Text size="xs" c="dimmed">
          {hint}
        </Text>
      )}
    </Paper>
  );
}

export function HomePage() {
  const [hours, setHours] = useState('24');
  const health = useQuery({
    queryKey: ['health'],
    queryFn: () => api<{ ok: boolean; components: ComponentHealth[] }>('/api/health/components'),
    refetchInterval: 15_000,
  });
  const ops = useQuery({
    queryKey: ['ops', hours],
    queryFn: () => api<OpsSummary>(`/api/ops/summary?hours=${hours}`),
    refetchInterval: 10_000,
  });
  const t = ops.data?.totals;

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Operations</Title>
        <SegmentedControl
          value={hours}
          onChange={setHours}
          data={[
            { label: '24 hours', value: '24' },
            { label: '7 days', value: String(24 * 7) },
            { label: '30 days', value: String(24 * 30) },
          ]}
          aria-label="Time range"
        />
      </Group>

      <SimpleGrid cols={{ base: 2, md: 5 }}>
        <Stat label="Runs" value={fmtNumber(t?.runs)} hint={t ? `${t.running} running` : undefined} />
        <Stat
          label="Succeeded"
          value={fmtNumber(t?.succeeded)}
          hint={t && t.runs ? `${Math.round((100 * t.succeeded) / Math.max(t.succeeded + t.failed, 1))}% of finished` : undefined}
        />
        <Stat label="Failed" value={fmtNumber(t?.failed)} />
        <Stat label="Rows loaded" value={fmtNumber(t?.rows_written)} hint={t ? `${fmtBytes(t.bytes_read)} read` : undefined} />
        <Stat label="Median duration" value={fmtDuration(t?.p50_duration_ms)} hint={t ? `max ${fmtDuration(t.max_duration_ms)}` : undefined} />
      </SimpleGrid>

      <Paper withBorder p="md" radius="md">
        <Text fw={600} mb="xs">
          Ingestion runs
        </Text>
        {ops.isLoading ? <Loader size="sm" /> : <RunsChart series={ops.data?.series ?? []} />}
      </Paper>

      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Paper withBorder p="md" radius="md">
          <Text fw={600} mb="xs">
            Jobs
          </Text>
          {ops.data?.jobs.length ? (
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Job</Table.Th>
                  <Table.Th>Last run</Table.Th>
                  <Table.Th>Rows</Table.Th>
                  <Table.Th>Duration</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {ops.data.jobs.map((j) => (
                  <Table.Tr key={j.job_id}>
                    <Table.Td>
                      <Anchor component={Link} to={`/ingestion/${j.job_id}`}>
                        {j.job}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      <StatusBadge status={j.last_status} />
                    </Table.Td>
                    <Table.Td>{fmtNumber(j.rows_written)}</Table.Td>
                    <Table.Td>{fmtDuration(j.duration_ms)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          ) : (
            <Text c="dimmed" size="sm">
              No runs yet. <Anchor component={Link} to="/ingestion/new">Create an ingestion job</Anchor>.
            </Text>
          )}
        </Paper>

        <Paper withBorder p="md" radius="md">
          <Group gap={6} mb="xs">
            <IconAlertTriangle size={18} aria-hidden />
            <Text fw={600}>Recent failures</Text>
          </Group>
          {ops.data?.recent_failures.length ? (
            <Stack gap="xs">
              {ops.data.recent_failures.map((f) => (
                <div key={f.run_id}>
                  <Group gap="xs">
                    <Badge color="red" variant="light" leftSection={<IconCircleX size={12} />}>
                      failed
                    </Badge>
                    <Text size="sm" fw={500}>
                      {f.job}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {fmtTime(f.started_at)}
                    </Text>
                  </Group>
                  <Text size="xs" c="dimmed" lineClamp={2}>
                    {f.error}
                  </Text>
                </div>
              ))}
            </Stack>
          ) : (
            <Text c="dimmed" size="sm">
              None in this period.
            </Text>
          )}
        </Paper>
      </SimpleGrid>

      <Title order={4} mt="sm">
        Platform components
      </Title>
      {health.isLoading ? (
        <Loader size="sm" />
      ) : health.isError ? (
        <Text c="red">Could not load component health: {String(health.error)}</Text>
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
          {health.data?.components.map((c) => (
            <Card key={c.name} withBorder padding="md" data-testid={`component-${c.name}`}>
              <Group justify="space-between" wrap="nowrap">
                <div>
                  <Text fw={600}>{LABELS[c.name] ?? c.name}</Text>
                  <Text size="xs" c="dimmed">
                    {c.adapter} · {c.latency_ms} ms
                  </Text>
                </div>
                <ThemeIcon color={c.ok ? 'green' : 'red'} variant="light" radius="xl" aria-label={c.ok ? 'healthy' : 'unhealthy'}>
                  {c.ok ? <IconCircleCheck size={18} /> : <IconCircleX size={18} />}
                </ThemeIcon>
              </Group>
              {c.error && (
                <Text size="xs" c="red" mt={4}>
                  {c.error}
                </Text>
              )}
            </Card>
          ))}
        </SimpleGrid>
      )}
    </Stack>
  );
}
