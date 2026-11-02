import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, realpathSync } from "node:fs";
import { mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { findViolations } from "../../../scripts/check-docs-node-writes.mjs";

function createScratchRoot(repoRoot: string, tempRoot: string, prefix: string) {
  const isInside = (repo: string, candidate: string) => {
    const relative = path.relative(repo, candidate);
    return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
  };
  const repo = path.resolve(repoRoot);
  const temp = path.resolve(tempRoot);
  if (isInside(repo, temp) || isInside(realpathSync(repo), realpathSync(temp))) {
    throw new Error("Temporary directory must be outside the repository");
  }
  return mkdtempSync(path.join(temp, prefix));
}

const repoRoot = path.resolve(process.cwd(), "..");
let scratchRoot: string | null = null;

afterEach(async () => {
  if (scratchRoot) {
    await rm(scratchRoot, { recursive: true, force: true });
    scratchRoot = null;
  }
});

describe("Docs node write production guard", () => {
  it("ignores test fixtures but keeps rejecting production writes", async () => {
    scratchRoot = createScratchRoot(repoRoot, os.tmpdir(), "docs-node-write-gate-");
    const testsDir = path.join(scratchRoot, "src", "app", "__tests__");
    const productionDir = path.join(scratchRoot, "src", "app", "routes");
    await mkdir(testsDir, { recursive: true });
    await mkdir(productionDir, { recursive: true });

    await writeFile(
      path.join(testsDir, "route.test.ts"),
      'const fixture = { bodyText: "mocked data" };\n',
    );
    await writeFile(
      path.join(productionDir, "route.ts"),
      "const write = {\n  bodyText: requestBody,\n};\n",
    );

    const violations = findViolations(scratchRoot);

    expect(violations).toHaveLength(1);
    expect(path.basename(violations[0].filePath)).toBe("route.ts");
    expect(violations[0].kind).toBe("bodyText write");
  });

  it("ignores test suffixes outside __tests__ directories", async () => {
    scratchRoot = createScratchRoot(repoRoot, os.tmpdir(), "docs-node-write-gate-");
    const sourceDir = path.join(scratchRoot, "src", "app");
    await mkdir(sourceDir, { recursive: true });
    await writeFile(
      path.join(sourceDir, "route.spec.ts"),
      "const fixture = { bodyText: value };\n",
    );

    expect(findViolations(scratchRoot)).toEqual([]);
  });
});
