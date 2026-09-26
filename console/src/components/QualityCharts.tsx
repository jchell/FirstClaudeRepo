import { useMemo, useState } from 'react';
import { Group, Table, Text, useComputedColorScheme } from '@mantine/core';

// Categorical slot 1 (validated in StreamCharts) for the score; the baseline is a
// reference, drawn as a dashed neutral line and labelled directly.
const COLORS = {
  light: { score: '#2a78d6', ref: '#7a7a74', bar: '#2a78d6' },
  dark: { score: '#3987e5', ref: '#9a9a94', bar: '#3987e5' },
};

const W = 560;
const H = 180;
const PAD = { top: 10, right: 58, bottom: 24, left: 36 };

export interface ScorePoint {
  ts: string;
  score: number | null;
  baseline?: number | null;
  degraded?: boolean;
}

const fmtTs = (iso: string) => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

/** Score over time (0-100, fixed axis) with the rolling baseline it is compared against. */
export function ScoreTrend({ points, label = 'Score' }: { points: ScorePoint[]; label?: string }) {
  const scheme = useComputedColorScheme('light');
  const c = COLORS[scheme === 'dark' ? 'dark' : 'light'];
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const g = useMemo(() => {
    const n = points.length;
    const x = (i: number) => PAD.left + (n <= 1 ? (W - PAD.left - PAD.right) / 2 : ((W - PAD.left - PAD.right) * i) / (n - 1));
    const y = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / 100);
    const path = (key: 'score' | 'baseline') =>
      points
        .map((p, i) => (p[key] == null ? null : `${x(i)},${y(p[key] as number)}`))
        .filter(Boolean)
        .join(' ');
    return { x, y, score: path('score'), baseline: path('baseline') };
  }, [points]);

  if (!points.length) return <Text size="sm" c="dimmed">No runs yet.</Text>;
  const last = points[points.length - 1];
  const lastBase = [...points].reverse().find((p) => p.baseline != null);
  const h = hover !== null ? points[hover] : null;
  const slot = (W - PAD.left - PAD.right) / Math.max(points.length - 1, 1);

  return (
    <div>
      <Group justify="space-between" mb={4}>
        <Text size="xs" c="dimmed">
          {h
            ? `${fmtTs(h.ts)}: ${label.toLowerCase()} ${h.score ?? '—'}${h.baseline != null ? ` · baseline ${h.baseline}` : ''}${h.degraded ? ' · degraded' : ''}`
            : 'Hover a run for details'}
        </Text>
        <Text size="xs" c="blue" style={{ cursor: 'pointer' }} onClick={() => setTable(!table)} role="button">
          {table ? 'Show chart' : 'Show table'}
        </Text>
      </Group>
      {table ? (
        <Table fz="xs" striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Run</Table.Th>
              <Table.Th>{label}</Table.Th>
              <Table.Th>Baseline</Table.Th>
              <Table.Th>Degraded</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {points.map((p) => (
              <Table.Tr key={p.ts}>
                <Table.Td>{fmtTs(p.ts)}</Table.Td>
                <Table.Td>{p.score ?? '—'}</Table.Td>
                <Table.Td>{p.baseline ?? '—'}</Table.Td>
                <Table.Td>{p.degraded ? 'yes' : 'no'}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`${label} per run, 0 to 100, with its baseline`}>
          {[0, 50, 100].map((v) => (
            <g key={v}>
              <line x1={PAD.left} x2={W - PAD.right} y1={g.y(v)} y2={g.y(v)} stroke="var(--mantine-color-default-border)" />
              <text x={PAD.left - 6} y={g.y(v) + 4} fontSize={10} textAnchor="end" fill="var(--mantine-color-dimmed)">
                {v}
              </text>
            </g>
          ))}
          <text x={PAD.left} y={H - 6} fontSize={10} fill="var(--mantine-color-dimmed)">
            {fmtTs(points[0].ts)}
          </text>
          {points.length > 1 && (
            <text x={W - PAD.right} y={H - 6} fontSize={10} textAnchor="end" fill="var(--mantine-color-dimmed)">
              {fmtTs(last.ts)}
            </text>
          )}
          {g.baseline && <polyline points={g.baseline} fill="none" stroke={c.ref} strokeWidth={1.5} strokeDasharray="5 4" />}
          {g.score && <polyline points={g.score} fill="none" stroke={c.score} strokeWidth={2} strokeLinejoin="round" />}
          {points.map((p, i) =>
            p.score == null ? null : (
              <circle
                key={p.ts}
                cx={g.x(i)}
                cy={g.y(p.score)}
                r={hover === i ? 5 : 4}
                fill={c.score}
                stroke="var(--mantine-color-body)"
                strokeWidth={2}
              />
            ),
          )}
          {last.score != null && (
            <text x={W - PAD.right + 6} y={g.y(last.score) + 4} fontSize={10} fill="var(--mantine-color-text)">
              {label.toLowerCase()} {last.score}
            </text>
          )}
          {lastBase?.baseline != null && (
            <text x={W - PAD.right + 6} y={g.y(lastBase.baseline) + (last.score != null && Math.abs(g.y(lastBase.baseline) - g.y(last.score)) < 12 ? 16 : 4)} fontSize={10} fill="var(--mantine-color-dimmed)">
              baseline
            </text>
          )}
          {hover !== null && (
            <line x1={g.x(hover)} x2={g.x(hover)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--mantine-color-dimmed)" strokeDasharray="2 3" />
          )}
          {points.map((p, i) => (
            <rect
              key={`hit-${p.ts}`}
              x={g.x(i) - slot / 2}
              y={PAD.top}
              width={Math.max(slot, 12)}
              height={H - PAD.top - PAD.bottom}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        </svg>
      )}
    </div>
  );
}

/** Score per DQ dimension: one magnitude, one hue, labelled bars. */
export function DimensionBars({ dimensions }: { dimensions: Record<string, number> }) {
  const scheme = useComputedColorScheme('light');
  const color = COLORS[scheme === 'dark' ? 'dark' : 'light'].bar;
  const entries = Object.entries(dimensions).sort((a, b) => a[0].localeCompare(b[0]));
  if (!entries.length) return <Text size="sm" c="dimmed">No dimension scores yet.</Text>;
  const rowH = 22;
  const labelW = 100;
  const barW = 300;
  return (
    <svg viewBox={`0 0 ${labelW + barW + 50} ${entries.length * rowH + 4}`} width="100%" style={{ maxWidth: 520 }} role="img" aria-label="Score per data quality dimension, 0 to 100">
      {entries.map(([dim, v], i) => {
        const w = Math.max((barW * v) / 100, 2);
        const y = i * rowH + 4;
        return (
          <g key={dim}>
            <title>{`${dim}: ${v}`}</title>
            <text x={labelW - 8} y={y + 12} fontSize={11} textAnchor="end" fill="var(--mantine-color-text)">
              {dim}
            </text>
            <rect x={labelW} y={y + 2} width={barW} height={12} rx={4} fill="var(--mantine-color-default-hover)" />
            <rect x={labelW} y={y + 2} width={w} height={12} rx={4} fill={color} />
            <text x={labelW + barW + 6} y={y + 12} fontSize={11} fill="var(--mantine-color-text)">
              {v}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
