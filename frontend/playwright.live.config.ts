import { defineConfig } from "@playwright/test";

const baseURL = "http://127.0.0.1:3002";

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: false,
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
  // Live specs require the already-running app (Next + Python). Do not spawn
  // `next start` here: that process has no Python proxy and hides the
  // boundaries these tests exist to catch.
  webServer: {
    command:
      "node -e \"console.error('live Playwright requires an existing server at http://127.0.0.1:3002'); process.exit(1)\"",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 10_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
