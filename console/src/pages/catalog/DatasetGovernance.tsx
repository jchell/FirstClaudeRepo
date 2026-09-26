import { useState } from 'react';
import { ActionIcon, Alert, Badge, Button, Group, Popover, Select, Stack, Text, Textarea, Tooltip } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconCheck, IconEyeOff, IconPlayerPlay, IconPlus, IconX } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { api, json } from '../../api/client';
import { runTask } from '../../api/tasks';
import type { DatasetDetail, GlossaryTermInfo, QueryResult, Task, TagInfo } from '../../api/types';
import { PreviewTable } from '../ingestion/JobWizard';

type TagRow = DatasetDetail['tags'][number];

async function call(path: string, init: RequestInit) {
  try {
    return await api(path, init);
  } catch (e) {
    notifications.show({ color: 'red', message: (e as Error).message });
    return undefined;
  }
}

/** Tags on one column (or the dataset when column is null): active chips, suggestions to review. */
export function TagChips({
  d,
  column,
  steward,
}: {
  d: DatasetDetail;
  column: string | null;
  steward: boolean;
}) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ['dataset', d.id] });
  const rows = d.tags.filter((t) => t.column === column);
  const masked = column ? d.masked_columns[column] : undefined;
  const review = async (t: TagRow, status: 'active' | 'rejected') => {
    await call(`/api/governance/assignments/${t.id}`, { method: 'PATCH', body: json({ status }) });
    refresh();
  };
  return (
    <Group gap={4}>
      {masked && (
        <Tooltip label={`Masked for you (${masked})`}>
          <Badge size="xs" variant="outline" color="gray" leftSection={<IconEyeOff size={10} aria-hidden />}>
            masked
          </Badge>
        </Tooltip>
      )}
      {rows.map((t) =>
        t.status === 'active' ? (
          <Badge key={t.id} size="xs" variant="light" color="grape">
            {t.tag}
          </Badge>
        ) : (
          <Tooltip key={t.id} label={`Suggested: ${t.reason}`} multiline w={260}>
            <Badge
              size="xs"
              variant="outline"
              color="grape"
              style={{ borderStyle: 'dashed' }}
              rightSection={
                steward ? (
                  <Group gap={0} wrap="nowrap">
                    <ActionIcon size={14} variant="transparent" color="green" aria-label={`Accept ${t.tag} on ${column}`} onClick={() => review(t, 'active')}>
                      <IconCheck size={10} />
                    </ActionIcon>
                    <ActionIcon size={14} variant="transparent" color="gray" aria-label={`Reject ${t.tag} on ${column}`} onClick={() => review(t, 'rejected')}>
                      <IconX size={10} />
                    </ActionIcon>
                  </Group>
                ) : undefined
              }
            >
              {t.tag}?
            </Badge>
          </Tooltip>
        ),
      )}
      {steward && <AddTag d={d} column={column} onDone={refresh} />}
    </Group>
  );
}

function AddTag({ d, column, onDone }: { d: DatasetDetail; column: string | null; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const tags = useQuery({ queryKey: ['gov', 'tags'], queryFn: () => api<TagInfo[]>('/api/governance/tags'), enabled: open });
  return (
    <Popover opened={open} onChange={setOpen} position="bottom-start" withinPortal>
      <Popover.Target>
        <ActionIcon size="xs" variant="subtle" aria-label={`Tag ${column ?? 'dataset'}`} onClick={() => setOpen((o) => !o)}>
          <IconPlus size={12} />
        </ActionIcon>
      </Popover.Target>
      <Popover.Dropdown>
        <Select
          size="xs"
          placeholder="Choose a tag"
          data={(tags.data ?? []).map((t) => t.name)}
          searchable
          onChange={async (tag) => {
            if (!tag) return;
            await call('/api/governance/assignments', { method: 'POST', body: json({ tag, dataset_id: d.id, column }) });
            setOpen(false);
            onDone();
          }}
          comboboxProps={{ withinPortal: false }}
        />
      </Popover.Dropdown>
    </Popover>
  );
}

/** Glossary terms linked to the dataset, and linking another. */
export function GlossaryLinks({ d, canLink }: { d: DatasetDetail; canLink: boolean }) {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const terms = useQuery({ queryKey: ['gov', 'glossary', ''], queryFn: () => api<GlossaryTermInfo[]>('/api/governance/glossary'), enabled: adding });
  return (
    <Group gap={6}>
      <Text size="xs" c="dimmed">Glossary:</Text>
      {d.glossary.length === 0 && !adding && <Text size="xs" c="dimmed">none</Text>}
      {d.glossary.map((g) => (
        <Badge key={`${g.term_id}${g.column}`} variant="light" color="teal" size="sm">
          {g.name}
          {g.column ? ` (${g.column})` : ''}
        </Badge>
      ))}
      {canLink &&
        (adding ? (
          <Select
            size="xs"
            placeholder="Term"
            data={(terms.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            searchable
            w={200}
            onChange={async (termId) => {
              if (!termId) return;
              await call(`/api/governance/glossary/${termId}/links`, { method: 'POST', body: json({ dataset_id: d.id }) });
              setAdding(false);
              qc.invalidateQueries({ queryKey: ['dataset', d.id] });
            }}
          />
        ) : (
          <Button size="compact-xs" variant="subtle" onClick={() => setAdding(true)}>
            Link term
          </Button>
        ))}
    </Group>
  );
}

/** Ad-hoc SQL over the lake, read under the user's policies on a worker. */
export function QueryPanel({ d }: { d: DatasetDetail }) {
  const [sql, setSql] = useState(`select *\nfrom {{ source('${d.layer}', '${d.name}') }}\nlimit 100`);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState(false);
  async function runQuery() {
    setBusy(true);
    setResult(null);
    try {
      const t = await runTask<QueryResult>(api<Task<QueryResult>>('/api/query', { method: 'POST', body: json({ sql, limit: 500 }) }));
      setResult(t.status === 'failed' ? { ok: false, error: t.error ?? 'Query failed' } : t.result);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Stack>
      <Textarea
        aria-label="SQL query"
        value={sql}
        onChange={(e) => setSql(e.currentTarget.value)}
        autosize
        minRows={4}
        styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 13 } }}
        spellCheck={false}
      />
      <Group>
        <Button leftSection={<IconPlayerPlay size={14} />} loading={busy} onClick={runQuery}>
          Run query
        </Button>
        <Text size="xs" c="dimmed">
          One SELECT; reference tables with {'{{ source(…) }}'}, {'{{ ref(…) }}'} or {'{{ vault(…) }}'}. Masking and row
          filters apply; queries are audited.
        </Text>
      </Group>
      {result && !result.ok && <Alert color="red">{result.error}</Alert>}
      {result?.ok && (
        <>
          <PreviewTable preview={{ columns: result.columns ?? [], rows: result.rows ?? [] }} />
          {result.truncated && <Text size="xs" c="dimmed">Showing the first 500 rows.</Text>}
        </>
      )}
    </Stack>
  );
}
