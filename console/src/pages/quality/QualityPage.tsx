import { useMemo, useState } from 'react';
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Drawer,
  Group,
  Loader,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
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
import {
  IconAlertTriangle,
  IconChecklist,
  IconCircleCheck,
  IconCircleDashed,
  IconCircleX,
  IconPlayerPlay,
  IconPlus,
  IconTrash,
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { useAuth } from '../../auth/AuthProvider';
import { api, json } from '../../api/client';
import { fmtNumber, fmtTime } from '../../api/tasks';
import type { Dataset, DatasetDetail, DqResultInfo, DqRuleInfo, DqSummary, RuleType, Scorecard } from '../../api/types';
import { DimensionBars, ScoreTrend } from '../../components/QualityCharts';

export const canSteward = (roles: string[] | undefined) =>
  !!roles && (roles.includes('admin') || roles.includes('steward') || roles.includes('engineer'));

const RULE_TYPES: { value: RuleType; label: string; hint: string }[] = [
  { value: 'not_null', label: 'Not null', hint: 'The column always has a value' },
  { value: 'unique', label: 'Unique', hint: 'No two rows share the key' },
  { value: 'range', label: 'Range', hint: 'Values lie between a minimum and a maximum' },
  { value: 'regex', label: 'Pattern (regex)', hint: 'Text values fully match a regular expression' },
  { value: 'allowed_values', label: 'Allowed values', hint: 'Values come from a fixed list' },
  { value: 'referential', label: 'Referential', hint: 'Every value exists in another dataset’s column' },
  { value: 'freshness', label: 'Freshness', hint: 'The newest row is at most N minutes old' },
  { value: 'row_count', label: 'Row count', hint: 'The table has between min and max rows' },
  { value: 'custom_sql', label: 'Custom SQL', hint: 'A SELECT over "data" returning the failing rows' },
];

/** Pass/fail always shows an icon and a word, never color alone. */
export function ResultBadge({ r }: { r: DqResultInfo | null }) {
  if (!r) {
    return (
      <Badge variant="light" color="gray" leftSection={<IconCircleDashed size={11} aria-hidden />}>
        not run
      </Badge>
    );
  }
  if (r.error) {
    return (
      <Badge variant="light" color="red" leftSection={<IconAlertTriangle size={11} aria-hidden />}>
        error
      </Badge>
    );
  }
  return r.passed ? (
    <Badge variant="light" color="green" leftSection={<IconCircleCheck size={11} aria-hidden />}>
      passed
    </Badge>
  ) : (
    <Badge variant="light" color="red" leftSection={<IconCircleX size={11} aria-hidden />}>
      failed
    </Badge>
  );
}

function ScoreText({ score }: { score: number | null | undefined }) {
  return <Text fw={600}>{score == null ? '—' : score.toFixed(1)}</Text>;
}

async function queue(path: string, body: unknown, ok: string) {
  try {
    const r = await api<{ queued: boolean; detail?: string }>(path, { method: 'POST', body: json(body) });
    notifications.show({ message: r.queued ? ok : (r.detail ?? 'already queued') });
  } catch (e) {
    notifications.show({ color: 'red', message: (e as Error).message });
  }
}

// ---------------------------------------------------------------- rule form

function RuleForm({ opened, onClose, edit, datasetId }: { opened: boolean; onClose: () => void; edit: DqRuleInfo | null; datasetId?: string }) {
  const qc = useQueryClient();
  const datasets = useQuery({ queryKey: ['catalog', 'all'], queryFn: () => api<Dataset[]>('/api/catalog/datasets'), enabled: opened });
  const [name, setName] = useState(edit?.name ?? '');
  const [ds, setDs] = useState<string | null>(edit?.dataset_id ?? datasetId ?? null);
  const [type, setType] = useState<RuleType>(edit?.rule_type ?? 'not_null');
  const [column, setColumn] = useState<string | null>(edit?.column ?? null);
  const [params, setParams] = useState<Record<string, unknown>>(edit?.params ?? {});
  const [severity, setSeverity] = useState<string>(edit?.severity ?? 'warning');
  const [threshold, setThreshold] = useState<number>(edit ? edit.threshold * 100 : 100);
  const [onLoad, setOnLoad] = useState<boolean>(edit?.run_on_load ?? false);
  const [description, setDescription] = useState(edit?.description ?? '');
  const [error, setError] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ['catalog', ds], queryFn: () => api<DatasetDetail>(`/api/catalog/datasets/${ds}`), enabled: !!ds && opened });
  const cols = (detail.data?.column_list ?? []).filter((c) => !c.removed_at).map((c) => c.name);
  const p = (k: string) => params[k] as never;
  const set = (k: string, v: unknown) => setParams({ ...params, [k]: v });
  const needsColumn = ['not_null', 'range', 'regex', 'allowed_values', 'referential', 'unique'].includes(type);

  async function save() {
    setError(null);
    const body = {
      name, dataset_id: ds, column: needsColumn ? column : null, rule_type: type, params, severity,
      threshold: threshold / 100, run_on_load: onLoad, description,
    };
    try {
      await api(edit ? `/api/quality/rules/${edit.id}` : '/api/quality/rules', { method: edit ? 'PUT' : 'POST', body: json(body) });
      qc.invalidateQueries({ queryKey: ['dq'] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} title={edit ? `Edit ${edit.name}` : 'New data quality rule'} size="lg">
      <Stack>
        <TextInput label="Name" description="Lower case, e.g. customers.email_not_null" value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <Select
          label="Dataset"
          data={(datasets.data ?? []).map((d) => ({ value: d.id, label: `${d.layer}.${d.name}` }))}
          value={ds}
          onChange={setDs}
          searchable
        />
        <Select
          label="Rule"
          data={RULE_TYPES.map((t) => ({ value: t.value, label: `${t.label} — ${t.hint}` }))}
          value={type}
          onChange={(v) => {
            setType(v as RuleType);
            setParams({});
          }}
          allowDeselect={false}
        />
        {needsColumn && <Select label="Column" data={cols} value={column} onChange={setColumn} searchable />}
        {(type === 'range' || type === 'row_count') && (
          <Group grow>
            <NumberInput label="Minimum" value={p('min') ?? ''} onChange={(v) => set('min', v === '' ? undefined : v)} />
            <NumberInput label="Maximum" value={p('max') ?? ''} onChange={(v) => set('max', v === '' ? undefined : v)} />
          </Group>
        )}
        {type === 'regex' && (
          <TextInput label="Pattern" description="Must match the whole value" ff="monospace" value={p('pattern') ?? ''} onChange={(e) => set('pattern', e.currentTarget.value)} />
        )}
        {type === 'allowed_values' && <TagsInput label="Allowed values" value={p('values') ?? []} onChange={(v) => set('values', v)} />}
        {type === 'unique' && (
          <MultiSelect label="Key columns (if more than one)" data={cols} value={p('columns') ?? []} onChange={(v) => set('columns', v)} />
        )}
        {type === 'referential' && (
          <Group grow>
            <Select
              label="Referenced dataset"
              data={(datasets.data ?? []).map((d) => `${d.layer}.${d.name}`)}
              value={p('ref_dataset') ?? null}
              onChange={(v) => set('ref_dataset', v)}
              searchable
            />
            <TextInput label="Referenced column" value={p('ref_column') ?? ''} onChange={(e) => set('ref_column', e.currentTarget.value)} />
          </Group>
        )}
        {type === 'freshness' && (
          <Group grow>
            <NumberInput label="Max age (minutes)" min={1} value={p('max_age_minutes') ?? ''} onChange={(v) => set('max_age_minutes', v)} />
            <Select label="Timestamp column" description="Default: load time" data={cols} value={p('timestamp_column') ?? null} onChange={(v) => set('timestamp_column', v ?? undefined)} clearable />
          </Group>
        )}
        {type === 'custom_sql' && (
          <Textarea
            label="Failing rows"
            description='A SELECT over the relation "data"; every row it returns fails'
            ff="monospace"
            autosize
            minRows={3}
            value={p('sql') ?? 'select * from data where '}
            onChange={(e) => set('sql', e.currentTarget.value)}
          />
        )}
        <Group grow>
          <Select label="Severity" description="Critical rules alert when they fail and count double" data={['warning', 'critical']} value={severity} onChange={(v) => setSeverity(v ?? 'warning')} />
          <NumberInput label="Pass threshold (%)" description="Share of rows that must pass" min={0} max={100} value={threshold} onChange={(v) => setThreshold(Number(v))} />
        </Group>
        <Switch label="Run after every load of the dataset" checked={onLoad} onChange={(e) => setOnLoad(e.currentTarget.checked)} />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        {error && <Alert color="red">{error}</Alert>}
        <Group justify="flex-end">
          <Button onClick={save} disabled={!name || !ds}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ---------------------------------------------------------------- rules

export function RulesTable({ rules, onOpen, steward }: { rules: DqRuleInfo[]; onOpen: (r: DqRuleInfo) => void; steward: boolean }) {
  if (!rules.length) return <Text c="dimmed" size="sm">No rules yet.</Text>;
  return (
    <Table striped highlightOnHover withTableBorder>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Rule</Table.Th>
          <Table.Th>Dataset · column</Table.Th>
          <Table.Th>Dimension</Table.Th>
          <Table.Th>Result</Table.Th>
          <Table.Th>Score</Table.Th>
          <Table.Th>Failed rows</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rules.map((r) => (
          <Table.Tr key={r.id} data-testid={`rule-${r.name}`}>
            <Table.Td>
              <Anchor onClick={() => onOpen(r)} fw={500} ff="monospace" size="sm">
                {r.name}
              </Anchor>
              <Group gap={4}>
                <Text size="xs" c="dimmed">{RULE_TYPES.find((t) => t.value === r.rule_type)?.label}</Text>
                {r.severity === 'critical' && <Badge size="xs" variant="outline" color="red">critical</Badge>}
                {r.run_on_load && <Badge size="xs" variant="outline">on load</Badge>}
              </Group>
            </Table.Td>
            <Table.Td ff="monospace" fz="xs" style={{ wordBreak: 'break-all' }}>
              {r.dataset}
              {r.column ? ` · ${r.column}` : ''}
            </Table.Td>
            <Table.Td>{r.dimension}</Table.Td>
            <Table.Td style={{ whiteSpace: 'nowrap' }}>
              <ResultBadge r={r.last} />
            </Table.Td>
            <Table.Td>{r.last?.score == null ? '—' : r.last.score.toFixed(1)}</Table.Td>
            <Table.Td>{r.last ? `${fmtNumber(r.last.rows_failed)} / ${fmtNumber(r.last.rows_checked)}` : '—'}</Table.Td>
            <Table.Td>
              {steward && (
                <ActionIcon variant="subtle" aria-label={`Run ${r.name}`} onClick={() => queue('/api/quality/run', { rule_ids: [r.id] }, `${r.name}: run queued`)}>
                  <IconPlayerPlay size={16} />
                </ActionIcon>
              )}
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

function RuleDrawer({ rule, onClose, steward, onEdit }: { rule: DqRuleInfo | null; onClose: () => void; steward: boolean; onEdit: () => void }) {
  const qc = useQueryClient();
  const results = useQuery({
    queryKey: ['dq', 'results', rule?.id],
    queryFn: () => api<DqResultInfo[]>(`/api/quality/rules/${rule!.id}/results?days=90`),
    enabled: !!rule,
    refetchInterval: 5000,
  });
  const last = results.data?.[results.data.length - 1] ?? rule?.last ?? null;
  return (
    <Drawer opened={!!rule} onClose={onClose} position="right" size="xl" title="Data quality rule">
      {rule && (
        <Stack>
          <Group justify="space-between">
            <Title order={3} ff="monospace">
              {rule.name}
            </Title>
            <ResultBadge r={last} />
          </Group>
          {rule.description && <Text>{rule.description}</Text>}
          <SimpleGrid cols={3}>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Checks</Text>
              <Text size="sm">{RULE_TYPES.find((t) => t.value === rule.rule_type)?.label} · {rule.dimension}</Text>
              <Text size="xs" ff="monospace">
                {rule.dataset}
                {rule.column ? ` · ${rule.column}` : ''}
              </Text>
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Passes at</Text>
              <Text size="sm">≥ {(rule.threshold * 100).toFixed(0)}% of rows · {rule.severity}</Text>
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Last run</Text>
              <Text size="sm">{fmtTime(last?.ts)}</Text>
            </Paper>
          </SimpleGrid>
          {!!Object.keys(rule.params).length && <Code block>{JSON.stringify(rule.params, null, 2)}</Code>}
          <Text fw={600} size="sm">Score per run</Text>
          <ScoreTrend points={(results.data ?? []).map((r) => ({ ts: r.ts, score: r.score }))} />
          {last?.error && <Alert color="red" title="Error">{last.error}</Alert>}
          {!!last?.sample?.length && (
            <Paper withBorder p="sm">
              <Text size="sm" fw={600}>
                Failing values (sample)
              </Text>
              <Text size="xs" c="dimmed">Masked like any data, per your access policies.</Text>
              <Group gap={6} mt={6}>
                {last.sample.map((v, i) => (
                  <Code key={i}>{v == null ? 'null' : typeof v === 'object' ? JSON.stringify(v) : String(v)}</Code>
                ))}
              </Group>
            </Paper>
          )}
          {steward && (
            <Group>
              <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={() => queue('/api/quality/run', { rule_ids: [rule.id] }, 'Run queued')}>
                Run now
              </Button>
              <Button size="xs" variant="default" onClick={onEdit}>
                Edit
              </Button>
              <Button
                size="xs"
                color="red"
                variant="subtle"
                leftSection={<IconTrash size={14} />}
                onClick={async () => {
                  if (!window.confirm(`Delete rule ${rule.name} and its history?`)) return;
                  await api(`/api/quality/rules/${rule.id}`, { method: 'DELETE' });
                  qc.invalidateQueries({ queryKey: ['dq'] });
                  onClose();
                }}
              >
                Delete
              </Button>
            </Group>
          )}
        </Stack>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- scorecards

function ScorecardForm({ opened, onClose, edit }: { opened: boolean; onClose: () => void; edit: Scorecard | null }) {
  const qc = useQueryClient();
  const rules = useQuery({ queryKey: ['dq', 'rules'], queryFn: () => api<DqRuleInfo[]>('/api/quality/rules'), enabled: opened });
  const [name, setName] = useState(edit?.name ?? '');
  const [description, setDescription] = useState(edit?.description ?? '');
  const [ruleIds, setRuleIds] = useState<string[]>(edit?.rule_ids ?? []);
  const [schedule, setSchedule] = useState<Scorecard['schedule']>(edit?.schedule ?? { type: 'none' });
  const [pct, setPct] = useState<number>(edit?.degradation_pct ?? 10);
  const [runs, setRuns] = useState<number>(edit?.baseline_runs ?? 7);
  const [error, setError] = useState<string | null>(null);
  async function save() {
    setError(null);
    try {
      await api(edit ? `/api/quality/scorecards/${edit.id}` : '/api/quality/scorecards', {
        method: edit ? 'PUT' : 'POST',
        body: json({ name, description, rule_ids: ruleIds, schedule, degradation_pct: pct, baseline_runs: runs }),
      });
      qc.invalidateQueries({ queryKey: ['dq'] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <Modal opened={opened} onClose={onClose} title={edit ? `Edit ${edit.name}` : 'New scorecard'} size="lg">
      <Stack>
        <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        <MultiSelect
          label="Rules"
          data={(rules.data ?? []).map((r) => ({ value: r.id, label: `${r.name} (${r.dataset})` }))}
          value={ruleIds}
          onChange={setRuleIds}
          searchable
        />
        <SegmentedControl
          value={schedule.type}
          onChange={(v) => setSchedule(v === 'cron' ? { type: 'cron', cron: '0 6 * * *' } : v === 'interval' ? { type: 'interval', interval_seconds: 3600 } : { type: 'none' })}
          data={[
            { label: 'No schedule', value: 'none' },
            { label: 'Cron', value: 'cron' },
            { label: 'Every…', value: 'interval' },
          ]}
        />
        {schedule.type === 'cron' && <TextInput label="Cron (UTC)" ff="monospace" value={schedule.cron ?? ''} onChange={(e) => setSchedule({ type: 'cron', cron: e.currentTarget.value })} />}
        {schedule.type === 'interval' && (
          <NumberInput label="Every (seconds)" min={60} value={schedule.interval_seconds ?? 3600} onChange={(v) => setSchedule({ type: 'interval', interval_seconds: Number(v) })} />
        )}
        <Group grow>
          <NumberInput label="Alert when the score drops (%)" description="below the rolling baseline" min={1} max={100} value={pct} onChange={(v) => setPct(Number(v))} />
          <NumberInput label="Baseline: mean of the last N runs" min={1} max={100} value={runs} onChange={(v) => setRuns(Number(v))} />
        </Group>
        {error && <Alert color="red">{error}</Alert>}
        <Group justify="flex-end">
          <Button onClick={save} disabled={!name || !ruleIds.length}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function ScorecardDrawer({ id, onClose, steward, onEdit, onOpenRule }: { id: string | null; onClose: () => void; steward: boolean; onEdit: (c: Scorecard) => void; onOpenRule: (r: DqRuleInfo) => void }) {
  const qc = useQueryClient();
  const card = useQuery({ queryKey: ['dq', 'card', id], queryFn: () => api<Scorecard>(`/api/quality/scorecards/${id}?days=90`), enabled: !!id, refetchInterval: 5000 });
  const c = card.data;
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="xl" title="Scorecard">
      {!c ? (
        <Loader />
      ) : (
        <Stack>
          <Group justify="space-between">
            <Title order={3}>{c.name}</Title>
            {c.degraded && (
              <Badge color="red" variant="light" leftSection={<IconAlertTriangle size={11} aria-hidden />}>
                degraded
              </Badge>
            )}
          </Group>
          {c.description && <Text>{c.description}</Text>}
          <SimpleGrid cols={3}>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Score</Text>
              <ScoreText score={c.score} />
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Baseline (last {c.baseline_runs} runs)</Text>
              <ScoreText score={c.baseline} />
            </Paper>
            <Paper withBorder p="xs">
              <Text size="xs" c="dimmed">Next run</Text>
              <Text size="sm">{c.next_run_at ? fmtTime(c.next_run_at) : c.schedule.type === 'none' ? 'not scheduled' : 'soon'}</Text>
            </Paper>
          </SimpleGrid>
          <Text fw={600} size="sm">Score and baseline per run</Text>
          <ScoreTrend points={(c.history ?? []).map((h) => ({ ts: h.ts, score: h.score, baseline: h.baseline, degraded: h.degraded }))} />
          <Text size="xs" c="dimmed">
            An alert opens when the score falls more than {c.degradation_pct}% below the baseline.
          </Text>
          <Text fw={600} size="sm">By dimension</Text>
          <DimensionBars dimensions={c.dimensions} />
          <Text fw={600} size="sm">Rules</Text>
          <RulesTable rules={c.rules} onOpen={onOpenRule} steward={steward} />
          {steward && (
            <Group>
              <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={() => queue(`/api/quality/scorecards/${c.id}/run`, {}, 'Run queued')}>
                Run now
              </Button>
              <Button size="xs" variant="default" onClick={() => onEdit(c)}>
                Edit
              </Button>
              <Button
                size="xs"
                color="red"
                variant="subtle"
                onClick={async () => {
                  if (!window.confirm(`Delete scorecard ${c.name}? Its rules stay.`)) return;
                  await api(`/api/quality/scorecards/${c.id}`, { method: 'DELETE' });
                  qc.invalidateQueries({ queryKey: ['dq'] });
                  onClose();
                }}
              >
                Delete
              </Button>
            </Group>
          )}
        </Stack>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- page

export function QualityPage() {
  const { user } = useAuth();
  const steward = canSteward(user?.roles);
  const summary = useQuery({ queryKey: ['dq', 'summary'], queryFn: () => api<DqSummary>('/api/quality/summary'), refetchInterval: 10_000 });
  const rules = useQuery({ queryKey: ['dq', 'rules'], queryFn: () => api<DqRuleInfo[]>('/api/quality/rules'), refetchInterval: 10_000 });
  const cards = useQuery({ queryKey: ['dq', 'cards'], queryFn: () => api<Scorecard[]>('/api/quality/scorecards'), refetchInterval: 10_000 });
  const [ruleForm, setRuleForm] = useState<{ edit: DqRuleInfo | null } | null>(null);
  const [cardForm, setCardForm] = useState<{ edit: Scorecard | null } | null>(null);
  const [openRule, setOpenRule] = useState<DqRuleInfo | null>(null);
  const [openCard, setOpenCard] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>('all');
  const shownRules = useMemo(
    () =>
      (rules.data ?? []).filter((r) =>
        filter === 'failing' ? r.last && !r.last.passed : filter === 'passing' ? r.last?.passed : filter === 'unevaluated' ? !r.last : true,
      ),
    [rules.data, filter],
  );
  const s = summary.data;

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <IconChecklist size={28} stroke={1.5} />
          <Title order={2}>Data Quality</Title>
        </Group>
        {steward && (
          <Group>
            <Button variant="default" leftSection={<IconPlus size={16} />} onClick={() => setCardForm({ edit: null })}>
              New scorecard
            </Button>
            <Button leftSection={<IconPlus size={16} />} onClick={() => setRuleForm({ edit: null })}>
              New rule
            </Button>
          </Group>
        )}
      </Group>
      <Text c="dimmed" maw={820}>
        Stewards define rules on catalog datasets and group them into scorecards. Results are kept as a time series; a
        scorecard that drops below its rolling baseline, and a failing critical rule, open an alert.
      </Text>
      <SimpleGrid cols={{ base: 2, md: 4 }}>
        {[
          { label: 'Rules', value: s?.rules, icon: IconChecklist },
          { label: 'Passing', value: s?.passing, icon: IconCircleCheck },
          { label: 'Failing', value: s?.failing, icon: IconCircleX },
          { label: 'Not run yet', value: s?.unevaluated, icon: IconCircleDashed },
        ].map((t) => (
          <Paper key={t.label} withBorder p="sm">
            <Group gap={6}>
              <t.icon size={16} aria-hidden />
              <Text size="sm" c="dimmed">{t.label}</Text>
            </Group>
            <Text size="xl" fw={700}>{t.value ?? '—'}</Text>
          </Paper>
        ))}
      </SimpleGrid>
      {!!s?.alerts.length && (
        <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title={`${s.alerts.length} open data quality alert(s)`}>
          <Stack gap={2}>
            {s.alerts.slice(0, 5).map((a) => (
              <Text key={a.id} size="sm">
                {a.message} <Text span c="dimmed" size="xs">({fmtTime(a.opened_at)})</Text>
              </Text>
            ))}
          </Stack>
        </Alert>
      )}
      <Tabs defaultValue="scorecards" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="scorecards">Scorecards</Tabs.Tab>
          <Tabs.Tab value="rules">Rules</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="scorecards" pt="sm">
          {cards.isLoading ? (
            <Loader />
          ) : !cards.data?.length ? (
            <Alert variant="light">No scorecards yet. Group rules into a scorecard to track a score over time.</Alert>
          ) : (
            <Table striped highlightOnHover withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Scorecard</Table.Th>
                  <Table.Th>Score</Table.Th>
                  <Table.Th>Baseline</Table.Th>
                  <Table.Th>State</Table.Th>
                  <Table.Th>Rules</Table.Th>
                  <Table.Th>Last run</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {cards.data.map((c) => (
                  <Table.Tr key={c.id} data-testid={`scorecard-${c.name}`}>
                    <Table.Td>
                      <Anchor onClick={() => setOpenCard(c.id)} fw={500}>
                        {c.name}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>{c.score == null ? '—' : c.score.toFixed(1)}</Table.Td>
                    <Table.Td>{c.baseline == null ? '—' : c.baseline.toFixed(1)}</Table.Td>
                    <Table.Td>
                      {c.score == null ? (
                        <Badge variant="light" color="gray" leftSection={<IconCircleDashed size={11} aria-hidden />}>not run</Badge>
                      ) : c.degraded ? (
                        <Badge variant="light" color="red" leftSection={<IconAlertTriangle size={11} aria-hidden />}>degraded</Badge>
                      ) : (
                        <Badge variant="light" color="green" leftSection={<IconCircleCheck size={11} aria-hidden />}>stable</Badge>
                      )}
                    </Table.Td>
                    <Table.Td>{c.rule_ids.length}</Table.Td>
                    <Table.Td>{fmtTime(c.last_run_at)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Tabs.Panel>
        <Tabs.Panel value="rules" pt="sm">
          <Stack>
            <SegmentedControl
              w="fit-content"
              value={filter}
              onChange={setFilter}
              data={[
                { label: 'All', value: 'all' },
                { label: 'Failing', value: 'failing' },
                { label: 'Passing', value: 'passing' },
                { label: 'Not run', value: 'unevaluated' },
              ]}
            />
            {rules.isLoading ? <Loader /> : <RulesTable rules={shownRules} onOpen={setOpenRule} steward={steward} />}
          </Stack>
        </Tabs.Panel>
      </Tabs>
      {ruleForm && <RuleForm opened onClose={() => setRuleForm(null)} edit={ruleForm.edit} />}
      {cardForm && <ScorecardForm opened onClose={() => setCardForm(null)} edit={cardForm.edit} />}
      <RuleDrawer
        rule={openRule}
        onClose={() => setOpenRule(null)}
        steward={steward}
        onEdit={() => {
          setRuleForm({ edit: openRule });
          setOpenRule(null);
        }}
      />
      <ScorecardDrawer
        id={openCard}
        onClose={() => setOpenCard(null)}
        steward={steward}
        onEdit={(c) => {
          setCardForm({ edit: c });
          setOpenCard(null);
        }}
        onOpenRule={(r) => {
          setOpenCard(null);
          setOpenRule(r);
        }}
      />
    </Stack>
  );
}

/** Rules of one dataset, for the catalog page. */
export function DatasetQuality({ datasetId }: { datasetId: string }) {
  const { user } = useAuth();
  const steward = canSteward(user?.roles);
  const rules = useQuery({ queryKey: ['dq', 'rules', datasetId], queryFn: () => api<DqRuleInfo[]>(`/api/quality/rules?dataset_id=${datasetId}`) });
  const [open, setOpen] = useState<DqRuleInfo | null>(null);
  const [form, setForm] = useState(false);
  return (
    <Stack>
      <Group>
        {steward && (
          <>
            <Button size="xs" leftSection={<IconPlus size={14} />} onClick={() => setForm(true)}>
              Add rule
            </Button>
            <Button size="xs" variant="default" leftSection={<IconPlayerPlay size={14} />} onClick={() => queue('/api/quality/run', { dataset_id: datasetId }, 'Rules queued')}>
              Run all
            </Button>
          </>
        )}
        <Anchor component={Link} to="/quality" size="sm">
          Data Quality
        </Anchor>
      </Group>
      {rules.isLoading ? <Loader /> : <RulesTable rules={rules.data ?? []} onOpen={setOpen} steward={steward} />}
      {form && <RuleForm opened onClose={() => setForm(false)} edit={null} datasetId={datasetId} />}
      <RuleDrawer rule={open} onClose={() => setOpen(null)} steward={steward} onEdit={() => setOpen(null)} />
    </Stack>
  );
}
