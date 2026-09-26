import { Badge, Group, Paper, Stack, Text, Title } from '@mantine/core';

import type { NavPage } from '../nav';

export function PlaceholderPage({ page }: { page: NavPage }) {
  return (
    <Stack maw={720}>
      <Group>
        <page.icon size={28} stroke={1.5} />
        <Title order={2}>{page.label}</Title>
        <Badge variant="light">Phase {page.phase}</Badge>
      </Group>
      <Paper withBorder p="lg" radius="md">
        <Text>{page.summary}</Text>
        <Text c="dimmed" size="sm" mt="sm">
          This page arrives in Phase {page.phase} of the build plan.
        </Text>
      </Paper>
    </Stack>
  );
}
