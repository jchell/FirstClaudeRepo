import { useState } from 'react';
import { Badge, Code, Group, Select, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type { AuditEntry } from '../../api/types';

const ACTIONS = [
  'auth.login',
  'user.create',
  'user.update',
  'group.create',
  'group.update',
  'service_account.create',
  'service_account.delete',
  'secret.write',
  'secret.delete',
  'job.submit',
  'setting.update',
];

export function AuditPage() {
  const [action, setAction] = useState<string | null>(null);
  const audit = useQuery({
    queryKey: ['audit', action],
    queryFn: () => api<AuditEntry[]>(`/api/admin/audit?limit=200${action ? `&action=${encodeURIComponent(action)}` : ''}`),
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed">Who did what, when. Secret values are never recorded.</Text>
        <Select placeholder="All actions" clearable data={ACTIONS} value={action} onChange={setAction} w={240} />
      </Group>
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Time</Table.Th>
            <Table.Th>Actor</Table.Th>
            <Table.Th>Action</Table.Th>
            <Table.Th>Target</Table.Th>
            <Table.Th>Outcome</Table.Th>
            <Table.Th>Details</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {audit.data?.map((e) => (
            <Table.Tr key={e.id}>
              <Table.Td>{new Date(e.ts).toLocaleString()}</Table.Td>
              <Table.Td>{e.actor}</Table.Td>
              <Table.Td>{e.action}</Table.Td>
              <Table.Td>{e.target}</Table.Td>
              <Table.Td>
                <Badge color={e.outcome === 'success' ? 'green' : 'red'} variant="light">
                  {e.outcome}
                </Badge>
              </Table.Td>
              <Table.Td>{Object.keys(e.detail).length > 0 && <Code>{JSON.stringify(e.detail)}</Code>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
