import { useState } from 'react';
import { Alert, Badge, Code, Group, Stack, Table, Text, TextInput } from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import { fmtTime } from '../../api/tasks';

interface VaultAuditEntry {
  time: string;
  who: string;
  policies: string[];
  operation: string | null;
  path: string | null;
  remote_address: string | null;
  error: string | null;
}

export function VaultAuditPage() {
  const [path, setPath] = useState('');
  const [debounced] = useDebouncedValue(path, 300);
  const q = useQuery({
    queryKey: ['vault-audit', debounced],
    queryFn: () =>
      api<{ available: boolean; entries: VaultAuditEntry[] }>(
        `/api/admin/vault-audit?limit=300${debounced ? `&path=${encodeURIComponent(debounced)}` : ''}`,
      ),
    refetchInterval: 15_000,
  });
  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed">Vault’s own audit log: which service or job token read or wrote which secret. Values are never shown.</Text>
        <TextInput placeholder="Filter by path, e.g. service-accounts" value={path} onChange={(e) => setPath(e.currentTarget.value)} w={300} />
      </Group>
      {q.data && !q.data.available && <Alert color="gray">The Vault audit log isn’t mounted into the API container.</Alert>}
      <Table striped withTableBorder fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Time</Table.Th>
            <Table.Th>Who</Table.Th>
            <Table.Th>Operation</Table.Th>
            <Table.Th>Path</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {q.data?.entries.map((e, i) => (
            <Table.Tr key={`${e.time}${i}`}>
              <Table.Td>{fmtTime(e.time)}</Table.Td>
              <Table.Td>
                <Text size="sm">{e.who}</Text>
                <Text size="xs" c="dimmed">{e.policies.filter((p) => p !== 'default').join(', ')}</Text>
              </Table.Td>
              <Table.Td>
                <Badge size="xs" variant="light" color={e.error ? 'red' : 'gray'}>
                  {e.operation}
                  {e.error ? ' · denied/error' : ''}
                </Badge>
              </Table.Td>
              <Table.Td>
                <Code>{e.path}</Code>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
