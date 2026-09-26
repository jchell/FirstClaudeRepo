import { ActionIcon, Button, Checkbox, Group, Paper, Stack, TagsInput, Text, TextInput } from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';

import type { RawVaultSpec } from '../../api/types';

const ident = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .replace(/^(\d)/, 'c_$1') || 'key';

/** Columns -> {business key: column}; business keys are the column names made identifier-safe. */
const keysOf = (cols: string[]) => Object.fromEntries(cols.map((c) => [ident(c), c]));

/**
 * The ingestion wizard's "Add to Raw Vault" step: business keys -> hub, descriptive
 * columns -> satellite, other keys -> links to further hubs.
 */
export function RawVaultFields({
  value,
  onChange,
  columns,
  dataset,
  cdc,
}: {
  value: RawVaultSpec | null | undefined;
  onChange: (v: RawVaultSpec | null) => void;
  columns: string[];
  dataset: string;
  cdc: boolean;
}) {
  const entity = ident(dataset.replace(/s$/, '')) || 'entity';
  const on = !!value;
  const keyCols = value ? Object.values(value.hub.keys) : [];
  const linkCols = new Set(value?.links.flatMap((l) => Object.values(l.hub.keys)) ?? []);
  const set = (patch: Partial<RawVaultSpec>) => value && onChange({ ...value, ...patch });

  return (
    <Stack gap="xs">
      <Checkbox
        label="Add to Raw Vault (hubs, links, satellites)"
        description="Load each run into the Data Vault: business keys become a hub, descriptive columns a satellite."
        checked={on}
        onChange={(e) =>
          onChange(
            e.currentTarget.checked
              ? { hub: { name: `hub_${entity}`, keys: {} }, attributes: [], satellite: null, track_deletes: cdc, links: [] }
              : null,
          )
        }
      />
      {value && (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Group grow align="start">
              <TextInput
                label="Hub"
                description="Existing hubs with the same business keys are reused"
                value={value.hub.name}
                onChange={(e) => set({ hub: { ...value.hub, name: e.currentTarget.value } })}
                error={!/^hub_[a-z0-9_]{1,58}$/.test(value.hub.name) ? 'hub_<name>' : undefined}
              />
              <TagsInput
                label="Business key column(s)"
                description="What identifies the entity across sources"
                data={columns}
                value={keyCols}
                onChange={(cols) => {
                  const attrs = value.attributes.filter((a) => !cols.includes(a));
                  set({ hub: { ...value.hub, keys: keysOf(cols) }, attributes: attrs });
                }}
                error={!keyCols.length ? 'Choose at least one' : undefined}
              />
            </Group>
            <TagsInput
              label="Satellite attributes"
              description={`Descriptive columns, versioned in ${value.satellite || `sat_${value.hub.name.replace(/^hub_/, '')}_details`} (only changes are stored)`}
              data={columns.filter((c) => !keyCols.includes(c) && !c.startsWith('_'))}
              value={value.attributes}
              onChange={(attributes) => set({ attributes })}
            />
            {!!columns.length && (
              <Button
                variant="subtle"
                size="compact-xs"
                w="fit-content"
                onClick={() =>
                  set({ attributes: columns.filter((c) => !keyCols.includes(c) && !linkCols.has(c) && !c.startsWith('_')) })
                }
              >
                Use all other columns
              </Button>
            )}
            <Checkbox
              label="Track deletes in a status satellite"
              description="Records when the source deletes a row (CDC replication)"
              checked={!!value.track_deletes}
              onChange={(e) => set({ track_deletes: e.currentTarget.checked })}
            />
            <Text size="sm" fw={600}>
              Relationships (links)
            </Text>
            {value.links.map((l, i) => (
              <Group key={i} align="end" wrap="nowrap">
                <TextInput
                  label="Link"
                  value={l.name}
                  onChange={(e) => {
                    const links = [...value.links];
                    links[i] = { ...l, name: e.currentTarget.value };
                    set({ links });
                  }}
                  error={!/^link_[a-z0-9_]{1,57}$/.test(l.name) ? 'link_<name>' : undefined}
                />
                <TextInput
                  label="To hub"
                  value={l.hub.name}
                  onChange={(e) => {
                    const links = [...value.links];
                    links[i] = { ...l, hub: { ...l.hub, name: e.currentTarget.value } };
                    set({ links });
                  }}
                  error={!/^hub_[a-z0-9_]{1,58}$/.test(l.hub.name) ? 'hub_<name>' : undefined}
                />
                <TagsInput
                  label="Its key column(s)"
                  data={columns}
                  value={Object.values(l.hub.keys)}
                  onChange={(cols) => {
                    const links = [...value.links];
                    links[i] = { ...l, hub: { ...l.hub, keys: keysOf(cols) } };
                    set({ links, attributes: value.attributes.filter((a) => !cols.includes(a)) });
                  }}
                />
                <ActionIcon variant="subtle" color="red" aria-label="Remove link" onClick={() => set({ links: value.links.filter((_, j) => j !== i) })}>
                  <IconTrash size={16} />
                </ActionIcon>
              </Group>
            ))}
            <Button
              size="xs"
              variant="light"
              w="fit-content"
              leftSection={<IconPlus size={14} />}
              onClick={() =>
                set({ links: [...value.links, { name: `link_${entity}_`, hub: { name: 'hub_', keys: {} } }] })
              }
            >
              Add relationship
            </Button>
          </Stack>
        </Paper>
      )}
    </Stack>
  );
}

export function rawVaultValid(v: RawVaultSpec | null | undefined): boolean {
  if (!v) return true;
  return (
    /^hub_[a-z0-9_]{1,58}$/.test(v.hub.name) &&
    Object.keys(v.hub.keys).length > 0 &&
    v.links.every((l) => /^link_[a-z0-9_]{1,57}$/.test(l.name) && /^hub_[a-z0-9_]{1,58}$/.test(l.hub.name) && Object.keys(l.hub.keys).length > 0)
  );
}
