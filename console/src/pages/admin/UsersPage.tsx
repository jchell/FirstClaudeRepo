import { useState } from 'react';
import {
  Badge,
  Button,
  Group,
  Modal,
  MultiSelect,
  PasswordInput,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, json } from '../../api/client';
import type { AdminUser, Role } from '../../api/types';

function useRoles() {
  return useQuery({ queryKey: ['roles'], queryFn: () => api<Role[]>('/api/admin/roles') });
}

export function UsersPage() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<AdminUser[]>('/api/admin/users') });
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminUser | null>(null);

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api<AdminUser>(`/api/admin/users/${id}`, { method: 'PATCH', body: json(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
    onError: (e) => notifications.show({ color: 'red', message: String((e as Error).message) }),
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Text c="dimmed">Console users and their roles. Roles can also come from groups.</Text>
        <Button onClick={() => setCreating(true)}>New user</Button>
      </Group>
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Username</Table.Th>
            <Table.Th>Name</Table.Th>
            <Table.Th>Roles</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Last login</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {users.data?.map((u) => (
            <Table.Tr key={u.id}>
              <Table.Td>{u.username}</Table.Td>
              <Table.Td>{u.display_name}</Table.Td>
              <Table.Td>
                <Group gap={4}>
                  {u.roles.map((r) => (
                    <Badge key={r} variant="light">
                      {r}
                    </Badge>
                  ))}
                </Group>
              </Table.Td>
              <Table.Td>
                {!u.is_active ? (
                  <Badge color="gray">inactive</Badge>
                ) : u.locked ? (
                  <Badge color="orange">locked</Badge>
                ) : (
                  <Badge color="green">active</Badge>
                )}
              </Table.Td>
              <Table.Td>{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : '—'}</Table.Td>
              <Table.Td>
                <Group gap="xs" justify="flex-end">
                  {u.locked && (
                    <Button size="xs" variant="light" onClick={() => update.mutate({ id: u.id, body: { unlock: true } })}>
                      Unlock
                    </Button>
                  )}
                  <Button size="xs" variant="default" onClick={() => setEditing(u)}>
                    Edit
                  </Button>
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <CreateUserModal opened={creating} onClose={() => setCreating(false)} />
      {editing && <EditUserModal user={editing} onClose={() => setEditing(null)} />}
    </Stack>
  );
}

function CreateUserModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const roles = useRoles();
  const [form, setForm] = useState({ username: '', display_name: '', email: '', password: '', roles: ['viewer'] });
  const create = useMutation({
    mutationFn: () =>
      api<AdminUser>('/api/admin/users', {
        method: 'POST',
        body: json({ ...form, email: form.email || null, display_name: form.display_name || null }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] });
      setForm({ username: '', display_name: '', email: '', password: '', roles: ['viewer'] });
      onClose();
    },
  });

  return (
    <Modal opened={opened} onClose={onClose} title="New user">
      <Stack>
        {create.isError && <Text c="red">{(create.error as Error).message}</Text>}
        <TextInput label="Username" required value={form.username} onChange={(e) => setForm({ ...form, username: e.currentTarget.value })} />
        <TextInput label="Display name" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.currentTarget.value })} />
        <TextInput label="Email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.currentTarget.value })} />
        <PasswordInput
          label="Initial password"
          description="At least 12 characters"
          required
          autoComplete="new-password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.currentTarget.value })}
        />
        <MultiSelect label="Roles" data={roles.data?.map((r) => r.name) ?? []} value={form.roles} onChange={(v) => setForm({ ...form, roles: v })} />
        <Button onClick={() => create.mutate()} loading={create.isPending}>
          Create
        </Button>
      </Stack>
    </Modal>
  );
}

function EditUserModal({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const qc = useQueryClient();
  const roles = useRoles();
  const [selected, setSelected] = useState(user.roles);
  const [active, setActive] = useState(user.is_active);
  const [password, setPassword] = useState('');
  const save = useMutation({
    mutationFn: () =>
      api<AdminUser>(`/api/admin/users/${user.id}`, {
        method: 'PATCH',
        body: json({ roles: selected, is_active: active, ...(password ? { password } : {}) }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] });
      onClose();
    },
  });

  return (
    <Modal opened onClose={onClose} title={`Edit ${user.username}`}>
      <Stack>
        {save.isError && <Text c="red">{(save.error as Error).message}</Text>}
        <MultiSelect label="Direct roles" data={roles.data?.map((r) => r.name) ?? []} value={selected} onChange={setSelected} />
        <Switch label="Active" checked={active} onChange={(e) => setActive(e.currentTarget.checked)} />
        <PasswordInput
          label="Reset password"
          description="Leave empty to keep the current password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.currentTarget.value)}
        />
        <Text size="xs" c="dimmed">
          Changing roles, status or password signs the user out of existing sessions.
        </Text>
        <Button onClick={() => save.mutate()} loading={save.isPending}>
          Save
        </Button>
      </Stack>
    </Modal>
  );
}
