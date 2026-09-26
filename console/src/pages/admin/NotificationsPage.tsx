import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  MultiSelect,
  PasswordInput,
  Select,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  TextInput,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconPlus, IconSend, IconTrash } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { api, json } from '../../api/client';
import { fmtTime, runTask } from '../../api/tasks';
import type { NotificationChannelInfo, Task } from '../../api/types';

const KINDS = ['freshness', 'stream_failed', 'dq_failed', 'dq_degradation'];

function ChannelForm({ edit, onClose }: { edit: NotificationChannelInfo | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(edit?.name ?? '');
  const [type, setType] = useState<'webhook' | 'email'>(edit?.type ?? 'webhook');
  const [recipients, setRecipients] = useState<string[]>(edit?.recipients ?? []);
  const [kinds, setKinds] = useState<string[]>(edit?.kinds ?? []);
  const [minSeverity, setMinSeverity] = useState<string>(edit?.min_severity ?? 'warning');
  const [enabled, setEnabled] = useState(edit?.enabled ?? true);
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    const secrets: Record<string, string> = {};
    if (type === 'webhook' && url) {
      secrets.url = url;
      if (token) secrets.token = token;
    }
    try {
      await api(edit ? `/api/admin/notifications/${edit.id}` : '/api/admin/notifications', {
        method: edit ? 'PUT' : 'POST',
        body: json({ name, type, recipients, kinds, min_severity: minSeverity, enabled, secrets }),
      });
      qc.invalidateQueries({ queryKey: ['notifications'] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <Modal opened onClose={onClose} title={edit ? `Edit ${edit.name}` : 'New notification channel'} size="lg">
      <Stack>
        <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <Select label="Type" data={['webhook', 'email']} value={type} onChange={(v) => setType((v as 'webhook' | 'email') ?? 'webhook')} disabled={!!edit} />
        {type === 'webhook' ? (
          <>
            <PasswordInput
              label="Webhook URL (stored in Vault)"
              description={edit ? 'Leave empty to keep the stored URL and token' : 'Often contains a token, so it is treated as a secret'}
              value={url}
              onChange={(e) => setUrl(e.currentTarget.value)}
            />
            <PasswordInput label="Bearer token (optional, stored in Vault)" value={token} onChange={(e) => setToken(e.currentTarget.value)} />
          </>
        ) : (
          <TagsInput label="Recipients" description="Email addresses" value={recipients} onChange={setRecipients} />
        )}
        <MultiSelect label="Alert kinds" description="Empty: every kind" data={KINDS} value={kinds} onChange={setKinds} />
        <Select label="Minimum severity" data={['warning', 'serious', 'critical']} value={minSeverity} onChange={(v) => setMinSeverity(v ?? 'warning')} />
        <Switch label="Enabled" checked={enabled} onChange={(e) => setEnabled(e.currentTarget.checked)} />
        {error && <Alert color="red">{error}</Alert>}
        <Group justify="flex-end">
          <Button onClick={save} disabled={!name}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function NotificationsPage() {
  const qc = useQueryClient();
  const channels = useQuery({ queryKey: ['notifications'], queryFn: () => api<NotificationChannelInfo[]>('/api/admin/notifications') });
  const [form, setForm] = useState<{ edit: NotificationChannelInfo | null } | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  async function test(c: NotificationChannelInfo) {
    setTesting(c.id);
    try {
      const t = await runTask<{ ok: boolean; error: string | null }>(
        api<Task<{ ok: boolean; error: string | null }>>(`/api/admin/notifications/${c.id}/test`, { method: 'POST' }),
      );
      notifications.show(
        t.result?.ok ? { message: `${c.name}: test sent` } : { color: 'red', message: `${c.name}: ${t.result?.error ?? t.error}` },
      );
      qc.invalidateQueries({ queryKey: ['notifications'] });
    } finally {
      setTesting(null);
    }
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed">Where alerts (stale datasets, failed streams, data quality) are sent. Channel secrets live in Vault.</Text>
        <Button leftSection={<IconPlus size={16} />} onClick={() => setForm({ edit: null })}>
          New channel
        </Button>
      </Group>
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Channel</Table.Th>
            <Table.Th>Sends</Table.Th>
            <Table.Th>Last sent</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {channels.data?.map((c) => (
            <Table.Tr key={c.id}>
              <Table.Td>
                <Group gap={6}>
                  <Text fw={500}>{c.name}</Text>
                  <Badge size="xs" variant="light">{c.type}</Badge>
                  {!c.enabled && <Badge size="xs" color="gray">disabled</Badge>}
                </Group>
                <Text size="xs" c="dimmed">
                  {c.type === 'email' ? c.recipients.join(', ') : `secrets in Vault: ${c.secret_fields.join(', ')}`}
                </Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm">{c.kinds.length ? c.kinds.join(', ') : 'all alerts'}</Text>
                <Text size="xs" c="dimmed">{c.min_severity} and above</Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm">{fmtTime(c.last_sent_at)}</Text>
                {c.last_error && <Text size="xs" c="red">{c.last_error}</Text>}
              </Table.Td>
              <Table.Td>
                <Group gap={4} wrap="nowrap" justify="flex-end">
                  <Button size="compact-xs" variant="light" leftSection={<IconSend size={12} />} loading={testing === c.id} onClick={() => test(c)}>
                    Test
                  </Button>
                  <Button size="compact-xs" variant="subtle" onClick={() => setForm({ edit: c })}>
                    Edit
                  </Button>
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label={`Delete ${c.name}`}
                    onClick={async () => {
                      if (!window.confirm(`Delete channel ${c.name}?`)) return;
                      await api(`/api/admin/notifications/${c.id}`, { method: 'DELETE' });
                      qc.invalidateQueries({ queryKey: ['notifications'] });
                    }}
                  >
                    <IconTrash size={16} />
                  </ActionIcon>
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {form && <ChannelForm edit={form.edit} onClose={() => setForm(null)} />}
    </Stack>
  );
}
