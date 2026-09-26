import { defineConfig } from '@playwright/test';

// Smoke tests against the running stack (make up / tasks.ps1 up).
//   DATAPLAT_ADMIN_PASSWORD=... npm run e2e
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: {
    baseURL: process.env.CONSOLE_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {},
  },
});
