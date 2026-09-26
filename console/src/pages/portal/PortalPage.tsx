import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Modal,
  Select,
  SimpleGrid,
  Stack,
  TagsInput,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { IconApps, IconExternalLink, IconPencil, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import type { Dataset, PortalApp } from '../../api/types';

export function PortalPage() {
  const { user } = useAuth();
  const apps = useQuery({ queryKey: ['portal'], queryFn: () => api<PortalApp[]>('/api/portal/apps') });
  const [editing, setEditing] = useState<PortalApp | 'new' | null>(null);
  const qc = useQueryClient();
  const canEdit = ['admin', 'engineer', 'analyst'].some((r) => user?.roles.includes(r));
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/portal/apps/${id}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['portal'] }),
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconApps size={28} stroke={1.5} />
          <Title order={2}>App Portal</Title>
        </Group>
        {canEdit && <Button onClick={() => setEditing('new')}>Add app</Button>}
      </Group>
      <Text c="dimmed">Reports, dashboards and business apps built on the platform. Listing the datasets an app uses adds it to lineage.</Text>
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
        {apps.data?.map((a) => (
          <Card key={a.id} withBorder padding="md">
            <Group justify="space-between" wrap="nowrap">
              <Text fw={600}>{a.name}</Text>
              <Badge variant="light">{a.category}</Badge>
            </Group>
            <Text size="sm" c="dimmed" mt={4} lineClamp={3}>
              {a.description}
            </Text>
            {a.datasets.length > 0 && (
              <Group gap={4} mt="xs">
                {a.datasets.map((d) => (
                  <Badge key={d} size="xs" variant="outline">
                    {d}
                  </Badge>
                ))}
              </Group>
            )}
            <Group justify="space-between" mt="md">
              <Anchor href={a.url} target="_blank" rel="noreferrer noopener">
                <Group gap={4}>
                  Open <IconExternalLink size={14} />
                </Group>
              </Anchor>
              <Group gap={4}>
                <Anchor component={Link} to={`/lineage?node=${encodeURIComponent(`app:${a.name}`)}`} size="sm">
                  Lineage
                </Anchor>
                {canEdit && (
                  <>
                    <ActionIcon variant="subtle" aria-label={`Edit ${a.name}`} onClick={() => setEditing(a)}>
                      <IconPencil size={16} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label={`Remove ${a.name}`}
                      onClick={() => window.confirm(`Remove ${a.name} from the portal?`) && remove.mutate(a.id)}
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </>
                )}
              </Group>
            </Group>
          </Card>
        ))}
      </SimpleGrid>
      {apps.data?.length === 0 && <Text c="dimmed">No apps listed yet.</Text>}
      {editing && <AppModal app={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </Stack>
  );
}

function AppModal({ app, onClose }: { app: PortalApp | null; onClose: () => void }) {
  const qc = useQueryClient();
  const datasets = useQuery({ queryKey: ['catalog', '', 'all'], queryFn: () => api<Dataset[]>('/api/catalog/datasets') });
  const [form, setForm] = useState({
    name: app?.name ?? '',
    url: app?.url ?? 'https://',
    description: app?.description ?? '',
    category: app?.category ?? 'report',
    owner: app?.owner ?? '',
    datasets: app?.datasets ?? [],
  });
  const save = useMutation({
    mutationFn: () =>
      app
        ? api(`/api/portal/apps/${app.id}`, { method: 'PUT', body: json({ ...form, owner: form.owner || null }) })
        : api('/api/portal/apps', { method: 'POST', body: json({ ...form, owner: form.owner || null }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['portal'] });
      onClose();
    },
  });
  return (
    <Modal opened onClose={onClose} title={app ? `Edit ${app.name}` : 'Add app'}>
      <Stack>
        {save.isError && <Alert color="red">{(save.error as Error).message}</Alert>}
        <TextInput label="Name" required value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} />
        <TextInput label="URL" required value={form.url} onChange={(e) => setForm({ ...form, url: e.currentTarget.value })} />
        <Select
          label="Kind"
          data={['report', 'dashboard', 'app', 'notebook']}
          value={form.category}
          onChange={(v) => setForm({ ...form, category: (v ?? 'report') as PortalApp['category'] })}
        />
        <TextInput label="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.currentTarget.value })} />
        <TextInput label="Owner" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.currentTarget.value })} />
        <TagsInput
          label="Datasets it reads"
          description="layer.name, e.g. gold.dim_customer"
          data={(datasets.data ?? []).map((d) => `${d.layer}.${d.name}`)}
          value={form.datasets}
          onChange={(v) => setForm({ ...form, datasets: v })}
        />
        <Button onClick={() => save.mutate()} loading={save.isPending}>
          Save
        </Button>
      </Stack>
    </Modal>
  );
}
