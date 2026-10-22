import { defineConfig } from "@playwright/test";

// This shader regression needs neither the application server nor QA accounts.
export default defineConfig({
  testDir: "./e2e",
  testMatch: "chat-background-renderer.spec.ts",
  outputDir: "../.local/wave-closure-20261017/renderer-test-results",
  reporter: "line",
  workers: 1,
  use: {
    browserName: "chromium",
    channel: process.env.PLAYWRIGHT_RENDERER_CHANNEL ?? (process.platform === "win32" ? "msedge" : undefined),
    headless: true,
    launchOptions: {
      args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    },
  },
});
