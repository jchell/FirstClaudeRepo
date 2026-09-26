import { useMemo, useState } from 'react';
import { Group, SegmentedControl, Table, Text } from '@mantine/core';
import { IconCircleCheck, IconCircleX } from '@tabler/icons-react';

import type { OpsSummary } from '../api/types';

// Status palette (fixed across themes). Red/green can't be told apart under
// deuteranopia, so "failed" also always sits on top of each stack, and the legend,
// tooltip and table name the status with an icon and a label.
const GOOD = '#0ca30c';
const CRITICAL = '#d03b3b';

const W = 720;
const H = 200;
const PAD = { top: 12, right: 8, bottom: 26, left: 36 };
const GAP = 2;

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * pow * 4 >= v) ?? 10;
  return step * pow * 4;
}

/** A bar whose top corners are rounded (4px) and whose base stays square on the baseline. */
function topRounded(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y + h} Z`;
}

export function RunsChart({ series }: { series: OpsSummary['series'] }) {
  const [view, setView] = useState<'chart' | 'table'>('chart');
  const [hover, setHover] = useState<number | null>(null);

  const { bars, max, ticks } = useMemo(() => {
    const max = niceMax(Math.max(1, ...series.map((b) => b.succeeded + b.failed)));
    const inner = W - PAD.left - PAD.right;
    const slot = inner / Math.max(series.length, 1);
    const bw = Math.max(3, Math.min(18, slot - GAP * 2));
    const y = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / max);
    const bars = series.map((b, i) => {
      const x = PAD.left + i * slot + (slot - bw) / 2;
      const base = y(0);
      const okTop = y(b.succeeded);
      const failTop = y(b.succeeded + b.failed);
      return { ...b, x, bw, slot, base, okTop, failTop };
    });
    return { bars, max, ticks: [0, max / 4, max / 2, (3 * max) / 4, max].map((v) => ({ v, y: y(v) })) };
  }, [series]);

  const label = (t: string) => {
    const d = new Date(t);
    return series.length > 30 ? d.toLocaleDateString() : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const legend = (
    <Group gap="md">
      <Group gap={4}>
        <IconCircleCheck size={16} color={GOOD} aria-hidden />
        <Text size="sm">Succeeded</Text>
      </Group>
      <Group gap={4}>
        <IconCircleX size={16} color={CRITICAL} aria-hidden />
        <Text size="sm">Failed</Text>
      </Group>
    </Group>
  );

  return (
    <div>
      <Group justify="space-between" mb="xs">
        {legend}
        <SegmentedControl
          size="xs"
          value={view}
          onChange={(v) => setView(v as 'chart' | 'table')}
          data={[
            { label: 'Chart', value: 'chart' },
            { label: 'Table', value: 'table' },
          ]}
          aria-label="Chart or table view"
        />
      </Group>

      {series.length === 0 ? (
        <Text c="dimmed" size="sm">
          No runs in this period.
        </Text>
      ) : view === 'table' ? (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Time</Table.Th>
              <Table.Th>Succeeded</Table.Th>
              <Table.Th>Failed</Table.Th>
              <Table.Th>Rows loaded</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {series.map((b) => (
              <Table.Tr key={b.t}>
                <Table.Td>{new Date(b.t).toLocaleString()}</Table.Td>
                <Table.Td>{b.succeeded}</Table.Td>
                <Table.Td>{b.failed}</Table.Td>
                <Table.Td>{b.rows.toLocaleString()}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <div style={{ position: 'relative' }}>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Ingestion runs per time bucket, succeeded and failed">
            {ticks.map((t) => (
              <g key={t.v}>
                <line x1={PAD.left} x2={W - PAD.right} y1={t.y} y2={t.y} stroke="var(--mantine-color-default-border)" strokeWidth={1} />
                <text x={PAD.left - 6} y={t.y + 4} textAnchor="end" fontSize={10} fill="var(--mantine-color-dimmed)">
                  {Number.isInteger(t.v) ? t.v : t.v.toFixed(1)}
                </text>
              </g>
            ))}
            {bars.map((b, i) => (
              <g key={b.t}>
                {b.succeeded > 0 && (
                  <path
                    d={b.failed > 0 ? `M${b.x},${b.base} V${b.okTop} H${b.x + b.bw} V${b.base} Z` : topRounded(b.x, b.okTop, b.bw, b.base - b.okTop)}
                    fill={GOOD}
                  />
                )}
                {b.failed > 0 && (
                  <path
                    d={topRounded(b.x, b.failTop, b.bw, Math.max(b.okTop - b.failTop - (b.succeeded > 0 ? GAP : 0), 1))}
                    fill={CRITICAL}
                  />
                )}
                {/* Hit target: the whole slot, taller than the mark. */}
                <rect
                  x={b.x - (b.slot - b.bw) / 2}
                  y={PAD.top}
                  width={b.slot}
                  height={H - PAD.top - PAD.bottom}
                  fill="transparent"
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                />
                {(i === 0 || i === bars.length - 1 || i % Math.ceil(bars.length / 6) === 0) && (
                  <text x={b.x + b.bw / 2} y={H - 8} textAnchor="middle" fontSize={10} fill="var(--mantine-color-dimmed)">
                    {label(b.t)}
                  </text>
                )}
              </g>
            ))}
            <line x1={PAD.left} x2={W - PAD.right} y1={H - PAD.bottom} y2={H - PAD.bottom} stroke="var(--mantine-color-dimmed)" strokeWidth={1} />
            {max > 0 && hover !== null && (
              <line
                x1={bars[hover].x + bars[hover].bw / 2}
                x2={bars[hover].x + bars[hover].bw / 2}
                y1={PAD.top}
                y2={H - PAD.bottom}
                stroke="var(--mantine-color-dimmed)"
                strokeDasharray="2 3"
              />
            )}
          </svg>
          {hover !== null && (
            <div
              role="tooltip"
              style={{
                position: 'absolute',
                top: 0,
                left: `${Math.min(((bars[hover].x + bars[hover].bw) / W) * 100 + 1, 75)}%`,
                background: 'var(--mantine-color-body)',
                border: '1px solid var(--mantine-color-default-border)',
                borderRadius: 6,
                padding: '6px 10px',
                pointerEvents: 'none',
                boxShadow: 'var(--mantine-shadow-sm)',
              }}
            >
              <Text size="xs" fw={600}>
                {new Date(bars[hover].t).toLocaleString()}
              </Text>
              <Group gap={4}>
                <IconCircleCheck size={14} color={GOOD} aria-hidden />
                <Text size="xs">Succeeded: {bars[hover].succeeded}</Text>
              </Group>
              <Group gap={4}>
                <IconCircleX size={14} color={CRITICAL} aria-hidden />
                <Text size="xs">Failed: {bars[hover].failed}</Text>
              </Group>
              <Text size="xs" c="dimmed">
                {bars[hover].rows.toLocaleString()} rows loaded
              </Text>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
