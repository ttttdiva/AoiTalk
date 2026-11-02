import { defineConfig } from "@playwright/test";
import baseConfig from "./playwright.config";
import { createTransferFixtureOutputDir } from "./e2e/fixtures/transfer-fixture-paths.cjs";

/**
 * Isolated browser fixtures must not create/deactivate the shared E2E user in
 * a database. The selected specs use an intercepted origin and mock transfer
 * endpoints while bundling the production client functions.
 */
export default defineConfig({
  ...baseConfig,
  globalSetup: undefined,
  globalTeardown: undefined,
  webServer: undefined,
  testMatch: ["admin-restart-overlay.spec.ts", "transfer-compat-download.spec.ts"],
  outputDir: createTransferFixtureOutputDir(),
  reporter: "line",
  workers: 1,
});
