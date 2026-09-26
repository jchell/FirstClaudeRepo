import { Badge } from '@mantine/core';
import { IconCircleCheck, IconCircleX, IconClock, IconLoader2 } from '@tabler/icons-react';

const STYLE: Record<string, { color: string; icon: typeof IconCircleCheck }> = {
  succeeded: { color: 'green', icon: IconCircleCheck },
  failed: { color: 'red', icon: IconCircleX },
  running: { color: 'blue', icon: IconLoader2 },
  queued: { color: 'gray', icon: IconClock },
};

/** Status is always shown as icon + word, never color alone. */
export function StatusBadge({ status }: { status: string }) {
  const s = STYLE[status] ?? { color: 'gray', icon: IconClock };
  return (
    <Badge color={s.color} variant="light" leftSection={<s.icon size={12} aria-hidden />}>
      {status}
    </Badge>
  );
}
