import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconCircleCheck, IconCircleX, IconLock, IconPencil, IconPlugConnected, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtTime, runTask } from '../../api/tasks';
import type { Connection, ConnectionType, ServiceAccount, Task } from '../../api/types';
import { SchemaForm } from '../../components/SchemaForm';

export function useConnectionTypes() {
  return useQuery({ queryKey: ['connection-types'], queryFn: () => api<ConnectionType[]>('/api/connection-types'), staleTime: 60_000 });
}

export function canEngineer(roles: string[] | undefined) {
  return !!roles && (roles.includes('admin') || roles.includes('engineer'));
}

function isRef(v: unknown) {
  return typeof v === 'string' && v.startsWith('vault://');
}

function storedSecretPaths(config: Record<string, unknown>, fields: string[]): Set<string> {
  const out = new Set<string>();
  for (const f of fields) {
    let v: unknown = config;
    for (const p of f.split('.')) v = v && typeof v === 'object' ? (v as Record<string, unknown>)[p] : undefined;
    if (isRef(v)) out.add(f);
  }
  return out;
}

export async function testConnection(id: string) {
  const t = await runTask<{ ok: boolean; message: string }>(api<Task<{ ok: boolean; message: string }>>(`/api/connections/${id}/test`, { method: 'POST' }));
  const ok = t.status === 'succeeded' && t.result?.ok;
  notifications.show({
    color: ok ? 'green' : 'red',
    icon: ok ? <IconCircleCheck size={18} /> : <IconCircleX size={18} />,
    title: ok ? 'Connection works' : 'Connection failed',
    message: t.result?.message ?? t.error ?? '',
  });
  return ok;
}

export function ConnectionsPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const types = useConnectionTypes();
  const conns = useQuery({ queryKey: ['connections'], queryFn: () => api<Connection[]>('/api/connections') });
  const [editing, setEditing] = useState<Connection | 'new' | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const label = (t: string) => types.data?.find((x) => x.type === t)?.label ?? t;
  const engineer = canEngineer(user?.roles);

  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/connections/${id}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['connections'] }),
    onError: (e) => notifications.show({ color: 'red', message: (e as Error).message }),
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconPlugConnected size={28} stroke={1.5} />
          <Title order={2}>Connections</Title>
        </Group>
        {engineer && <Button onClick={() => setEditing('new')}>New connection</Button>}
      </Group>
      <Text c="dimmed" maw={760}>
        Sources the platform reads from. Credentials go straight to Vault under the connection's service account; the
        platform keeps only references to them, and only jobs running as that account can read them.
      </Text>

      {conns.isLoading ? (
        <Loader />
      ) : (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Type</Table.Th>
              <Table.Th>Service account</Table.Th>
              <Table.Th>Last test</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {conns.data?.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={5}>
                  <Text c="dimmed">No connections yet.</Text>
                </Table.Td>
              </Table.Tr>
            )}
            {conns.data?.map((c) => (
              <Table.Tr key={c.id}>
                <Table.Td>
                  <Text fw={500}>{c.name}</Text>
                  <Text size="xs" c="dimmed">
                    {c.description}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge variant="light">{label(c.type)}</Badge>
                </Table.Td>
                <Table.Td>{c.service_account ?? '—'}</Table.Td>
                <Table.Td>
                  {c.last_test_at ? (
                    <Tooltip label={c.last_test_message ?? ''} multiline w={300}>
                      <Badge
                        color={c.last_test_ok ? 'green' : 'red'}
                        variant="light"
                        leftSection={c.last_test_ok ? <IconCircleCheck size={12} /> : <IconCircleX size={12} />}
                      >
                        {c.last_test_ok ? 'ok' : 'failed'} · {fmtTime(c.last_test_at)}
                      </Badge>
                    </Tooltip>
                  ) : (
                    <Text size="sm" c="dimmed">
                      never
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  {engineer && (
                    <Group gap="xs" justify="flex-end" wrap="nowrap">
                      <Button
                        size="xs"
                        variant="light"
                        loading={testing === c.id}
                        onClick={async () => {
                          setTesting(c.id);
                          try {
                            await testConnection(c.id);
                          } catch (e) {
                            notifications.show({ color: 'red', message: (e as Error).message });
                          } finally {
                            setTesting(null);
                            qc.invalidateQueries({ queryKey: ['connections'] });
                          }
                        }}
                      >
                        Test
                      </Button>
                      <ActionIcon variant="subtle" aria-label={`Edit ${c.name}`} onClick={() => setEditing(c)}>
                        <IconPencil size={16} />
                      </ActionIcon>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        aria-label={`Delete ${c.name}`}
                        onClick={() => window.confirm(`Delete connection ${c.name} and its stored secrets?`) && remove.mutate(c.id)}
                      >
                        <IconTrash size={16} />
                      </ActionIcon>
                    </Group>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {editing && <ConnectionModal connection={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </Stack>
  );
}

function ConnectionModal({ connection, onClose }: { connection: Connection | null; onClose: () => void }) {
  const qc = useQueryClient();
  const types = useConnectionTypes();
  const accounts = useQuery({
    queryKey: ['service-accounts'],
    queryFn: () => api<ServiceAccount[]>('/api/admin/service-accounts'),
  });
  const [type, setType] = useState<string | null>(connection?.type ?? null);
  const [name, setName] = useState(connection?.name ?? '');
  const [description, setDescription] = useState(connection?.description ?? '');
  const [account, setAccount] = useState<string | null>(connection?.service_account ?? null);
  const [config, setConfig] = useState<Record<string, unknown>>(() => {
    // Hide stored references from the form; they are kept server-side unless replaced.
    const strip = (o: Record<string, unknown>): Record<string, unknown> =>
      Object.fromEntries(
        Object.entries(o)
          .filter(([, v]) => !isRef(v))
          .map(([k, v]) => [k, v && typeof v === 'object' && !Array.isArray(v) ? strip(v as Record<string, unknown>) : v]),
      );
    return connection ? strip(connection.config) : {};
  });
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const ct = types.data?.find((t) => t.type === type);
  const stored = connection && ct ? storedSecretPaths(connection.config, ct.secret_fields) : new Set<string>();

  const save = useMutation({
    mutationFn: () =>
      connection
        ? api<Connection>(`/api/connections/${connection.id}`, { method: 'PUT', body: json({ description, config: mergeRefs(), secrets }) })
        : api<Connection>('/api/connections', {
            method: 'POST',
            body: json({ name, type, description, service_account: account, config, secrets }),
          }),
    onSuccess: async (c) => {
      setSecrets({});
      qc.invalidateQueries({ queryKey: ['connections'] });
      onClose();
      await testConnection(c.id).catch(() => undefined);
      qc.invalidateQueries({ queryKey: ['connections'] });
    },
  });

  // Put stored references back for fields whose secret wasn't re-entered.
  function mergeRefs(): Record<string, unknown> {
    const out = structuredClone(config);
    if (!connection || !ct) return out;
    for (const f of ct.secret_fields) {
      if (secrets[f]) continue;
      let src: unknown = connection.config;
      for (const p of f.split('.')) src = src && typeof src === 'object' ? (src as Record<string, unknown>)[p] : undefined;
      if (!isRef(src)) continue;
      const parts = f.split('.');
      let dst = out as Record<string, unknown>;
      for (const p of parts.slice(0, -1)) dst = (dst[p] ??= {}) as Record<string, unknown>;
      dst[parts.at(-1)!] = src;
    }
    return out;
  }

  const needsAccount = Object.keys(secrets).length > 0 && !account;

  return (
    <Modal opened onClose={onClose} title={connection ? `Edit ${connection.name}` : 'New connection'} size="lg">
      <Stack>
        {save.isError && (
          <Alert color="red" icon={<IconCircleX size={16} />}>
            {(save.error as Error).message}
          </Alert>
        )}
        {!connection && (
          <Select
            label="Type"
            required
            searchable
            data={(types.data ?? []).map((t) => ({ value: t.type, label: `${t.label}` }))}
            value={type}
            onChange={(v) => {
              setType(v);
              setConfig({});
              setSecrets({});
            }}
          />
        )}
        {!connection && <TextInput label="Name" required value={name} onChange={(e) => setName(e.currentTarget.value)} />}
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        {ct && ct.secret_fields.length > 0 && !connection && (
          <Select
            label="Service account"
            description="Owns this connection's secrets in Vault; jobs using the connection run as it"
            data={(accounts.data ?? []).map((a) => a.name)}
            value={account}
            onChange={setAccount}
            clearable
            error={needsAccount ? 'Connections with secrets need a service account' : undefined}
          />
        )}
        {ct && (
          <SchemaForm
            schema={ct.config_schema}
            value={config}
            onChange={setConfig}
            secrets={secrets}
            onSecretsChange={setSecrets}
            storedSecrets={stored}
          />
        )}
        {ct && ct.secret_fields.length > 0 && (
          <Alert icon={<IconLock size={16} />} variant="light">
            {connection && stored.size > 0
              ? 'To change a secret, re-enter all of this connection’s secrets (Vault replaces them together).'
              : 'Secret values are written to Vault and never shown again.'}
          </Alert>
        )}
        <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!type || (!connection && !name) || needsAccount}>
          Save and test
        </Button>
      </Stack>
    </Modal>
  );
}
