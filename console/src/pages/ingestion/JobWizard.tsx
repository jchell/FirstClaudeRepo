import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  JsonInput,
  Loader,
  NumberInput,
  Paper,
  Radio,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Stepper,
  Table,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { IconAlertCircle, IconEye, IconSearch } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';

import { api, json } from '../../api/client';
import { runTask } from '../../api/tasks';
import type { Connection, IngestionJob, JobSpec, Preview, SourceObject, Task } from '../../api/types';
import { useConnectionTypes } from '../connections/ConnectionsPage';

const EMPTY: JobSpec = {
  source: { format_options: {}, options: {} },
  load_mode: 'full',
  target: { layer: 'bronze', dataset: '' },
  schedule: { type: 'none' },
};

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/\{[^}]*\}/g, '')
    .replace(/\.[a-z0-9]+$/, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .replace(/^(\d)/, 'd_$1')
    .slice(0, 60);

const LOAD_HELP: Record<string, Record<string, string>> = {
  file: {
    full: 'Replace the bronze table with every matching file on each run.',
    incremental: 'New files only: files already loaded (same size and timestamp) are skipped; new or changed files are appended.',
    append: 'Append every matching file on each run, even if it was loaded before.',
  },
  default: {
    full: 'Replace the bronze table with a full copy of the source on each run.',
    incremental: 'Only rows whose watermark column is greater than the last run’s maximum are read and appended.',
    append: 'Append everything the source returns on each run.',
  },
  event: {
    full: 'Replace the bronze table with the events read in this run.',
    incremental: 'Append events since the last committed offset.',
    append: 'Append events since the last committed offset.',
  },
};

export function JobWizard() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const types = useConnectionTypes();
  const conns = useQuery({ queryKey: ['connections'], queryFn: () => api<Connection[]>('/api/connections') });
  const existing = useQuery({
    queryKey: ['ingestion-job', jobId],
    queryFn: () => api<IngestionJob>(`/api/ingestion/jobs/${jobId}`),
    enabled: !!jobId,
  });

  const [step, setStep] = useState(0);
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [spec, setSpec] = useState<JobSpec>(EMPTY);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [sqlMode, setSqlMode] = useState<'table' | 'query'>('table');
  const [objects, setObjects] = useState<SourceObject[] | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (existing.data) {
      setConnectionId(existing.data.connection_id);
      setSpec({ ...EMPTY, ...existing.data.spec });
      setName(existing.data.name);
      setDescription(existing.data.description);
      setSqlMode(existing.data.spec.source.query ? 'query' : 'table');
      setStep(1);
    }
  }, [existing.data]);

  const conn = conns.data?.find((c) => c.id === connectionId);
  const category = types.data?.find((t) => t.type === conn?.type)?.category;
  const src = spec.source;
  const setSource = (patch: Partial<JobSpec['source']>) => setSpec((s) => ({ ...s, source: { ...s.source, ...patch } }));
  const help = LOAD_HELP[category === 'file' ? 'file' : category === 'event' ? 'event' : 'default'];
  const columns = useMemo(() => preview?.columns.map((c) => c.name) ?? [], [preview]);

  async function task<R>(label: string, path: string, body: unknown): Promise<R | null> {
    setBusy(label);
    setError(null);
    try {
      const t = await runTask<R>(api<Task<R>>(path, { method: 'POST', body: json(body) }));
      if (t.status !== 'succeeded') {
        setError(t.error ?? `${label} failed`);
        return null;
      }
      return t.result;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(null);
    }
  }

  const discover = async () => {
    const r = await task<{ objects: SourceObject[] }>('Browsing', `/api/connections/${connectionId}/discover`, {});
    if (r) setObjects(r.objects);
  };
  const runPreview = async () => {
    const body = sqlMode === 'query' && category === 'database' ? { query: src.query } : { ...src, query: undefined };
    const r = await task<Preview>('Previewing', `/api/connections/${connectionId}/preview`, body);
    if (r) {
      setPreview(r);
      if (!spec.target.dataset) {
        const base = src.object ?? src.path_template ?? (conn?.name ?? 'dataset');
        setSpec((s) => ({ ...s, target: { ...s.target, dataset: slug(`${conn?.name ?? ''}_${base.split('/').pop()}`) } }));
      }
    }
  };

  const sourceReady =
    category === 'file' ? !!src.path_template : category === 'database' ? !!(sqlMode === 'query' ? src.query : src.object) : !!src.object;
  const loadReady = spec.load_mode !== 'incremental' || category === 'file' || category === 'event' || !!spec.watermark_column;

  const save = async (runAfter: boolean) => {
    setBusy('Saving');
    setError(null);
    const finalSpec: JobSpec = {
      ...spec,
      source:
        category === 'database'
          ? sqlMode === 'query'
            ? { ...src, object: null }
            : { ...src, query: null }
          : src,
    };
    try {
      const job = jobId
        ? await api<IngestionJob>(`/api/ingestion/jobs/${jobId}`, { method: 'PUT', body: json({ spec: finalSpec, description }) })
        : await api<IngestionJob>('/api/ingestion/jobs', {
            method: 'POST',
            body: json({ name, description, connection_id: connectionId, spec: finalSpec }),
          });
      if (runAfter) await api(`/api/ingestion/jobs/${job.id}/run`, { method: 'POST' });
      navigate(`/ingestion/${job.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (jobId && existing.isLoading) return <Loader />;

  return (
    <Stack maw={980}>
      <Title order={2}>{jobId ? `Edit ${name}` : 'New ingestion job'}</Title>
      {error && (
        <Alert color="red" icon={<IconAlertCircle size={16} />} withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={false}>
        {/* ------------------------------------------------ 1. connection */}
        <Stepper.Step label="Connection" description="Where the data is">
          <Stack mt="md" maw={520}>
            <Select
              label="Connection"
              searchable
              disabled={!!jobId}
              data={(conns.data ?? []).map((c) => ({ value: c.id, label: `${c.name} (${c.type})` }))}
              value={connectionId}
              onChange={(v) => {
                setConnectionId(v);
                setSpec(EMPTY);
                setObjects(null);
                setPreview(null);
              }}
            />
            <Group>
              <Button disabled={!connectionId} onClick={() => setStep(1)}>
                Next
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>

        {/* ------------------------------------------------ 2. source */}
        <Stepper.Step label="Source" description="What to read">
          <Stack mt="md">
            {category === 'database' && (
              <SegmentedControl
                w={260}
                value={sqlMode}
                onChange={(v) => setSqlMode(v as 'table' | 'query')}
                data={[
                  { label: 'Pick a table', value: 'table' },
                  { label: 'Write SQL', value: 'query' },
                ]}
              />
            )}
            {category === 'database' && sqlMode === 'query' ? (
              <Textarea
                label="SQL query"
                autosize
                minRows={4}
                ff="monospace"
                placeholder="select id, name, updated_at from sales.customers where active"
                value={src.query ?? ''}
                onChange={(e) => setSource({ query: e.currentTarget.value })}
              />
            ) : category === 'file' ? (
              <Stack>
                <TextInput
                  label="Path template"
                  description="Relative to the connection's base path. Date tokens: {yyyyMMdd}, {yyyy}/{MM}/{dd}, {yyyyMMdd-1d}; wildcards * and ?"
                  placeholder="/in/orders_{yyyyMMdd}*.csv"
                  ff="monospace"
                  value={src.path_template ?? ''}
                  onChange={(e) => setSource({ path_template: e.currentTarget.value })}
                />
                <Group grow>
                  <Select
                    label="Format"
                    placeholder="From the file extension"
                    clearable
                    data={['csv', 'jsonl', 'json', 'parquet']}
                    value={src.format ?? null}
                    onChange={(v) => setSource({ format: v })}
                  />
                  <TextInput
                    label="CSV delimiter"
                    placeholder=","
                    value={(src.format_options?.delimiter as string) ?? ''}
                    onChange={(e) => setSource({ format_options: { ...src.format_options, delimiter: e.currentTarget.value || undefined } })}
                  />
                  <TextInput
                    label="JSON records key"
                    placeholder="e.g. data"
                    value={(src.format_options?.records_key as string) ?? ''}
                    onChange={(e) => setSource({ format_options: { ...src.format_options, records_key: e.currentTarget.value || undefined } })}
                  />
                </Group>
              </Stack>
            ) : category === 'api' ? (
              <ApiSourceFields src={src} setSource={setSource} />
            ) : (
              <Select
                label={category === 'event' ? 'Topic' : category === 'nosql' ? 'Collection' : 'Table'}
                searchable
                data={(objects ?? []).map((o) => o.name)}
                value={src.object ?? null}
                onChange={(v) => setSource({ object: v })}
                placeholder={objects ? 'Choose…' : 'Browse the source first'}
              />
            )}
            <Group>
              {category !== 'api' && (
                <Button variant="default" leftSection={<IconSearch size={16} />} loading={busy === 'Browsing'} onClick={discover}>
                  Browse source
                </Button>
              )}
              <Button variant="default" leftSection={<IconEye size={16} />} loading={busy === 'Previewing'} disabled={!sourceReady} onClick={runPreview}>
                Preview
              </Button>
            </Group>
            {objects && category !== 'database' && category !== 'nosql' && category !== 'event' && (
              <Paper withBorder p="xs">
                <Text size="sm" fw={500} mb={4}>
                  {objects.length} object(s) at the source
                </Text>
                <ScrollArea h={140}>
                  {objects.map((o) => (
                    <Text key={o.name} size="xs" ff="monospace">
                      {o.name}
                    </Text>
                  ))}
                </ScrollArea>
              </Paper>
            )}
            {preview && <PreviewTable preview={preview} />}
            <Group>
              <Button variant="default" onClick={() => setStep(0)} disabled={!!jobId}>
                Back
              </Button>
              <Button disabled={!sourceReady} onClick={() => setStep(2)}>
                Next
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>

        {/* ------------------------------------------------ 3. load mode */}
        <Stepper.Step label="Load mode" description="How each run loads">
          <Stack mt="md" maw={640}>
            <Radio.Group value={spec.load_mode} onChange={(v) => setSpec({ ...spec, load_mode: v as JobSpec['load_mode'] })}>
              <Stack>
                <Radio value="full" label="Full" description={help.full} />
                <Radio value="incremental" label={category === 'file' ? 'New files only' : 'Incremental (watermark)'} description={help.incremental} />
                <Radio value="append" label="Append" description={help.append} />
              </Stack>
            </Radio.Group>
            {spec.load_mode === 'incremental' && category !== 'file' && category !== 'event' && (
              <Select
                label="Watermark column"
                description="A column that only grows, e.g. updated_at or an increasing id"
                data={columns}
                searchable
                value={spec.watermark_column ?? null}
                onChange={(v) => setSpec({ ...spec, watermark_column: v })}
                placeholder={columns.length ? 'Choose…' : 'Preview the source to list columns'}
              />
            )}
            <Group>
              <Button variant="default" onClick={() => setStep(1)}>
                Back
              </Button>
              <Button disabled={!loadReady} onClick={() => setStep(3)}>
                Next
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>

        {/* ------------------------------------------------ 4. target & schedule */}
        <Stepper.Step label="Target & schedule" description="Where it lands, when it runs">
          <Stack mt="md" maw={640}>
            <TextInput
              label="Bronze dataset"
              description="Lowercase letters, digits and _; stored as a Delta table at s3://bronze/<name>"
              leftSection={<Text size="xs" c="dimmed">bronze.</Text>}
              leftSectionWidth={56}
              value={spec.target.dataset}
              onChange={(e) => setSpec({ ...spec, target: { layer: 'bronze', dataset: e.currentTarget.value } })}
              error={spec.target.dataset && !/^[a-z][a-z0-9_]{1,62}$/.test(spec.target.dataset) ? 'Invalid name' : undefined}
            />
            <SegmentedControl
              value={spec.schedule.type}
              onChange={(v) => setSpec({ ...spec, schedule: { type: v as JobSpec['schedule']['type'] } })}
              data={[
                { label: 'Manual', value: 'none' },
                { label: 'Cron', value: 'cron' },
                { label: 'Every…', value: 'interval' },
              ]}
            />
            {spec.schedule.type === 'cron' && (
              <TextInput
                label="Cron expression (UTC)"
                description="minute hour day month weekday, e.g. 15 2 * * * for 02:15 every day"
                ff="monospace"
                value={spec.schedule.cron ?? ''}
                onChange={(e) => setSpec({ ...spec, schedule: { type: 'cron', cron: e.currentTarget.value } })}
              />
            )}
            {spec.schedule.type === 'interval' && (
              <NumberInput
                label="Every (minutes)"
                min={1}
                value={spec.schedule.interval_seconds ? spec.schedule.interval_seconds / 60 : ''}
                onChange={(v) => setSpec({ ...spec, schedule: { type: 'interval', interval_seconds: Number(v) * 60 } })}
              />
            )}
            <Checkbox
              disabled
              label={
                <Group gap={6}>
                  Add to Raw Vault (hubs, links, satellites) <Badge size="xs" variant="light">Phase 2</Badge>
                </Group>
              }
            />
            <Checkbox
              disabled
              label={
                <Group gap={6}>
                  Promote to silver automatically <Badge size="xs" variant="light">Phase 2</Badge>
                </Group>
              }
            />
            <Group>
              <Button variant="default" onClick={() => setStep(2)}>
                Back
              </Button>
              <Button disabled={!/^[a-z][a-z0-9_]{1,62}$/.test(spec.target.dataset)} onClick={() => setStep(4)}>
                Next
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>

        {/* ------------------------------------------------ 5. review */}
        <Stepper.Step label="Review" description="Save the job">
          <Stack mt="md" maw={640}>
            <TextInput
              label="Job name"
              description="Lowercase letters, digits, _ and -"
              disabled={!!jobId}
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              error={name && !/^[a-z][a-z0-9_-]{1,127}$/.test(name) ? 'Invalid name' : undefined}
            />
            <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
            <Paper withBorder p="sm">
              <Text size="sm">
                Read from <b>{conn?.name}</b> ·{' '}
                <Code>{src.query ? 'SQL query' : src.path_template ?? src.object}</Code>
              </Text>
              <Text size="sm">
                Load: <b>{spec.load_mode}</b>
                {spec.watermark_column && (
                  <>
                    {' '}
                    on <Code>{spec.watermark_column}</Code>
                  </>
                )}{' '}
                → <Code>bronze.{spec.target.dataset}</Code>
              </Text>
              <Text size="sm">
                Schedule:{' '}
                {spec.schedule.type === 'none'
                  ? 'manual'
                  : spec.schedule.type === 'cron'
                    ? `cron ${spec.schedule.cron}`
                    : `every ${(spec.schedule.interval_seconds ?? 0) / 60} min`}
              </Text>
              <Text size="xs" c="dimmed" mt={4}>
                Each run adds _load_ts, _source, _batch_id and _file columns, registers the table in the catalog, profiles
                it and records lineage.
              </Text>
            </Paper>
            <Group>
              <Button variant="default" onClick={() => setStep(3)}>
                Back
              </Button>
              <Button variant="light" loading={busy === 'Saving'} disabled={!/^[a-z][a-z0-9_-]{1,127}$/.test(name)} onClick={() => save(false)}>
                Save
              </Button>
              <Button loading={busy === 'Saving'} disabled={!/^[a-z][a-z0-9_-]{1,127}$/.test(name)} onClick={() => save(true)}>
                Save and run now
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>
      </Stepper>
    </Stack>
  );
}

function ApiSourceFields({ src, setSource }: { src: JobSpec['source']; setSource: (p: Partial<JobSpec['source']>) => void }) {
  const opts = (src.options ?? {}) as Record<string, unknown>;
  const pag = (opts.pagination ?? { type: 'none' }) as Record<string, unknown>;
  const setOpts = (p: Record<string, unknown>) => setSource({ options: { ...opts, ...p } });
  return (
    <Stack>
      <TextInput label="Endpoint path" placeholder="/v1/customers" ff="monospace" value={src.object ?? ''} onChange={(e) => setSource({ object: e.currentTarget.value })} />
      <TextInput
        label="Records (JSONPath)"
        description="Where the list of records is in the response, e.g. $.data[*] (default: the whole body)"
        ff="monospace"
        value={(opts.records_path as string) ?? ''}
        onChange={(e) => setOpts({ records_path: e.currentTarget.value || undefined })}
      />
      <Select
        label="Pagination"
        data={[
          { value: 'none', label: 'None' },
          { value: 'page', label: 'Page number' },
          { value: 'offset', label: 'Offset / limit' },
          { value: 'cursor', label: 'Cursor (from the response)' },
          { value: 'next_url', label: 'Next-page URL (from the response)' },
          { value: 'link_header', label: 'Link header' },
        ]}
        value={(pag.type as string) ?? 'none'}
        onChange={(v) => setOpts({ pagination: { ...pag, type: v } })}
      />
      {['page', 'offset'].includes(pag.type as string) && (
        <NumberInput label="Page size" value={(pag.page_size as number) ?? 100} onChange={(v) => setOpts({ pagination: { ...pag, page_size: Number(v) } })} />
      )}
      {pag.type === 'cursor' && (
        <TextInput label="Next cursor (JSONPath)" placeholder="$.meta.next" ff="monospace" value={(pag.cursor_path as string) ?? ''}
          onChange={(e) => setOpts({ pagination: { ...pag, cursor_path: e.currentTarget.value } })} />
      )}
      {pag.type === 'next_url' && (
        <TextInput label="Next URL (JSONPath)" placeholder="$.links.next" ff="monospace" value={(pag.next_url_path as string) ?? ''}
          onChange={(e) => setOpts({ pagination: { ...pag, next_url_path: e.currentTarget.value } })} />
      )}
      <JsonInput
        label="Query parameters (JSON)"
        placeholder='{"status": "active"}'
        autosize
        minRows={2}
        defaultValue={opts.params ? JSON.stringify(opts.params, null, 2) : ''}
        onBlur={(e) => {
          try {
            setOpts({ params: e.currentTarget.value.trim() ? JSON.parse(e.currentTarget.value) : undefined });
          } catch {
            /* invalid JSON: keep previous */
          }
        }}
      />
      <TextInput
        label="Incremental: send the last watermark as"
        description="Query parameter name, e.g. since (used with the incremental load mode)"
        value={(opts.watermark_param as string) ?? ''}
        onChange={(e) => setOpts({ watermark_param: e.currentTarget.value || undefined })}
      />
    </Stack>
  );
}

export function PreviewTable({ preview }: { preview: Preview }) {
  if (!preview.columns.length) return <Text c="dimmed">The source returned no rows.</Text>;
  return (
    <Paper withBorder>
      <ScrollArea type="auto" mah={320}>
        <Table striped fz="xs" stickyHeader>
          <Table.Thead>
            <Table.Tr>
              {preview.columns.map((c) => (
                <Table.Th key={c.name}>
                  <div>{c.name}</div>
                  <Text size="xs" c="dimmed" fw={400}>
                    {c.type}
                  </Text>
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {preview.rows.map((r, i) => (
              <Table.Tr key={i}>
                {preview.columns.map((c) => (
                  <Table.Td key={c.name} style={{ whiteSpace: 'nowrap', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {r[c.name] == null ? <Text span c="dimmed" size="xs">null</Text> : String(r[c.name])}
                  </Table.Td>
                ))}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </ScrollArea>
    </Paper>
  );
}
