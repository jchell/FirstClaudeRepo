import { expect, test, type Page } from '@playwright/test';

const ADMIN = process.env.DATAPLAT_ADMIN_USER ?? 'admin';
const PASSWORD = process.env.DATAPLAT_ADMIN_PASSWORD ?? '';

async function signIn(page: Page, username = ADMIN, password = PASSWORD, expectSuccess = true) {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Username').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  if (expectSuccess) await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
}

test.beforeAll(() => {
  if (!PASSWORD) throw new Error('Set DATAPLAT_ADMIN_PASSWORD to the admin password printed by `init`.');
});

test('wrong password shows a generic error', async ({ page }) => {
  await signIn(page, ADMIN, 'definitely-not-the-password', false);
  await expect(page.getByRole('alert')).toHaveText('Incorrect username or password.');
});

test('admin signs in, sees healthy components and the full navigation, and the session survives reload', async ({
  page,
}) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
  for (const name of ['secret_store', 'metadata_store', 'object_store', 'query_engine', 'event_bus', 'knowledge_graph']) {
    await expect(page.getByTestId(`component-${name}`).getByLabel('healthy')).toBeVisible();
  }
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  for (const label of ['Connections', 'Ingestion Jobs', 'Streams & Replication', 'Lineage', 'Ontology Studio', 'Admin']) {
    await expect(nav.getByRole('link', { name: new RegExp(label) })).toBeVisible();
  }

  // Access token lives only in memory; a reload restores it from the httpOnly refresh cookie.
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toMatch(/eyJ/);
  expect(await page.evaluate(() => document.cookie)).toBe(''); // refresh cookie is httpOnly
});

test('admin creates a user; the viewer cannot reach Admin', async ({ page, browser }) => {
  const username = `viewer${Date.now() % 100000}`;
  await signIn(page);
  await page.getByRole('link', { name: /Admin/ }).click();
  await page.getByRole('button', { name: 'New user' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Username').fill(username);
  await dialog.getByLabel('Initial password').fill('viewer-password-123');
  await dialog.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByRole('cell', { name: username, exact: true })).toBeVisible();

  const viewer = await (await browser.newContext()).newPage();
  await signIn(viewer, username, 'viewer-password-123');
  await expect(viewer.getByRole('heading', { name: 'Operations' })).toBeVisible();
  await expect(viewer.getByRole('link', { name: /Admin/ })).toHaveCount(0);
  await viewer.goto('/admin/users');
  await expect(viewer).toHaveURL(/\/$/);
});

test('service account secrets are write-only in the console', async ({ page }) => {
  const name = `e2e-${Date.now() % 100000}`;
  await signIn(page);
  await page.goto('/admin/service-accounts');
  await page.getByRole('button', { name: 'New service account' }).click();
  await page.getByRole('dialog').getByLabel('Name').fill(name);
  await page.getByRole('dialog').getByRole('button', { name: 'Create' }).click();
  await expect(page.getByText(`sa-${name}`)).toBeVisible();

  await page.getByRole('row', { name: new RegExp(name) }).getByRole('button', { name: 'Secrets' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Secret').fill('sftp');
  await dialog.getByLabel('Value').fill('e2e-super-secret-value');
  await dialog.getByRole('button', { name: 'Save to Vault' }).click();
  await expect(dialog.getByText(`vault://kv/dataplat/service-accounts/${name}/sftp#password`)).toBeVisible();
  await expect(dialog.getByText('v1')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('e2e-super-secret-value');
  await expect(dialog.getByLabel('Value')).toHaveValue('');
});

test('logout returns to the login page', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Account menu' }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
});
