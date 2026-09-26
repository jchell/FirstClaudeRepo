import { useState } from 'react';
import { Badge, Button, Group, Modal, MultiSelect, Stack, Table, Text, TextInput } from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, json } from '../../api/client';
import type { AdminUser, Group as GroupT, Role } from '../../api/types';

export function GroupsPage() {
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api<GroupT[]>('/api/admin/groups') });
  const [editing, setEditing] = useState<GroupT | 'new' | null>(null);

  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed">Members of a group get all of the group's roles.</Text>
        <Button onClick={() => setEditing('new')}>New group</Button>
      </Group>
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Group</Table.Th>
            <Table.Th>Roles</Table.Th>
            <Table.Th>Members</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {groups.data?.map((g) => (
            <Table.Tr key={g.id}>
              <Table.Td>
                <Text fw={500}>{g.name}</Text>
                <Text size="xs" c="dimmed">
                  {g.description}
                </Text>
              </Table.Td>
              <Table.Td>
                <Group gap={4}>
                  {g.roles.map((r) => (
                    <Badge key={r} variant="light">
                      {r}
                    </Badge>
                  ))}
                </Group>
              </Table.Td>
              <Table.Td>{g.members.join(', ') || '—'}</Table.Td>
              <Table.Td>
                <Button size="xs" variant="default" onClick={() => setEditing(g)}>
                  Edit
                </Button>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {editing && <GroupModal group={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </Stack>
  );
}

function GroupModal({ group, onClose }: { group: GroupT | null; onClose: () => void }) {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => api<Role[]>('/api/admin/roles') });
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<AdminUser[]>('/api/admin/users') });
  const [form, setForm] = useState({
    name: group?.name ?? '',
    description: group?.description ?? '',
    roles: group?.roles ?? [],
    members: group?.members ?? [],
  });
  const save = useMutation({
    mutationFn: () =>
      group
        ? api(`/api/admin/groups/${group.id}`, { method: 'PUT', body: json(form) })
        : api('/api/admin/groups', { method: 'POST', body: json(form) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['groups'] });
      onClose();
    },
  });

  return (
    <Modal opened onClose={onClose} title={group ? `Edit ${group.name}` : 'New group'}>
      <Stack>
        {save.isError && <Text c="red">{(save.error as Error).message}</Text>}
        <TextInput label="Name" required value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} />
        <TextInput label="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.currentTarget.value })} />
        <MultiSelect label="Roles" data={roles.data?.map((r) => r.name) ?? []} value={form.roles} onChange={(v) => setForm({ ...form, roles: v })} />
        <MultiSelect
          label="Members"
          searchable
          data={users.data?.map((u) => u.username) ?? []}
          value={form.members}
          onChange={(v) => setForm({ ...form, members: v })}
        />
        <Button onClick={() => save.mutate()} loading={save.isPending}>
          Save
        </Button>
      </Stack>
    </Modal>
  );
}
