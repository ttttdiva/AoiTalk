import { defineConfig } from "@playwright/test";
import { mkdtempSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

function isInsideOrEqual(parent: string, candidate: string): boolean {
  const rel = relative(parent, candidate);
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

function createRendererOutputDir(): string {
  // Playwright's CommonJS config loader anchors this file in frontend/.
  const repositoryRoot = resolve(__dirname, "..");
  const tempRoot = resolve(tmpdir());
  if (isInsideOrEqual(repositoryRoot, tempRoot)) {
    throw new Error(`OS temp directory is inside the repository: ${tempRoot}`);
  }

  const canonicalRepositoryRoot = realpathSync(repositoryRoot);
  const canonicalTempRoot = realpathSync(tempRoot);
  if (isInsideOrEqual(canonicalRepositoryRoot, canonicalTempRoot)) {
    throw new Error(`OS temp directory resolves inside the repository: ${canonicalTempRoot}`);
  }
  return mkdtempSync(join(canonicalTempRoot, "aoitalk-renderer-test-results-"));
}

const outputDir = createRendererOutputDir();

// This shader regression needs neither the application server nor QA accounts.
export default defineConfig({
  testDir: "./e2e",
  testMatch: "chat-background-renderer.spec.ts",
  outputDir,
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
