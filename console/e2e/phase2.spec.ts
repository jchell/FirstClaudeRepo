import { expect, test, type Page } from '@playwright/test';

// Phase 2 smoke: wizard "Add to Raw Vault" -> Data Vault page -> SCD2 model -> pipeline ->
// Lineage Explorer column trace and impact analysis. Needs the dev sources, seeded.
const ADMIN = process.env.DATAPLAT_ADMIN_USER ?? 'admin';
const PASSWORD = process.env.DATAPLAT_ADMIN_PASSWORD ?? '';
const SUFFIX = `${Date.now() % 100000}`;
const SA = `ui-dv-${SUFFIX}`;
const CONN = `ui-dv-crm-${SUFFIX}`;
const DATASET = `ui_dv_customers_${SUFFIX}`;
const HUB = `hub_ui${SUFFIX}`;
const SAT = `sat_ui${SUFFIX}_details`;
const DIM = `dim_ui${SUFFIX}`;
const PIPE = `ui-dims-${SUFFIX}`;
// This spec signs in as its own engineer, so it doesn't use up the admin's login attempts.
const ENGINEER = `ui-eng-${SUFFIX}`;
const ENG_PASSWORD = `Eng-${SUFFIX}-${Math.random().toString(36).slice(2, 10)}-pw`;

test.describe.configure({ mode: 'serial' });

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Username').fill(ENGINEER);
  await page.locator('input[autocomplete="current-password"]').fill(ENG_PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
}

async function pick(page: Page, label: string | RegExp, option: string | RegExp) {
  await page.getByRole('textbox', { name: label }).click();
  await page.getByRole('option', { name: option }).first().click();
}

test.beforeAll(async ({ request }) => {
  const login = await request.post('/api/auth/login', { data: { username: ADMIN, password: PASSWORD } });
  const headers = { Authorization: `Bearer ${(await login.json()).access_token}` };
  expect((await request.post('/api/admin/service-accounts', { data: { name: SA }, headers })).status()).toBe(201);
  const u = await request.post('/api/admin/users', {
    headers,
    data: { username: ENGINEER, password: ENG_PASSWORD, roles: ['engineer'] },
  });
  expect(u.status()).toBe(201);
  const c = await request.post('/api/connections', {
    headers,
    data: {
      name: CONN,
      type: 'postgres',
      service_account: SA,
      config: { host: 'src-postgres', database: 'sales', username: 'dev' },
      secrets: { password: 'devsource' },
    },
  });
  expect(c.status()).toBe(201);
});

test('the wizard adds a table to the raw vault and the hub and satellite load', async ({ page }) => {
  await signIn(page);
  await page.goto('/ingestion/new');
  await pick(page, 'Connection', new RegExp(CONN));
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByRole('button', { name: 'Browse source' }).click();
  await pick(page, 'Table', 'crm.customers');
  await page.getByRole('button', { name: 'Preview' }).click();
  await expect(page.getByRole('columnheader', { name: /first_name/ })).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByRole('radio', { name: 'Full' }).check();
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('textbox', { name: 'Bronze dataset' }).fill(DATASET);
  await page.getByLabel('Add to Raw Vault (hubs, links, satellites)').check();
  await page.getByRole('textbox', { name: 'Hub', exact: true }).fill(HUB);
  const keys = page.getByRole('textbox', { name: 'Business key column(s)' });
  await keys.fill('id');
  await keys.press('Enter');
  await page.getByRole('button', { name: 'Use all other columns' }).click();
  await page.getByLabel('Promote to silver automatically').check();
  await page.getByRole('button', { name: 'Next' }).click();

  await page.getByRole('textbox', { name: 'Job name' }).fill(`ui_dv_${SUFFIX}`);
  await expect(page.getByText(`Raw vault: ${HUB}`)).toBeVisible();
  await page.getByRole('button', { name: 'Save and run now' }).click();
  await expect(page.getByRole('heading', { name: `ui_dv_${SUFFIX}` })).toBeVisible();

  await page.getByRole('link', { name: /Data Vault/ }).click();
  const hub = page.getByTestId(`vault-${HUB}`);
  await expect(hub).toBeVisible();
  // Rows appear once the ingestion run and the vault load it triggers have finished.
  await expect(async () => {
    await page.reload();
    await expect(page.getByTestId(`vault-${SAT}`).getByRole('cell').nth(3)).not.toHaveText('—', { timeout: 2000 });
  }).toPass({ timeout: 90_000 });
  await page.getByRole('link', { name: SAT }).or(page.getByText(SAT, { exact: true })).first().click();
  await expect(page.getByRole('dialog').getByText(`bronze.${DATASET}`).first()).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: 'Model diagram' }).click();
  await expect(page.locator('.react-flow__node').filter({ hasText: HUB })).toBeVisible();

  // "Promote to silver" keeps a silver copy, rebuilt by its pipeline after the load.
  await page.goto('/pipelines');
  const silver = page.getByTestId(`model-${DATASET}`);
  await expect(silver).toContainText(`bronze.${DATASET}`);
  await expect(async () => {
    await page.reload();
    await expect(silver.getByText('succeeded')).toBeVisible({ timeout: 2000 });
  }).toPass({ timeout: 60_000 });
});

test('an SCD2 dimension is previewed, built and run by an event-driven pipeline', async ({ page }) => {
  await signIn(page);
  await page.goto('/pipelines/models/new');
  await page.getByRole('textbox', { name: 'Name' }).fill(DIM);
  await pick(page, 'Layer', 'gold');
  await pick(page, 'Kind', 'SCD2 dimension');
  await pick(page, 'History source', `vault.${SAT}`);
  await page.getByRole('textbox', { name: 'Surrogate key column' }).fill('customer_sk');
  await page.getByRole('button', { name: 'Preview' }).click();
  await expect(page.getByRole('columnheader', { name: /customer_sk/ })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("cell", { name: `vault.${HUB}.id`, exact: true })).toBeVisible(); // column lineage: key from the hub
  await page.getByRole('button', { name: 'Save and build' }).click();
  await expect(page.getByRole('heading', { name: `gold.${DIM}` })).toBeVisible();
  await expect(page.getByText('succeeded').first()).toBeVisible({ timeout: 60_000 });

  await page.goto('/pipelines');
  await page.getByRole('tab', { name: 'Pipelines' }).click();
  await page.getByRole('button', { name: 'New pipeline' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox', { name: 'Name' }).fill(PIPE);
  await dialog.getByRole('textbox', { name: 'Models' }).click();
  await page.getByRole('option', { name: `gold.${DIM}` }).click();
  await dialog.getByRole('textbox', { name: 'Description' }).click(); // closes the dropdown
  await dialog.getByRole('textbox', { name: 'Run when these datasets get new data' }).click();
  await page.getByRole('option', { name: `vault.${SAT}` }).click();
  await dialog.getByRole('textbox', { name: 'Description' }).click();
  await dialog.getByRole('button', { name: 'Save' }).click();
  const row = page.getByTestId(`pipeline-${PIPE}`);
  await expect(row).toContainText(`vault.${SAT} changes`);
  await row.getByRole('button', { name: `Run ${PIPE}` }).click();
  await expect(row.getByText('succeeded')).toBeVisible({ timeout: 60_000 });
});

test('the Lineage Explorer traces a gold column back to the source and shows its impact', async ({ page }) => {
  await signIn(page);
  await page.goto('/lineage');
  await pick(page, 'Focus node', `Gold: ${DIM}`);
  // the gold dataset node (not the model job or the serving copy, whose labels also mention it)
  await page.locator('.react-flow__node').filter({ hasText: /^Gold/ }).filter({ hasText: DIM }).click();
  await page.getByRole('button', { name: 'Trace city upstream' }).click();
  await expect(page.getByText('Transformations along the path')).toBeVisible();
  await expect(page.getByRole('button', { name: new RegExp(`vault\\.load\\.bronze\\.${DATASET}`) })).toBeVisible();
  await expect(page.locator('.react-flow__node').filter({ hasText: 'crm.customers' })).toBeVisible();
  await page.getByRole('button', { name: 'Table-level graph' }).click();

  const bronze = page.locator('.react-flow__node').filter({ hasText: /^Bronze/ }).filter({ hasText: DATASET });
  await bronze.click();
  await page.getByRole('button', { name: 'Impact analysis' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('cell', { name: DIM, exact: true })).toBeVisible();
  await expect(dialog.getByRole('cell', { name: `model.${DIM}` })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Export CSV' })).toBeVisible();
});
