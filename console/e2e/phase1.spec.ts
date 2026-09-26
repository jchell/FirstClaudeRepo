import { expect, test, type Page } from '@playwright/test';

// Needs the dev sources (docker-compose.dev.yml) running and seeded.
const ADMIN = process.env.DATAPLAT_ADMIN_USER ?? 'admin';
const PASSWORD = process.env.DATAPLAT_ADMIN_PASSWORD ?? '';
const SUFFIX = `${Date.now() % 100000}`;
const SA = `ui-etl-${SUFFIX}`;
const CONN = `ui-crm-${SUFFIX}`;
const JOB = `ui_orders_${SUFFIX}`;
const DATASET = `ui_crm_orders_${SUFFIX}`;

test.describe.configure({ mode: 'serial' });

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
  const login = await request.post('/api/auth/login', { data: { username: ADMIN, password: PASSWORD } });
  const token = (await login.json()).access_token;
  const r = await request.post('/api/admin/service-accounts', {
    data: { name: SA, description: 'UI test' },
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(r.status()).toBe(201);
});

test('create a Postgres connection; its password goes to Vault and the test succeeds', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: /Connections/ }).click();
  await page.getByRole('button', { name: 'New connection' }).click();
  const dialog = page.getByRole('dialog');
  await pick(page, 'Type', 'PostgreSQL');
  await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill(CONN);
  await pick(page, 'Service account', SA);
  await dialog.getByRole('textbox', { name: 'Host' }).fill('src-postgres');
  await dialog.getByRole('textbox', { name: 'Database' }).fill('sales');
  await dialog.getByRole('textbox', { name: 'Username' }).fill('dev');
  await dialog.getByLabel('Password (stored in Vault)').fill('devsource');
  await dialog.getByRole('button', { name: 'Save and test' }).click();
  await expect(page.getByText('Connection works')).toBeVisible({ timeout: 30_000 });
  const row = page.getByRole('row', { name: new RegExp(CONN) });
  await expect(row.getByText(/^ok/)).toBeVisible();
  await expect(page.locator('body')).not.toContainText('devsource');
});

test('the wizard builds a job from a table pick and preview, and the run lands in bronze', async ({ page }) => {
  await signIn(page);
  await page.goto('/ingestion/new');
  await pick(page, 'Connection', new RegExp(CONN));
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('button', { name: 'Browse source' }).click();
  await pick(page, 'Table', 'crm.orders');
  await page.getByRole('button', { name: 'Preview' }).click();
  await expect(page.getByRole('columnheader', { name: /order_id/ })).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('radio', { name: 'Full' }).check();
  await page.getByRole('button', { name: 'Next' }).click();

  const dataset = page.getByRole('textbox', { name: 'Bronze dataset' });
  await dataset.fill(DATASET);
  await expect(page.getByText('Phase 2').first()).toBeVisible();
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('textbox', { name: 'Job name' }).fill(JOB);
  await expect(page.getByText(`bronze.${DATASET}`)).toBeVisible();
  await page.getByRole('button', { name: 'Save and run now' }).click();

  await expect(page.getByRole('heading', { name: JOB })).toBeVisible();
  await expect(page.getByText('succeeded').first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText('200 rows').first()).toBeVisible();
});

test('the catalog shows the dataset with profile, preview and lineage', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: /Catalog/ }).click();
  await page.getByRole('textbox', { name: 'Search the catalog' }).fill(DATASET);
  await page.getByRole('link', { name: DATASET }).click();
  await expect(page.getByRole('heading', { name: DATASET })).toBeVisible();
  await expect(page.getByRole('cell', { name: /^amount/ })).toBeVisible();
  await expect(page.getByRole('cell', { name: /_batch_id/ })).toBeVisible();

  await page.getByRole('tab', { name: 'Preview' }).click();
  await page.getByRole('button', { name: 'Show the first 50 rows' }).click();
  await expect(page.getByRole('columnheader', { name: /status/ })).toBeVisible();

  await page.getByRole('tab', { name: 'Lineage' }).click();
  await expect(page.locator('.react-flow__node').filter({ hasText: 'crm.orders' })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.react-flow__node').filter({ hasText: `ingest.${JOB}` })).toBeVisible();
});

test('a portal app linked to the dataset appears downstream in lineage', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: /App Portal/ }).click();
  await page.getByRole('button', { name: 'Add app' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox', { name: 'Name' }).fill(`Orders report ${SUFFIX}`);
  await dialog.getByRole('textbox', { name: 'URL' }).fill('https://bi.example.com/orders');
  await dialog.getByRole('textbox', { name: /Datasets it reads/ }).fill(`bronze.${DATASET}`);
  await dialog.getByRole('textbox', { name: /Datasets it reads/ }).press('Enter');
  await dialog.getByRole('button', { name: 'Save' }).click();
  const card = page.locator('.mantine-Card-root').filter({ hasText: `Orders report ${SUFFIX}` });
  await card.getByRole('link', { name: 'Lineage' }).click();
  await expect(page.locator('.react-flow__node').filter({ hasText: `Orders report ${SUFFIX}` })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.react-flow__node').filter({ hasText: DATASET })).toBeVisible();
});

test('the ops dashboard shows the run in its chart and job table', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('img', { name: /Ingestion runs per time bucket/ })).toBeVisible();
  await expect(page.getByRole('link', { name: JOB })).toBeVisible();
  await page.locator('label').filter({ hasText: /^Table$/ }).click();
  await expect(page.getByRole('columnheader', { name: 'Rows loaded' })).toBeVisible();
});
