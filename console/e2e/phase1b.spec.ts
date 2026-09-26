import { execFileSync } from 'node:child_process';

import { expect, test, type Page } from '@playwright/test';

// Needs the dev sources running (docker-compose.dev.yml). Source changes are made with
// `docker compose exec` against src-postgres, like an application writing to it would.
const ADMIN = process.env.DATAPLAT_ADMIN_USER ?? 'admin';
const PASSWORD = process.env.DATAPLAT_ADMIN_PASSWORD ?? '';
const SUFFIX = `${Date.now() % 100000}`;
const SA = `ui-cdc-${SUFFIX}`;
const CONN = `ui-pg-${SUFFIX}`;
const JOB = `ui_cdc_${SUFFIX}`;
const DATASET = `ui_cdc_orders_${SUFFIX}`;

test.describe.configure({ mode: 'serial' });

function psql(sql: string) {
  execFileSync('docker', ['compose', 'exec', '-T', 'src-postgres', 'psql', '-U', 'dev', '-d', 'sales', '-qc', sql], {
    cwd: '..',
    stdio: 'pipe',
  });
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Username').fill(ADMIN);
  await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
}

async function pick(page: Page, label: string | RegExp, option: string | RegExp) {
  await page.getByRole('textbox', { name: label }).click();
  await page.getByRole('option', { name: option }).first().click();
}

test.beforeAll(async ({ request }) => {
  const token = (await (await request.post('/api/auth/login', { data: { username: ADMIN, password: PASSWORD } })).json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  expect((await request.post('/api/admin/service-accounts', { data: { name: SA }, headers })).status()).toBe(201);
  const r = await request.post('/api/connections', {
    headers,
    data: { name: CONN, type: 'postgres', service_account: SA, config: { host: 'src-postgres', database: 'sales', username: 'dev' },
            secrets: { password: 'devsource' } },
  });
  expect(r.status()).toBe(201);
  psql(`drop table if exists crm.ui_orders_${SUFFIX}; create table crm.ui_orders_${SUFFIX} (id int primary key, amount int);` +
       `insert into crm.ui_orders_${SUFFIX} select g, g * 10 from generate_series(1, 5) g`);
});

test.afterAll(async ({ request }) => {
  const token = (await (await request.post('/api/auth/login', { data: { username: ADMIN, password: PASSWORD } })).json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  const jobs = (await (await request.get('/api/ingestion/jobs', { headers })).json()) as { id: string; name: string }[];
  for (const j of jobs.filter((x) => x.name === JOB)) await request.delete(`/api/ingestion/jobs/${j.id}`, { headers });
});

test('the wizard sets up CDC replication and the stream shows up live', async ({ page }) => {
  await signIn(page);
  await page.goto('/ingestion/new');
  await pick(page, 'Connection', new RegExp(CONN));
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByRole('button', { name: 'Browse source' }).click();
  await pick(page, 'Table', `crm.ui_orders_${SUFFIX}`);
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('radio', { name: /Real-time replication \(CDC\)/ }).check();
  await page.locator('label').filter({ hasText: /^Mirror \(current state\)$/ }).click();
  await page.getByRole('textbox', { name: /or every \(seconds\)/ }).fill('1');
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('textbox', { name: 'Bronze dataset' }).fill(DATASET);
  await expect(page.getByText('Runs continuously once saved')).toBeVisible();
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByRole('textbox', { name: 'Job name' }).fill(JOB);
  await expect(page.getByText('continuous')).toBeVisible();
  await page.getByRole('button', { name: 'Save and start' }).click();

  await expect(page.getByRole('heading', { name: 'Streams & Replication' })).toBeVisible();
  await expect(page.getByText('live')).toBeVisible();
  const row = page.getByTestId(`stream-${JOB}`);
  await expect(row.getByText('running')).toBeVisible({ timeout: 60_000 });
  await expect(row.getByRole('cell').nth(5)).toHaveText('5', { timeout: 60_000 }); // snapshot

  psql(`insert into crm.ui_orders_${SUFFIX} values (6, 60), (7, 70); update crm.ui_orders_${SUFFIX} set amount = 0 where id = 1`);
  await expect(row.getByRole('cell').nth(5)).toHaveText('8', { timeout: 20_000 }); // 5 + 3 changes
});

test('stream details show charts, and pause/resume work', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: /Streams & Replication/ }).click();
  const row = page.getByTestId(`stream-${JOB}`);
  await row.getByRole('button', { name: `Pause ${JOB}` }).click();
  await expect(row.getByText('paused')).toBeVisible({ timeout: 20_000 });
  await row.getByRole('button', { name: `Resume ${JOB}` }).click();
  await expect(row.getByText('running')).toBeVisible({ timeout: 30_000 });

  await row.getByText(JOB).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByRole('img', { name: 'Records ingested per minute' })).toBeVisible();
  await expect(drawer.getByRole('img', { name: /End-to-end latency/ })).toBeVisible();
  await expect(drawer.getByText('RUNNING').first()).toBeVisible();
});

test('the mirror in the catalog reflects the source', async ({ page }) => {
  await signIn(page);
  await page.goto('/catalog');
  await page.getByRole('textbox', { name: 'Search the catalog' }).fill(DATASET);
  await page.getByRole('link', { name: DATASET }).click();
  await page.getByRole('tab', { name: 'Preview' }).click();
  await page.getByRole('button', { name: 'Show the first 50 rows' }).click();
  await expect(page.getByRole('row')).toHaveCount(8); // header + 7 rows (mirror: one per key)
  await page.getByRole('tab', { name: 'Lineage' }).click();
  await expect(page.locator('.react-flow__node').filter({ hasText: `stream.${JOB}` })).toBeVisible({ timeout: 20_000 });
});
