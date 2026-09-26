import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Button,
  Code,
  Group,
  Modal,
  PasswordInput,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconKey, IconLock, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, json } from '../../api/client';
import type { SecretMetadata, ServiceAccount } from '../../api/types';

export function ServiceAccountsPage() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ['service-accounts'], queryFn: () => api<ServiceAccount[]>('/api/admin/service-accounts') });
  const [creating, setCreating] = useState(false);
  const [secretsFor, setSecretsFor] = useState<ServiceAccount | null>(null);

  const remove = useMutation({
    mutationFn: (name: string) => api(`/api/admin/service-accounts/${name}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['service-accounts'] }),
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed" maw={640}>
          Jobs run as a service account. Each one has its own Vault path and least-privilege policy, and only jobs running
          as that account can read its secrets.
        </Text>
        <Button onClick={() => setCreating(true)}>New service account</Button>
      </Group>
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th>Vault path</Table.Th>
            <Table.Th>Policy</Table.Th>
            <Table.Th>Created by</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {accounts.data?.map((a) => (
            <Table.Tr key={a.id}>
              <Table.Td>
                <Text fw={500}>{a.name}</Text>
                <Text size="xs" c="dimmed">
                  {a.description}
                </Text>
              </Table.Td>
              <Table.Td>
                <Code>{a.vault_path}</Code>
              </Table.Td>
              <Table.Td>
                <Code>{a.vault_policy}</Code>
              </Table.Td>
              <Table.Td>{a.created_by}</Table.Td>
              <Table.Td>
                <Group gap="xs" justify="flex-end" wrap="nowrap">
                  <Button size="xs" variant="light" leftSection={<IconKey size={14} />} onClick={() => setSecretsFor(a)}>
                    Secrets
                  </Button>
                  <Tooltip label="Delete account, policy and secrets">
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label={`Delete ${a.name}`}
                      onClick={() => {
                        if (window.confirm(`Delete service account ${a.name} and all of its secrets?`)) remove.mutate(a.name);
                      }}
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Tooltip>
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <CreateModal opened={creating} onClose={() => setCreating(false)} />
      {secretsFor && <SecretsModal account={secretsFor} onClose={() => setSecretsFor(null)} />}
    </Stack>
  );
}

function CreateModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const create = useMutation({
    mutationFn: () => api('/api/admin/service-accounts', { method: 'POST', body: json({ name, description }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['service-accounts'] });
      setName('');
      setDescription('');
      onClose();
    },
  });
  return (
    <Modal opened={opened} onClose={onClose} title="New service account">
      <Stack>
        {create.isError && <Text c="red">{(create.error as Error).message}</Text>}
        <TextInput
          label="Name"
          description="Lowercase letters, digits and dashes, e.g. etl-sftp"
          required
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
        />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        <Button onClick={() => create.mutate()} loading={create.isPending}>
          Create
        </Button>
      </Stack>
    </Modal>
  );
}

function SecretsModal({ account, onClose }: { account: ServiceAccount; onClose: () => void }) {
  const qc = useQueryClient();
  const key = ['sa-secrets', account.name];
  const secrets = useQuery({ queryKey: key, queryFn: () => api<SecretMetadata[]>(`/api/admin/service-accounts/${account.name}/secrets`) });
  const [secretName, setSecretName] = useState('');
  const [field, setField] = useState('password');
  const [value, setValue] = useState('');
  const [lastRefs, setLastRefs] = useState<string[]>([]);

  const write = useMutation({
    mutationFn: () =>
      api<SecretMetadata>(`/api/admin/service-accounts/${account.name}/secrets/${secretName}`, {
        method: 'PUT',
        body: json({ values: { [field]: value } }),
      }),
    onSuccess: (meta) => {
      setValue(''); // never keep the value around in the page
      setLastRefs(meta.refs);
      qc.invalidateQueries({ queryKey: key });
    },
  });

  return (
    <Modal opened onClose={onClose} title={`Secrets of ${account.name}`} size="lg">
      <Stack>
        <Alert icon={<IconLock size={16} />} color="blue" variant="light">
          Values are write-only: they go straight to Vault and are never shown again. Writing an existing secret rotates it
          (a new version); jobs pick it up on their next run.
        </Alert>
        <Table withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Path</Table.Th>
              <Table.Th>Version</Table.Th>
              <Table.Th>Last rotated</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {secrets.data?.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={3}>
                  <Text c="dimmed">No secrets yet.</Text>
                </Table.Td>
              </Table.Tr>
            )}
            {secrets.data?.map((s) => (
              <Table.Tr key={s.path}>
                <Table.Td>
                  <Code>{s.path}</Code>
                </Table.Td>
                <Table.Td>v{s.current_version}</Table.Td>
                <Table.Td>{new Date(s.updated_time).toLocaleString()}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        <Group grow align="flex-end">
          <TextInput label="Secret" placeholder="sftp" value={secretName} onChange={(e) => setSecretName(e.currentTarget.value)} />
          <TextInput label="Field" value={field} onChange={(e) => setField(e.currentTarget.value)} />
          <PasswordInput label="Value" autoComplete="new-password" value={value} onChange={(e) => setValue(e.currentTarget.value)} />
        </Group>
        {write.isError && <Text c="red">{(write.error as Error).message}</Text>}
        <Button onClick={() => write.mutate()} loading={write.isPending} disabled={!secretName || !field || !value}>
          Save to Vault
        </Button>
        {lastRefs.length > 0 && (
          <Text size="sm">
            Saved. Reference it from connections as <Code>{lastRefs[0]}</Code>
          </Text>
        )}
      </Stack>
    </Modal>
  );
}
