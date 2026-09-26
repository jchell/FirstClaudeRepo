import { Fieldset, NumberInput, PasswordInput, Select, Stack, Switch, Textarea, TextInput } from '@mantine/core';

import type { JsonSchema } from '../api/types';

/**
 * Renders a connector's config form from its JSON schema (pydantic). Secret fields
 * become password inputs whose values go to `secrets`, never into `config`.
 */
interface Props {
  schema: JsonSchema;
  root?: JsonSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  secrets: Record<string, string>;
  onSecretsChange: (v: Record<string, string>) => void;
  /** Secret fields that already hold a Vault reference (editing an existing connection). */
  storedSecrets?: Set<string>;
  prefix?: string;
}

function resolve(s: JsonSchema, root: JsonSchema): JsonSchema {
  const ref = s.$ref ?? s.allOf?.find((a) => a.$ref)?.$ref;
  if (ref) return { ...root.$defs?.[ref.split('/').pop()!], ...s, $ref: undefined, allOf: undefined };
  if (s.anyOf) {
    const nonNull = s.anyOf.filter((a) => a.type !== 'null');
    if (nonNull.length === 1) return { ...resolve(nonNull[0], root), ...s, anyOf: undefined, type: nonNull[0].type ?? 'object' };
  }
  return s;
}

const humanize = (k: string) => k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

export function SchemaForm({ schema, root = schema, value, onChange, secrets, onSecretsChange, storedSecrets, prefix = '' }: Props) {
  const secretFields = new Set(root['x-secret-fields'] ?? []);
  const required = new Set(schema.required ?? []);
  const set = (k: string, v: unknown) => {
    const next = { ...value };
    if (v === '' || v === undefined) delete next[k];
    else next[k] = v;
    onChange(next);
  };

  return (
    <Stack gap="sm">
      {Object.entries(schema.properties ?? {}).map(([key, raw]) => {
        const s = resolve(raw, root);
        const path = prefix + key;
        const label = s.title ?? humanize(key);
        const description = s.description;
        const isRequired = required.has(key);

        if (secretFields.has(path) || s['x-secret']) {
          const stored = storedSecrets?.has(path);
          return (
            <PasswordInput
              key={path}
              label={`${label} (stored in Vault)`}
              description={stored ? 'Leave empty to keep the stored value' : 'Written straight to Vault; never shown again'}
              placeholder={stored ? '•••••••• (stored)' : undefined}
              autoComplete="new-password"
              value={secrets[path] ?? ''}
              onChange={(e) => {
                const next = { ...secrets };
                if (e.currentTarget.value) next[path] = e.currentTarget.value;
                else delete next[path];
                onSecretsChange(next);
              }}
            />
          );
        }
        if (s.type === 'object' && s.properties) {
          return (
            <Fieldset key={path} legend={label}>
              <SchemaForm
                schema={s}
                root={root}
                value={(value[key] as Record<string, unknown>) ?? {}}
                onChange={(v) => set(key, v)}
                secrets={secrets}
                onSecretsChange={onSecretsChange}
                storedSecrets={storedSecrets}
                prefix={`${path}.`}
              />
            </Fieldset>
          );
        }
        if (s.enum) {
          return (
            <Select
              key={path}
              label={label}
              description={description}
              required={isRequired}
              data={s.enum.map(String)}
              value={(value[key] as string) ?? (s.default as string) ?? null}
              onChange={(v) => set(key, v ?? undefined)}
            />
          );
        }
        if (s.type === 'boolean') {
          return (
            <Switch
              key={path}
              label={label}
              description={description}
              checked={Boolean(value[key] ?? s.default)}
              onChange={(e) => set(key, e.currentTarget.checked)}
            />
          );
        }
        if (s.type === 'integer' || s.type === 'number') {
          return (
            <NumberInput
              key={path}
              label={label}
              description={description}
              required={isRequired}
              placeholder={s.default != null ? String(s.default) : undefined}
              value={(value[key] as number) ?? ''}
              onChange={(v) => set(key, v === '' ? undefined : Number(v))}
            />
          );
        }
        if (s.type === 'object') {
          // Free-form maps (headers, driver options): edit as JSON.
          return (
            <Textarea
              key={path}
              label={`${label} (JSON)`}
              description={description}
              autosize
              minRows={2}
              defaultValue={value[key] ? JSON.stringify(value[key], null, 2) : ''}
              onBlur={(e) => {
                try {
                  set(key, e.currentTarget.value.trim() ? JSON.parse(e.currentTarget.value) : undefined);
                } catch {
                  /* keep last valid value */
                }
              }}
            />
          );
        }
        return (
          <TextInput
            key={path}
            label={label}
            description={description}
            required={isRequired}
            placeholder={s.default != null ? String(s.default) : undefined}
            value={(value[key] as string) ?? ''}
            onChange={(e) => set(key, e.currentTarget.value)}
          />
        );
      })}
    </Stack>
  );
}
