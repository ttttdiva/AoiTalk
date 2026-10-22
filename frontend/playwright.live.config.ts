import { defineConfig } from "@playwright/test";

const baseURL =
  process.env.PLAYWRIGHT_LIVE_BASE_URL?.trim() || "http://127.0.0.1:3002";

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: false,
  globalSetup: "./e2e-live/support/global-setup.ts",
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "off",
    actionTimeout: 15_000,
    navigationTimeout: 45_000,
  },
  // Live specs require the already-running app (Next + Python). The global
  // setup performs a read-only connection probe through this exact origin and
  // fails closed when the existing process is missing or points at another
  // database. Do not spawn `next start` here: that process has no Python proxy
  // and hides the boundaries these tests exist to catch.
  webServer: {
    command:
      "node -e \"console.error('live Playwright requires an existing server at http://127.0.0.1:3002'); process.exit(1)\"",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 10_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
