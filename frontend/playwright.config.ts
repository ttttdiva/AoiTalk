import { defineConfig } from "@playwright/test";

const host = process.env.PLAYWRIGHT_HOST ?? "127.0.0.1";
const port = process.env.PLAYWRIGHT_PORT ?? "3002";
const baseURL = `http://${host}:${port}`;
const nextDistDir =
  process.env.PLAYWRIGHT_NEXT_DIST_DIR ??
  `.next-playwright-${port.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
const reuseExistingServer =
  process.env.PLAYWRIGHT_REUSE_EXISTING_SERVER === "0"
    ? false
    : !process.env.CI;

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/support/global-setup.ts",
  globalTeardown: "./e2e/support/global-teardown.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: "html",
  use: {
    baseURL,
    trace: "on-first-retry",
    // Next dev compiles each App Router entry lazily.  A clean CI worker can
    // legitimately need more than Playwright's 5s assertion default before
    // the first Docs/Chat shell is interactive.
    actionTimeout: 15_000,
    navigationTimeout: 45_000,
  },
  webServer: {
    // CI verifies the production artifact contract before this job. Reuse
    // that canonical artifact instead of relying on cold App Router dev
    // compilation, which is both slower and a different runtime contract.
    command: `npm run start -- -H ${host} -p ${port}`,
    env: {
      NEXT_DIST_DIR: nextDistDir,
    },
    url: baseURL,
    reuseExistingServer,
    timeout: 120000,
  },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
  ],
});
