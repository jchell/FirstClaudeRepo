import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

// Phase 3 smoke: a steward reviews a PII suggestion and adds a masking policy; an
// analyst's preview and query are masked; a DQ rule and scorecard run and chart.
const ADMIN = process.env.DATAPLAT_ADMIN_USER ?? 'admin';
const PASSWORD = process.env.DATAPLAT_ADMIN_PASSWORD ?? '';
const SUFFIX = `${Date.now() % 100000}`;
const DATASET = `ui_gov_customers_${SUFFIX}`;
const STEWARD = `ui-stu-${SUFFIX}`;
const ANALYST = `ui-ana-${SUFFIX}`;
const PW = `Pw-${SUFFIX}-${Math.random().toString(36).slice(2, 10)}-x`;
const RULE = `ui.${SUFFIX}.email_format`;
const CARD = `UI customers ${SUFFIX}`;
let datasetId = '';

test.describe.configure({ mode: 'serial' });

async function signIn(page: Page, user: string) {
  await page.goto('/');
  await page.getByLabel('Username').fill(user);
  await page.locator('input[autocomplete="current-password"]').fill(PW);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
}

async function pick(page: Page, label: string | RegExp, option: string | RegExp) {
  await page.getByRole('textbox', { name: label }).click();
  await page.getByRole('option', { name: option }).first().click();
}

async function adminApi(request: APIRequestContext) {
  const login = await request.post('/api/auth/login', { data: { username: ADMIN, password: PASSWORD } });
  return { Authorization: `Bearer ${(await login.json()).access_token}` };
}

test.beforeAll(async ({ request }) => {
  test.setTimeout(150_000);
  const headers = await adminApi(request);
  for (const [username, role] of [
    [STEWARD, 'steward'],
    [ANALYST, 'analyst'],
  ]) {
    expect((await request.post('/api/admin/users', { headers, data: { username, password: PW, roles: [role] } })).status()).toBe(201);
  }
  const sa = `ui-gov-${SUFFIX}`;
  expect((await request.post('/api/admin/service-accounts', { headers, data: { name: sa } })).status()).toBe(201);
  const conn = await request.post('/api/connections', {
    headers,
    data: {
      name: `ui-gov-crm-${SUFFIX}`,
      type: 'postgres',
      service_account: sa,
      config: { host: 'src-postgres', database: 'sales', username: 'dev' },
      secrets: { password: 'devsource' },
    },
  });
  const job = await request.post('/api/ingestion/jobs', {
    headers,
    data: {
      name: `ui_gov_${SUFFIX}`,
      connection_id: (await conn.json()).id,
      spec: { source: { object: 'crm.customers' }, load_mode: 'full', target: { layer: 'bronze', dataset: DATASET } },
    },
  });
  expect(job.status()).toBe(201);
  expect((await request.post(`/api/ingestion/jobs/${(await job.json()).id}/run`, { headers })).status()).toBe(202);
  await expect(async () => {
    const r = await request.get(`/api/catalog/datasets?q=${DATASET}`, { headers });
    const found = (await r.json()).find((d: { name: string }) => d.name === DATASET);
    expect(found).toBeTruthy();
    datasetId = found.id;
    // the load queues a PII scan; wait for its suggestions
    const s = await request.get(`/api/governance/assignments?dataset_id=${datasetId}&status=suggested`, { headers });
    expect((await s.json()).some((a: { column: string }) => a.column === 'email')).toBeTruthy();
  }).toPass({ timeout: 120_000 });
});

test.afterAll(async ({ request }) => {
  // The masking policy is global (every pii.email column): don't leave it behind.
  const headers = await adminApi(request);
  const policies = await (await request.get('/api/governance/masking', { headers })).json();
  for (const p of policies.filter((x: { name: string }) => x.name === `ui-mask-${SUFFIX}`)) {
    await request.delete(`/api/governance/masking/${p.id}`, { headers });
  }
});

test('a steward accepts a PII suggestion and masks it; analysts see masked values', async ({ page, browser }) => {
  await signIn(page, STEWARD);
  await page.getByRole('link', { name: /Governance/ }).click();
  const row = page.getByTestId(`suggestion-bronze.${DATASET}.email`);
  await expect(row).toContainText('pii.email');
  await row.getByRole('button', { name: /Accept/ }).click();
  await page.locator('label').filter({ hasText: /^Accepted$/ }).click();
  await expect(page.getByTestId(`suggestion-bronze.${DATASET}.email`)).toBeVisible();

  await page.getByRole('tab', { name: 'Masking & row filters' }).click();
  await page.getByRole('textbox', { name: 'Name' }).first().fill(`ui-mask-${SUFFIX}`);
  await pick(page, 'Tag', 'pii.email');
  await pick(page, 'Method', /Partial/);
  await page.getByRole('button', { name: 'Add policy' }).click();
  await expect(page.getByRole('cell', { name: `ui-mask-${SUFFIX}`, exact: true })).toBeVisible();

  const analyst = await browser.newPage();
  await signIn(analyst, ANALYST);
  await analyst.goto(`/catalog/${datasetId}`);
  await expect(analyst.getByText(/email masked/)).toBeVisible();
  await analyst.getByRole('tab', { name: 'Preview' }).click();
  await analyst.getByRole('button', { name: 'Show the first 50 rows' }).click();
  const cells = analyst.getByRole('cell', { name: /@/ });
  await expect(analyst.getByRole('cell', { name: /^\*+/ }).first()).toBeVisible();
  expect(await cells.count()).toBe(0);

  await analyst.getByRole('tab', { name: 'Query' }).click();
  await analyst.getByRole('textbox', { name: 'SQL query' }).fill(`select email from {{ source('bronze', '${DATASET}') }} limit 3`);
  await analyst.getByRole('button', { name: 'Run query' }).click();
  await expect(analyst.getByRole('cell', { name: /^\*+/ }).first()).toBeVisible({ timeout: 60_000 });
  expect(await analyst.getByRole('cell', { name: /@/ }).count()).toBe(0);
  await analyst.close();
});

test('a steward adds a DQ rule and a scorecard; the run shows a score and a trend', async ({ page }) => {
  await signIn(page, STEWARD);
  await page.getByRole('link', { name: /Data Quality/ }).click();
  await page.getByRole('button', { name: 'New rule' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox', { name: 'Name' }).fill(RULE);
  await pick(page, 'Dataset', `bronze.${DATASET}`);
  await pick(page, 'Rule', /Pattern/);
  await pick(page, 'Column', /^email$/);
  await dialog.getByRole('textbox', { name: 'Pattern' }).fill('^[^@]+@[^@]+$');
  await dialog.getByRole('button', { name: 'Save' }).click();
  await expect(dialog).toBeHidden();

  await page.getByRole('button', { name: 'New scorecard' }).click();
  const card = page.getByRole('dialog');
  await card.getByRole('textbox', { name: 'Name' }).fill(CARD);
  await card.getByRole('textbox', { name: 'Rules' }).click();
  await page.getByRole('option', { name: new RegExp(RULE.replace(/\./g, '\\.')) }).click();
  await card.getByRole('textbox', { name: 'Description' }).click();
  await card.getByRole('button', { name: 'Save' }).click();

  const row = page.getByTestId(`scorecard-${CARD}`);
  await row.getByRole('link', { name: CARD }).or(row.getByText(CARD)).first().click();
  const drawer = page.getByRole('dialog');
  await drawer.getByRole('button', { name: 'Run now' }).click();
  await expect(drawer.getByText('100.0').first()).toBeVisible({ timeout: 60_000 });
  await expect(drawer.getByRole('img', { name: /Score per run/ })).toBeVisible();
  await expect(drawer.getByRole('img', { name: /dimension/ })).toBeVisible();
  await page.keyboard.press('Escape');

  await page.getByRole('tab', { name: 'Rules' }).click();
  await expect(page.getByTestId(`rule-${RULE}`).getByText('passed')).toBeVisible();
});
