import { useEffect, useState } from 'react';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Drawer,
  Group,
  Loader,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import {
  IconAlertTriangle,
  IconBolt,
  IconCircleCheck,
  IconCircleX,
  IconClock,
  IconLoader2,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { getAccessToken } from '../../auth/login';
import { api } from '../../api/client';
import { fmtNumber, fmtTime } from '../../api/tasks';
import type { StreamInfo, StreamMetrics } from '../../api/types';
import { StreamCharts } from '../../components/StreamCharts';
import { canEngineer } from '../connections/ConnectionsPage';

const STATUS: Record<string, { color: string; icon: typeof IconCircleCheck; label: string }> = {
  running: { color: 'green', icon: IconCircleCheck, label: 'running' },
  starting: { color: 'blue', icon: IconLoader2, label: 'starting' },
  paused: { color: 'gray', icon: IconPlayerPause, label: 'paused' },
  failed: { color: 'red', icon: IconCircleX, label: 'failed' },
  stalled: { color: 'orange', icon: IconClock, label: 'stalled' },
};

function StreamStatus({ s }: { s: StreamInfo }) {
  const st = STATUS[s.status] ?? STATUS.starting;
  return (
    <Badge color={st.color} variant="light" leftSection={<st.icon size={12} aria-hidden />}>
      {st.label}
    </Badge>
  );
}

const ms = (v: number | null) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);

/** Live stream list over the WebSocket, falling back to polling if it can't connect. */
function useLiveStreams() {
  const [streams, setStreams] = useState<StreamInfo[] | null>(null);
  const [live, setLive] = useState(false);
  const poll = useQuery({ queryKey: ['streams'], queryFn: () => api<StreamInfo[]>('/api/streams'), refetchInterval: live ? false : 5000 });

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    (async () => {
      const token = await getAccessToken();
      if (!token || closed) return;
      ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws/streams`);
      ws.onopen = () => ws?.send(JSON.stringify({ token }));
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'streams') {
          setStreams(msg.streams);
          setLive(true);
        }
      };
      ws.onclose = () => setLive(false);
    })();
    return () => {
      closed = true;
      ws?.close();
    };
  }, []);
  return { streams: streams ?? poll.data ?? null, live, loading: poll.isLoading && !streams };
}

export function StreamsPage() {
  const { user } = useAuth();
  const engineer = canEngineer(user?.roles);
  const { streams, live, loading } = useLiveStreams();
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function act(s: StreamInfo, action: 'pause' | 'resume' | 'resnapshot') {
    if (action === 'resnapshot' && !window.confirm(`Re-read the whole of ${s.source} into ${s.target}?`)) return;
    setBusy(`${s.job_id}:${action}`);
    try {
      await api(`/api/streams/${s.job_id}/${action}`, { method: 'POST' });
      notifications.show({ message: `${s.name}: ${action === 'resnapshot' ? 'snapshot restarted' : action + 'd'}` });
    } catch (e) {
      notifications.show({ color: 'red', message: (e as Error).message });
    } finally {
      setBusy(null);
    }
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconBolt size={28} stroke={1.5} />
          <Title order={2}>Streams & Replication</Title>
          <Badge variant="dot" color={live ? 'green' : 'gray'}>
            {live ? 'live' : 'polling'}
          </Badge>
        </Group>
        {engineer && (
          <Button component={Link} to="/ingestion/new">
            New stream
          </Button>
        )}
      </Group>
      <Text c="dimmed" maw={800}>
        Continuous ingestion: database change capture (Debezium), Kafka topics and webhook events, written to bronze in
        micro-batches. Latency is measured from the source commit to the bronze write.
      </Text>

      {loading ? (
        <Loader />
      ) : !streams?.length ? (
        <Alert variant="light">
          No streams yet. Create an ingestion job with the <b>Real-time replication (CDC)</b> or <b>Continuous stream</b> load mode.
        </Alert>
      ) : (
        <Table striped withTableBorder highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Stream</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Lag</Table.Th>
              <Table.Th>Latency p50 / p95</Table.Th>
              <Table.Th>Records/min</Table.Th>
              <Table.Th>Total</Table.Th>
              <Table.Th>Dead letters</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {streams.map((s) => (
              <Table.Tr key={s.job_id} data-testid={`stream-${s.name}`}>
                <Table.Td>
                  <Anchor onClick={() => setSelected(s.job_id)} fw={500}>
                    {s.name}
                  </Anchor>
                  <Text size="xs" c="dimmed">
                    {s.kind === 'cdc' ? 'CDC' : 'stream'} · {s.connection} · {s.source} → <Code fz="xs">{s.target}</Code> ({s.write_mode})
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Stack gap={2}>
                    <StreamStatus s={s} />
                    {s.connector && s.connector.state !== 'RUNNING' && (
                      <Text size="xs" c="dimmed">
                        connector {s.connector.state.toLowerCase()}
                      </Text>
                    )}
                  </Stack>
                </Table.Td>
                <Table.Td>{fmtNumber(s.lag)}</Table.Td>
                <Table.Td>
                  {ms(s.latency_p50_ms)} / {ms(s.latency_p95_ms)}
                </Table.Td>
                <Table.Td>{fmtNumber(s.records_per_minute)}</Table.Td>
                <Table.Td>{fmtNumber(s.totals.records ?? 0)}</Table.Td>
                <Table.Td>
                  {s.totals.dlq ? (
                    <Badge color="orange" variant="light" leftSection={<IconAlertTriangle size={12} />}>
                      {s.totals.dlq}
                    </Badge>
                  ) : (
                    '0'
                  )}
                </Table.Td>
                <Table.Td>
                  {engineer && (
                    <Group gap={4} wrap="nowrap" justify="flex-end">
                      {s.desired === 'running' ? (
                        <Tooltip label="Pause">
                          <Button size="xs" variant="subtle" loading={busy === `${s.job_id}:pause`} onClick={() => act(s, 'pause')}
                            aria-label={`Pause ${s.name}`}>
                            <IconPlayerPause size={16} />
                          </Button>
                        </Tooltip>
                      ) : (
                        <Tooltip label="Resume">
                          <Button size="xs" variant="subtle" loading={busy === `${s.job_id}:resume`} onClick={() => act(s, 'resume')}
                            aria-label={`Resume ${s.name}`}>
                            <IconPlayerPlay size={16} />
                          </Button>
                        </Tooltip>
                      )}
                      {s.kind === 'cdc' && (
                        <Tooltip label="Re-snapshot">
                          <Button size="xs" variant="subtle" loading={busy === `${s.job_id}:resnapshot`} onClick={() => act(s, 'resnapshot')}
                            aria-label={`Re-snapshot ${s.name}`}>
                            <IconRefresh size={16} />
                          </Button>
                        </Tooltip>
                      )}
                    </Group>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Drawer opened={!!selected} onClose={() => setSelected(null)} position="right" size="xl" title="Stream details">
        {selected && <StreamDetail jobId={selected} engineer={engineer} />}
      </Drawer>
    </Stack>
  );
}

function StreamDetail({ jobId, engineer }: { jobId: string; engineer: boolean }) {
  const m = useQuery({ queryKey: ['stream-metrics', jobId], queryFn: () => api<StreamMetrics>(`/api/streams/${jobId}/metrics`), refetchInterval: 5000 });
  const dlq = useQuery({
    queryKey: ['stream-dlq', jobId],
    queryFn: () => api<{ offset: string; error: string; source_offset: string; value: string }[]>(`/api/streams/${jobId}/dlq`),
    enabled: engineer && !!m.data?.stream.totals.dlq,
  });
  if (!m.data) return <Loader />;
  const s = m.data.stream;
  return (
    <Stack>
      <Group>
        <Title order={3}>{s.name}</Title>
        <StreamStatus s={s} />
      </Group>
      {s.last_error && <Alert color="red" title="Last error">{s.last_error}</Alert>}
      <SimpleGrid cols={3}>
        <Paper withBorder p="xs">
          <Text size="xs" c="dimmed">Source → target</Text>
          <Text size="sm">{s.source}</Text>
          <Code fz="xs">{s.target}</Code>
        </Paper>
        <Paper withBorder p="xs">
          <Text size="xs" c="dimmed">Mode · key</Text>
          <Text size="sm">
            {s.write_mode}
            {s.key_columns?.length ? ` · ${s.key_columns.join(', ')}` : ''}
          </Text>
        </Paper>
        <Paper withBorder p="xs">
          <Text size="xs" c="dimmed">Last batch · heartbeat</Text>
          <Text size="sm">{fmtTime(s.last_batch_at)}</Text>
          <Text size="xs" c="dimmed">{fmtTime(s.heartbeat_at)}</Text>
        </Paper>
      </SimpleGrid>
      <Text size="xs" c="dimmed">
        Topic <Code fz="xs">{s.topics.join(', ')}</Code>
        {s.connector && (
          <>
            {' '}· Debezium connector <b>{s.connector.state}</b>
          </>
        )}
      </Text>
      <StreamCharts series={m.data.series} />
      {!!s.totals.dlq && engineer && (
        <Paper withBorder p="sm">
          <Group gap={6} mb="xs">
            <IconAlertTriangle size={16} />
            <Text fw={600} size="sm">Dead letters ({s.totals.dlq})</Text>
          </Group>
          <Table fz="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Source offset</Table.Th>
                <Table.Th>Error</Table.Th>
                <Table.Th>Value (start)</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {dlq.data?.map((d) => (
                <Table.Tr key={d.offset}>
                  <Table.Td>{d.source_offset}</Table.Td>
                  <Table.Td>{d.error}</Table.Td>
                  <Table.Td ff="monospace">{d.value}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Paper>
      )}
      <Anchor component={Link} to={`/ingestion/${s.job_id}`} size="sm">
        Job definition and versions
      </Anchor>
    </Stack>
  );
}
