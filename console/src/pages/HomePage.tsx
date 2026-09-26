import { Badge, Card, Group, Loader, SimpleGrid, Stack, Table, Text, ThemeIcon, Title } from '@mantine/core';
import { IconCircleCheck, IconCircleX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../api/client';
import type { ComponentHealth, JobRun } from '../api/types';

const LABELS: Record<string, string> = {
  secret_store: 'Secret store',
  metadata_store: 'Metadata store',
  object_store: 'Object store',
  query_engine: 'Query engine',
  event_bus: 'Event bus',
  knowledge_graph: 'Knowledge graph',
};

const STATUS_COLOR: Record<JobRun['status'], string> = {
  queued: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
};

export function HomePage() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: () => api<{ ok: boolean; components: ComponentHealth[] }>('/api/health/components'),
    refetchInterval: 15_000,
  });
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => api<JobRun[]>('/api/jobs?limit=10'), refetchInterval: 10_000 });

  return (
    <Stack>
      <Title order={2}>Operations</Title>

      <Title order={4}>Platform components</Title>
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

      <Title order={4} mt="md">
        Recent runs
      </Title>
      {jobs.data && jobs.data.length === 0 ? (
        <Text c="dimmed">No runs yet. Ingestion jobs arrive in Phase 1.</Text>
      ) : (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>#</Table.Th>
              <Table.Th>Kind</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Attempts</Table.Th>
              <Table.Th>Created</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {jobs.data?.map((j) => (
              <Table.Tr key={j.id}>
                <Table.Td>{j.id}</Table.Td>
                <Table.Td>{j.kind}</Table.Td>
                <Table.Td>
                  <Badge color={STATUS_COLOR[j.status]} variant="light">
                    {j.status}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  {j.attempts}/{j.max_attempts}
                </Table.Td>
                <Table.Td>{new Date(j.created_at).toLocaleString()}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
