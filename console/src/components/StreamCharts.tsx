import { useMemo, useState } from 'react';
import { Group, SimpleGrid, Table, Text, useComputedColorScheme } from '@mantine/core';

import type { StreamMetrics } from '../api/types';

// Categorical slots 1 and 2 of the reference palette, light / dark steps
// (validated: CVD ΔE ≥ 24 in both modes).
const SERIES = {
  light: { p50: '#2a78d6', p95: '#eb6834', bars: '#2a78d6' },
  dark: { p50: '#3987e5', p95: '#d95926', bars: '#3987e5' },
};

const W = 520;
const H = 170;
const PAD = { top: 10, right: 44, bottom: 22, left: 44 };

type Row = StreamMetrics['series'][number];

function niceMax(v: number) {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  return ([1, 2, 2.5, 5, 10].find((s) => s * pow * 4 >= v) ?? 10) * pow * 4;
}

function fmtMs(v: number) {
  return v >= 10_000 ? `${(v / 1000).toFixed(0)}s` : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
}

function Frame({ max, fmt, children, label }: { max: number; fmt: (v: number) => string; children: React.ReactNode; label: string }) {
  const y = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / max);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={label}>
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(max * f)} y2={y(max * f)} stroke="var(--mantine-color-default-border)" />
          <text x={PAD.left - 6} y={y(max * f) + 4} fontSize={10} textAnchor="end" fill="var(--mantine-color-dimmed)">
            {fmt(max * f)}
          </text>
        </g>
      ))}
      {children}
    </svg>
  );
}

export function StreamCharts({ series }: { series: Row[] }) {
  const scheme = useComputedColorScheme('light');
  const c = SERIES[scheme === 'dark' ? 'dark' : 'light'];
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);

  const g = useMemo(() => {
    const n = Math.max(series.length, 1);
    const slot = (W - PAD.left - PAD.right) / n;
    const x = (i: number) => PAD.left + slot * i + slot / 2;
    const maxRec = niceMax(Math.max(1, ...series.map((r) => r.records)));
    const maxLat = niceMax(Math.max(1, ...series.map((r) => r.latency_p95_ms ?? 0)));
    const yRec = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / maxRec);
    const yLat = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / maxLat);
    const line = (k: 'latency_p50_ms' | 'latency_p95_ms') =>
      series
        .map((r, i) => (r[k] == null ? null : `${x(i)},${yLat(r[k] as number)}`))
        .filter(Boolean)
        .join(' ');
    return { slot, x, maxRec, maxLat, yRec, yLat, p50: line('latency_p50_ms'), p95: line('latency_p95_ms') };
  }, [series]);

  if (!series.length) return <Text c="dimmed" size="sm">No batches in the last hour.</Text>;
  const last = [...series].reverse().find((r) => r.latency_p50_ms != null);
  const bw = Math.max(2, Math.min(12, g.slot - 4));
  const hitboxes = series.map((r, i) => (
    <rect
      key={r.minute}
      x={g.x(i) - g.slot / 2}
      y={PAD.top}
      width={g.slot}
      height={H - PAD.top - PAD.bottom}
      fill="transparent"
      onMouseEnter={() => setHover(i)}
      onMouseLeave={() => setHover(null)}
    />
  ));
  const crosshair = hover !== null && (
    <line x1={g.x(hover)} x2={g.x(hover)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--mantine-color-dimmed)" strokeDasharray="2 3" />
  );
  const h = hover !== null ? series[hover] : null;

  return (
    <div>
      <Group justify="space-between" mb={4}>
        <Text size="xs" c="dimmed">
          {h
            ? `${new Date(h.minute).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}: ${h.records.toLocaleString()} records · ` +
              `p50 ${h.latency_p50_ms != null ? fmtMs(h.latency_p50_ms) : '—'} · p95 ${h.latency_p95_ms != null ? fmtMs(h.latency_p95_ms) : '—'}` +
              (h.dlq ? ` · ${h.dlq} dead letters` : '')
            : 'Hover a minute for details'}
        </Text>
        <Text size="xs" c="blue" style={{ cursor: 'pointer' }} onClick={() => setTable(!table)} role="button">
          {table ? 'Show charts' : 'Show table'}
        </Text>
      </Group>
      {table ? (
        <Table fz="xs" striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Minute</Table.Th>
              <Table.Th>Records</Table.Th>
              <Table.Th>Dead letters</Table.Th>
              <Table.Th>Latency p50</Table.Th>
              <Table.Th>Latency p95</Table.Th>
              <Table.Th>Lag</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {series.map((r) => (
              <Table.Tr key={r.minute}>
                <Table.Td>{new Date(r.minute).toLocaleTimeString()}</Table.Td>
                <Table.Td>{r.records}</Table.Td>
                <Table.Td>{r.dlq}</Table.Td>
                <Table.Td>{r.latency_p50_ms != null ? fmtMs(r.latency_p50_ms) : '—'}</Table.Td>
                <Table.Td>{r.latency_p95_ms != null ? fmtMs(r.latency_p95_ms) : '—'}</Table.Td>
                <Table.Td>{r.lag ?? '—'}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <SimpleGrid cols={{ base: 1, md: 2 }}>
          <div>
            <Text size="sm" fw={600}>Records per minute</Text>
            <Frame max={g.maxRec} fmt={(v) => String(Math.round(v))} label="Records ingested per minute">
              {series.map((r, i) =>
                r.records > 0 ? (
                  <path
                    key={r.minute}
                    fill={c.bars}
                    d={(() => {
                      const x0 = g.x(i) - bw / 2;
                      const y0 = g.yRec(r.records);
                      const base = g.yRec(0);
                      const rr = Math.min(4, bw / 2, base - y0);
                      return `M${x0},${base} V${y0 + rr} Q${x0},${y0} ${x0 + rr},${y0} H${x0 + bw - rr} Q${x0 + bw},${y0} ${x0 + bw},${y0 + rr} V${base} Z`;
                    })()}
                  />
                ) : null,
              )}
              {crosshair}
              {hitboxes}
            </Frame>
          </div>
          <div>
            <Group gap="md">
              <Text size="sm" fw={600}>End-to-end latency</Text>
              <Group gap={4}>
                <span style={{ width: 14, height: 2, background: c.p50, display: 'inline-block' }} />
                <Text size="xs">p50</Text>
              </Group>
              <Group gap={4}>
                <span style={{ width: 14, height: 2, background: c.p95, display: 'inline-block' }} />
                <Text size="xs">p95</Text>
              </Group>
            </Group>
            <Frame max={g.maxLat} fmt={fmtMs} label="End-to-end latency from source commit to bronze, p50 and p95">
              <polyline points={g.p95} fill="none" stroke={c.p95} strokeWidth={2} strokeLinejoin="round" />
              <polyline points={g.p50} fill="none" stroke={c.p50} strokeWidth={2} strokeLinejoin="round" />
              {last && (
                <>
                  <text x={W - PAD.right + 4} y={g.yLat(last.latency_p95_ms ?? 0) + 4} fontSize={10} fill="var(--mantine-color-text)">
                    p95
                  </text>
                  <text x={W - PAD.right + 4} y={g.yLat(last.latency_p50_ms ?? 0) + 12} fontSize={10} fill="var(--mantine-color-text)">
                    p50
                  </text>
                </>
              )}
              {hover !== null && series[hover].latency_p50_ms != null && (
                <>
                  <circle cx={g.x(hover)} cy={g.yLat(series[hover].latency_p50_ms!)} r={4} fill={c.p50} stroke="var(--mantine-color-body)" strokeWidth={2} />
                  <circle cx={g.x(hover)} cy={g.yLat(series[hover].latency_p95_ms ?? 0)} r={4} fill={c.p95} stroke="var(--mantine-color-body)" strokeWidth={2} />
                </>
              )}
              {crosshair}
              {hitboxes}
            </Frame>
          </div>
        </SimpleGrid>
      )}
    </div>
  );
}
