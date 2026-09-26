import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  MultiSelect,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Table,
  Tabs,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconCheck, IconEyeOff, IconLock, IconPlus, IconScan, IconShieldLock, IconTrash, IconX } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import type {
  AccessGrantInfo,
  Dataset,
  GlossaryTermInfo,
  MaskingPolicyInfo,
  RowFilterInfo,
  TagAssignmentInfo,
  TagInfo,
} from '../../api/types';

const isSteward = (roles: string[] | undefined) => !!roles && (roles.includes('admin') || roles.includes('steward'));

function useRoles() {
  return useQuery({
    queryKey: ['roles'],
    queryFn: () => api<{ name: string }[]>('/api/governance/roles'),
  });
}

function useDatasets() {
  return useQuery({ queryKey: ['catalog', 'all'], queryFn: () => api<Dataset[]>('/api/catalog/datasets') });
}

async function run<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (ok) notifications.show({ message: ok });
    return r;
  } catch (e) {
    notifications.show({ color: 'red', message: (e as Error).message });
    return undefined;
  }
}

// ---------------------------------------------------------------- classification review

function ReviewTab({ steward }: { steward: boolean }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState<'suggested' | 'active' | 'rejected'>('suggested');
  const q = useQuery({
    queryKey: ['gov', 'assignments', status],
    queryFn: () => api<TagAssignmentInfo[]>(`/api/governance/assignments?status=${status}`),
    refetchInterval: 10_000,
  });
  const review = async (a: TagAssignmentInfo, s: 'active' | 'rejected') => {
    await run(() => api(`/api/governance/assignments/${a.id}`, { method: 'PATCH', body: json({ status: s }) }));
    qc.invalidateQueries({ queryKey: ['gov'] });
  };
  return (
    <Stack>
      <Group justify="space-between">
        <SegmentedControl
          value={status}
          onChange={(v) => setStatus(v as typeof status)}
          data={[
            { label: 'Suggested', value: 'suggested' },
            { label: 'Accepted', value: 'active' },
            { label: 'Rejected', value: 'rejected' },
          ]}
        />
        {steward && (
          <Button
            variant="default"
            leftSection={<IconScan size={16} />}
            onClick={() => run(() => api('/api/governance/classify', { method: 'POST' }), 'Scan queued; suggestions appear here')}
          >
            Scan for PII
          </Button>
        )}
      </Group>
      <Text size="sm" c="dimmed" maw={820}>
        Suggestions come from column names, value patterns in the latest profile (e.g. most values look like email
        addresses), and lineage (a column computed from a classified column). Accepted classifications drive masking
        policies.
      </Text>
      {q.isLoading ? (
        <Loader />
      ) : !q.data?.length ? (
        <Text c="dimmed" size="sm">Nothing here.</Text>
      ) : (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Column</Table.Th>
              <Table.Th>Classification</Table.Th>
              <Table.Th>Why</Table.Th>
              <Table.Th>Confidence</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data.map((a) => (
              <Table.Tr key={a.id} data-testid={`suggestion-${a.dataset}.${a.column}`}>
                <Table.Td>
                  <Anchor component={Link} to={`/catalog/${a.dataset_id}`} ff="monospace" size="sm">
                    {a.dataset}
                    {a.column ? `.${a.column}` : ''}
                  </Anchor>
                </Table.Td>
                <Table.Td>
                  <Badge variant="light" color="grape">{a.tag}</Badge>
                </Table.Td>
                <Table.Td>
                  <Text size="xs">{a.reason || a.source}</Text>
                  <Text size="10px" c="dimmed">{a.source}</Text>
                </Table.Td>
                <Table.Td>{a.confidence == null ? '—' : `${Math.round(a.confidence * 100)}%`}</Table.Td>
                <Table.Td>
                  {steward && (
                    <Group gap={4} wrap="nowrap">
                      {a.status !== 'active' && (
                        <Button size="compact-xs" variant="light" color="green" leftSection={<IconCheck size={12} />} onClick={() => review(a, 'active')} aria-label={`Accept ${a.tag} on ${a.column}`}>
                          Accept
                        </Button>
                      )}
                      {a.status !== 'rejected' && (
                        <Button size="compact-xs" variant="subtle" color="gray" leftSection={<IconX size={12} />} onClick={() => review(a, 'rejected')} aria-label={`Reject ${a.tag} on ${a.column}`}>
                          Reject
                        </Button>
                      )}
                    </Group>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------- tags

function TagsTab({ steward }: { steward: boolean }) {
  const qc = useQueryClient();
  const tags = useQuery({ queryKey: ['gov', 'tags'], queryFn: () => api<TagInfo[]>('/api/governance/tags') });
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  return (
    <Stack>
      {steward && (
        <Group align="end">
          <TextInput label="New tag" placeholder="e.g. finance or confidential" value={name} onChange={(e) => setName(e.currentTarget.value)} />
          <TextInput label="Description" value={desc} onChange={(e) => setDesc(e.currentTarget.value)} w={320} />
          <Button
            disabled={!name}
            onClick={async () => {
              await run(() => api('/api/governance/tags', { method: 'POST', body: json({ name, description: desc }) }));
              setName('');
              setDesc('');
              qc.invalidateQueries({ queryKey: ['gov', 'tags'] });
            }}
          >
            Add
          </Button>
        </Group>
      )}
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Tag</Table.Th>
            <Table.Th>Kind</Table.Th>
            <Table.Th>Description</Table.Th>
            <Table.Th>Columns tagged</Table.Th>
            <Table.Th>Masking</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {tags.data?.map((t) => (
            <Table.Tr key={t.name}>
              <Table.Td ff="monospace">{t.name}</Table.Td>
              <Table.Td>{t.category}</Table.Td>
              <Table.Td>{t.description}</Table.Td>
              <Table.Td>{t.uses}</Table.Td>
              <Table.Td>
                {t.masking_policy ? (
                  <Badge variant="light" leftSection={<IconEyeOff size={11} aria-hidden />}>{t.masking_policy}</Badge>
                ) : (
                  '—'
                )}
              </Table.Td>
              <Table.Td>
                {steward && !t.builtin && (
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label={`Delete ${t.name}`}
                    onClick={async () => {
                      await run(() => api(`/api/governance/tags/${t.name}`, { method: 'DELETE' }));
                      qc.invalidateQueries({ queryKey: ['gov', 'tags'] });
                    }}
                  >
                    <IconTrash size={16} />
                  </ActionIcon>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

// ---------------------------------------------------------------- glossary

function TermForm({ edit, onClose }: { edit: GlossaryTermInfo | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(edit?.name ?? '');
  const [definition, setDefinition] = useState(edit?.definition ?? '');
  const [synonyms, setSynonyms] = useState<string[]>(edit?.synonyms ?? []);
  const [domain, setDomain] = useState(edit?.domain ?? '');
  const [status, setStatus] = useState<string>(edit?.status ?? 'draft');
  return (
    <Modal opened onClose={onClose} title={edit ? edit.name : 'New glossary term'} size="lg">
      <Stack>
        <TextInput label="Term" value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <Textarea label="Definition" autosize minRows={3} value={definition} onChange={(e) => setDefinition(e.currentTarget.value)} />
        <TagsInput label="Synonyms" value={synonyms} onChange={setSynonyms} />
        <Group grow>
          <TextInput label="Domain" value={domain} onChange={(e) => setDomain(e.currentTarget.value)} />
          <Select label="Status" data={['draft', 'approved', 'deprecated']} value={status} onChange={(v) => setStatus(v ?? 'draft')} />
        </Group>
        <Group justify="flex-end">
          <Button
            disabled={!name}
            onClick={async () => {
              const ok = await run(() =>
                api(edit ? `/api/governance/glossary/${edit.id}` : '/api/governance/glossary', {
                  method: edit ? 'PUT' : 'POST',
                  body: json({ name, definition, synonyms, domain: domain || null, status }),
                }),
              );
              if (ok !== undefined) {
                qc.invalidateQueries({ queryKey: ['gov', 'glossary'] });
                onClose();
              }
            }}
          >
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function GlossaryTab({ steward }: { steward: boolean }) {
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const terms = useQuery({ queryKey: ['gov', 'glossary', q], queryFn: () => api<GlossaryTermInfo[]>(`/api/governance/glossary?q=${encodeURIComponent(q)}`) });
  const [form, setForm] = useState<{ edit: GlossaryTermInfo | null } | null>(null);
  return (
    <Stack>
      <Group>
        <TextInput placeholder="Search terms" value={q} onChange={(e) => setQ(e.currentTarget.value)} w={320} />
        {steward && (
          <Button leftSection={<IconPlus size={16} />} onClick={() => setForm({ edit: null })}>
            New term
          </Button>
        )}
      </Group>
      {!terms.data?.length ? (
        <Text c="dimmed" size="sm">No terms yet.</Text>
      ) : (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Term</Table.Th>
              <Table.Th>Definition</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Used by</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {terms.data.map((t) => (
              <Table.Tr key={t.id}>
                <Table.Td>
                  {steward ? <Anchor onClick={() => setForm({ edit: t })}>{t.name}</Anchor> : t.name}
                  {!!t.synonyms.length && <Text size="xs" c="dimmed">also: {t.synonyms.join(', ')}</Text>}
                </Table.Td>
                <Table.Td maw={420}>
                  <Text size="sm">{t.definition}</Text>
                </Table.Td>
                <Table.Td>
                  <Badge variant="light" color={t.status === 'approved' ? 'green' : t.status === 'deprecated' ? 'gray' : 'blue'}>{t.status}</Badge>
                </Table.Td>
                <Table.Td ff="monospace" fz="xs">
                  {t.links.map((l) => `${l.dataset}${l.column ? `.${l.column}` : ''}`).join(', ') || '—'}
                </Table.Td>
                <Table.Td>
                  {steward && (
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label={`Delete ${t.name}`}
                      onClick={async () => {
                        if (!window.confirm(`Delete the term ${t.name}?`)) return;
                        await run(() => api(`/api/governance/glossary/${t.id}`, { method: 'DELETE' }));
                        qc.invalidateQueries({ queryKey: ['gov', 'glossary'] });
                      }}
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {form && <TermForm edit={form.edit} onClose={() => setForm(null)} />}
    </Stack>
  );
}

// ---------------------------------------------------------------- policies

const METHODS = [
  { value: 'redact', label: 'Redact (****)' },
  { value: 'partial', label: 'Partial (keep last 4)' },
  { value: 'hash', label: 'Keyed hash (joinable, not reversible)' },
  { value: 'null', label: 'Null' },
];

function PoliciesTab() {
  const qc = useQueryClient();
  const roles = useRoles();
  const datasets = useDatasets();
  const tags = useQuery({ queryKey: ['gov', 'tags'], queryFn: () => api<TagInfo[]>('/api/governance/tags') });
  const masking = useQuery({ queryKey: ['gov', 'masking'], queryFn: () => api<MaskingPolicyInfo[]>('/api/governance/masking') });
  const filters = useQuery({ queryKey: ['gov', 'filters'], queryFn: () => api<RowFilterInfo[]>('/api/governance/row-filters') });
  const roleNames = (roles.data ?? []).map((r) => r.name);
  const [m, setM] = useState({ name: '', tag: '', method: 'redact', exempt_roles: ['steward'] as string[] });
  const [f, setF] = useState({ name: '', dataset_id: '', predicate: '', exempt_roles: ['steward'] as string[] });
  const [check, setCheck] = useState<{ dataset_id: string | null; role: string | null }>({ dataset_id: null, role: null });
  const eff = useQuery({
    queryKey: ['gov', 'effective', check],
    queryFn: () => api<{ readable: boolean; restricted: boolean; masks: Record<string, string>; row_filters: string[] }>(
      `/api/governance/effective?dataset_id=${check.dataset_id}&role=${check.role}`,
    ),
    enabled: !!check.dataset_id && !!check.role,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ['gov'] });

  return (
    <Stack>
      <Title order={4}>Column masking</Title>
      <Text size="sm" c="dimmed" maw={820}>
        Every column carrying the tag (or a sub-tag: a policy on <Code>pii</Code> covers <Code>pii.email</Code>) is masked
        in previews, profiles, DQ samples, queries and model previews for everyone except the exempt roles.
      </Text>
      <Table withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Policy</Table.Th>
            <Table.Th>Tag</Table.Th>
            <Table.Th>Method</Table.Th>
            <Table.Th>Exempt roles</Table.Th>
            <Table.Th>Enabled</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {masking.data?.map((p) => (
            <Table.Tr key={p.id}>
              <Table.Td ff="monospace">{p.name}</Table.Td>
              <Table.Td ff="monospace">{p.tag}</Table.Td>
              <Table.Td>{METHODS.find((x) => x.value === p.method)?.label}</Table.Td>
              <Table.Td>{p.exempt_roles.join(', ') || 'nobody'}</Table.Td>
              <Table.Td>
                <Switch
                  checked={p.enabled}
                  aria-label={`Enable ${p.name}`}
                  onChange={async (e) => {
                    const body = { name: p.name, tag: p.tag, method: p.method, exempt_roles: p.exempt_roles, description: p.description };
                    await run(() => api(`/api/governance/masking/${p.id}`, { method: 'PUT', body: json({ ...body, enabled: e.currentTarget.checked }) }));
                    refresh();
                  }}
                />
              </Table.Td>
              <Table.Td>
                <ActionIcon variant="subtle" color="red" aria-label={`Delete ${p.name}`} onClick={async () => {
                  await run(() => api(`/api/governance/masking/${p.id}`, { method: 'DELETE' }));
                  refresh();
                }}>
                  <IconTrash size={16} />
                </ActionIcon>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Group align="end">
        <TextInput label="Name" value={m.name} onChange={(e) => setM({ ...m, name: e.currentTarget.value })} w={180} />
        <Select label="Tag" data={(tags.data ?? []).map((t) => t.name)} value={m.tag || null} onChange={(v) => setM({ ...m, tag: v ?? '' })} searchable w={180} />
        <Select label="Method" data={METHODS} value={m.method} onChange={(v) => setM({ ...m, method: v ?? 'redact' })} w={260} />
        <MultiSelect label="Exempt roles" data={roleNames} value={m.exempt_roles} onChange={(v) => setM({ ...m, exempt_roles: v })} w={220} />
        <Button
          disabled={!m.name || !m.tag}
          onClick={async () => {
            const ok = await run(() => api('/api/governance/masking', { method: 'POST', body: json(m) }), 'Masking policy added');
            if (ok !== undefined) {
              setM({ name: '', tag: '', method: 'redact', exempt_roles: ['steward'] });
              refresh();
            }
          }}
        >
          Add policy
        </Button>
      </Group>

      <Title order={4} mt="md">Row filters</Title>
      <Text size="sm" c="dimmed" maw={820}>
        Users without an exempt role see only the rows matching the condition (SQL over the dataset’s own columns).
      </Text>
      <Table withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Filter</Table.Th>
            <Table.Th>Dataset</Table.Th>
            <Table.Th>Rows visible when</Table.Th>
            <Table.Th>Exempt roles</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {filters.data?.map((x) => (
            <Table.Tr key={x.id}>
              <Table.Td ff="monospace">{x.name}</Table.Td>
              <Table.Td ff="monospace">{x.dataset}</Table.Td>
              <Table.Td>
                <Code>{x.predicate}</Code>
              </Table.Td>
              <Table.Td>{x.exempt_roles.join(', ') || 'nobody'}</Table.Td>
              <Table.Td>
                <ActionIcon variant="subtle" color="red" aria-label={`Delete ${x.name}`} onClick={async () => {
                  await run(() => api(`/api/governance/row-filters/${x.id}`, { method: 'DELETE' }));
                  refresh();
                }}>
                  <IconTrash size={16} />
                </ActionIcon>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Group align="end">
        <TextInput label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.currentTarget.value })} w={160} />
        <Select
          label="Dataset"
          data={(datasets.data ?? []).map((d) => ({ value: d.id, label: `${d.layer}.${d.name}` }))}
          value={f.dataset_id || null}
          onChange={(v) => setF({ ...f, dataset_id: v ?? '' })}
          searchable
          w={220}
        />
        <TextInput label="Condition" placeholder="country = 'NO'" ff="monospace" value={f.predicate} onChange={(e) => setF({ ...f, predicate: e.currentTarget.value })} w={260} />
        <MultiSelect label="Exempt roles" data={roleNames} value={f.exempt_roles} onChange={(v) => setF({ ...f, exempt_roles: v })} w={200} />
        <Button
          disabled={!f.name || !f.dataset_id || !f.predicate}
          onClick={async () => {
            const ok = await run(() => api('/api/governance/row-filters', { method: 'POST', body: json(f) }), 'Row filter added');
            if (ok !== undefined) {
              setF({ name: '', dataset_id: '', predicate: '', exempt_roles: ['steward'] });
              refresh();
            }
          }}
        >
          Add filter
        </Button>
      </Group>

      <Paper withBorder p="sm" mt="md">
        <Text fw={600} size="sm">Check what a role sees</Text>
        <Group align="end" mt="xs">
          <Select
            label="Dataset"
            data={(datasets.data ?? []).map((d) => ({ value: d.id, label: `${d.layer}.${d.name}` }))}
            value={check.dataset_id}
            onChange={(v) => setCheck({ ...check, dataset_id: v })}
            searchable
            w={260}
          />
          <Select label="Role" data={roleNames} value={check.role} onChange={(v) => setCheck({ ...check, role: v })} w={200} />
        </Group>
        {eff.data && (
          <Stack gap={4} mt="sm">
            <Text size="sm">
              {eff.data.readable ? 'Can read this dataset' : 'Cannot read this dataset'}
              {eff.data.restricted ? ' (restricted by grants)' : ''}
            </Text>
            <Text size="sm">
              Masked columns:{' '}
              {Object.keys(eff.data.masks).length
                ? Object.entries(eff.data.masks).map(([c, how]) => `${c} (${how})`).join(', ')
                : 'none'}
            </Text>
            <Text size="sm">Row filters: {eff.data.row_filters.length ? eff.data.row_filters.join(' AND ') : 'none'}</Text>
          </Stack>
        )}
      </Paper>
    </Stack>
  );
}

// ---------------------------------------------------------------- access grants

function GrantsTab() {
  const qc = useQueryClient();
  const roles = useRoles();
  const datasets = useDatasets();
  const grants = useQuery({ queryKey: ['gov', 'grants'], queryFn: () => api<AccessGrantInfo[]>('/api/governance/grants') });
  const [g, setG] = useState({ target_type: 'domain', target: '', principal_type: 'role', principal: '' });
  const domains = [...new Set((datasets.data ?? []).map((d) => d.domain).filter(Boolean) as string[])];
  return (
    <Stack>
      <Text size="sm" c="dimmed" maw={820}>
        A dataset with grants (on itself or its domain) is restricted: only the granted roles and users (plus admins and
        stewards) can read it. It disappears from the catalog for everyone else and shows as a placeholder in lineage.
        Datasets without grants are open to every signed-in user. Set a dataset’s domain on its catalog page.
      </Text>
      <Table withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Target</Table.Th>
            <Table.Th>Granted to</Table.Th>
            <Table.Th>By</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {grants.data?.map((x) => (
            <Table.Tr key={x.id}>
              <Table.Td>
                <Group gap={6}>
                  <IconLock size={14} aria-hidden />
                  <Text size="sm">{x.target_type}: <Code>{x.target_name}</Code></Text>
                </Group>
              </Table.Td>
              <Table.Td>{x.principal_type}: <Code>{x.principal}</Code></Table.Td>
              <Table.Td>{x.created_by}</Table.Td>
              <Table.Td>
                <ActionIcon variant="subtle" color="red" aria-label="Remove grant" onClick={async () => {
                  await run(() => api(`/api/governance/grants/${x.id}`, { method: 'DELETE' }));
                  qc.invalidateQueries({ queryKey: ['gov'] });
                  qc.invalidateQueries({ queryKey: ['catalog'] });
                }}>
                  <IconTrash size={16} />
                </ActionIcon>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Group align="end">
        <Select label="Target" data={[{ value: 'domain', label: 'Domain' }, { value: 'dataset', label: 'Dataset' }]} value={g.target_type} onChange={(v) => setG({ ...g, target_type: v ?? 'domain', target: '' })} w={130} />
        {g.target_type === 'domain' ? (
          <Select label="Domain" data={domains} value={g.target || null} onChange={(v) => setG({ ...g, target: v ?? '' })} searchable w={220} />
        ) : (
          <Select label="Dataset" data={(datasets.data ?? []).map((d) => ({ value: d.id, label: `${d.layer}.${d.name}` }))} value={g.target || null} onChange={(v) => setG({ ...g, target: v ?? '' })} searchable w={260} />
        )}
        <Select label="To" data={[{ value: 'role', label: 'Role' }, { value: 'user', label: 'User' }]} value={g.principal_type} onChange={(v) => setG({ ...g, principal_type: v ?? 'role', principal: '' })} w={110} />
        {g.principal_type === 'role' ? (
          <Select label="Role" data={(roles.data ?? []).map((r) => r.name)} value={g.principal || null} onChange={(v) => setG({ ...g, principal: v ?? '' })} w={200} />
        ) : (
          <TextInput label="Username" value={g.principal} onChange={(e) => setG({ ...g, principal: e.currentTarget.value })} w={200} />
        )}
        <Button
          disabled={!g.target || !g.principal}
          onClick={async () => {
            const ok = await run(() => api('/api/governance/grants', { method: 'POST', body: json(g) }), 'Access granted');
            if (ok !== undefined) {
              qc.invalidateQueries({ queryKey: ['gov'] });
              qc.invalidateQueries({ queryKey: ['catalog'] });
            }
          }}
        >
          Grant
        </Button>
      </Group>
    </Stack>
  );
}

// ---------------------------------------------------------------- page

export function GovernancePage() {
  const { user } = useAuth();
  const steward = isSteward(user?.roles);
  return (
    <Stack>
      <Group>
        <IconShieldLock size={28} stroke={1.5} />
        <Title order={2}>Governance</Title>
      </Group>
      <Text c="dimmed" maw={820}>
        Classify and tag data, keep the business glossary, and decide who may read what: access grants per dataset or
        domain, column masking driven by tags, and row filters. Every change is in the audit log.
      </Text>
      {!steward && <Alert variant="light">You can browse; stewards and admins make changes.</Alert>}
      <Tabs defaultValue="review" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="review">Classifications</Tabs.Tab>
          <Tabs.Tab value="tags">Tags</Tabs.Tab>
          <Tabs.Tab value="glossary">Glossary</Tabs.Tab>
          {steward && <Tabs.Tab value="policies">Masking & row filters</Tabs.Tab>}
          {steward && <Tabs.Tab value="grants">Access grants</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="review" pt="sm">
          <ReviewTab steward={steward} />
        </Tabs.Panel>
        <Tabs.Panel value="tags" pt="sm">
          <TagsTab steward={steward} />
        </Tabs.Panel>
        <Tabs.Panel value="glossary" pt="sm">
          <GlossaryTab steward={steward} />
        </Tabs.Panel>
        {steward && (
          <Tabs.Panel value="policies" pt="sm">
            <PoliciesTab />
          </Tabs.Panel>
        )}
        {steward && (
          <Tabs.Panel value="grants" pt="sm">
            <GrantsTab />
          </Tabs.Panel>
        )}
      </Tabs>
    </Stack>
  );
}
