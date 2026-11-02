import { createRequire } from "node:module";
import { writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ensureTransferFixtureBundleDir, getTransferFixtureBundleFile } from "./transfer-fixture-paths.cjs";

const require = createRequire(import.meta.url);
const { build } = require("esbuild");
const fixtureDir = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(fixtureDir, "../..");
ensureTransferFixtureBundleDir();
for (const entry of ["admin-restart-entry.ts", "transfer-download-entry.ts"]) {
  const output = await build({
    entryPoints: [resolve(fixtureDir, entry)],
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
    target: "es2022",
    jsx: "automatic",
    alias: { "@": resolve(frontendDir, "src") },
    define: {
      "process.env.NEXT_PUBLIC_BASE_PATH": '""',
      "process.env.NODE_ENV": '"production"',
    },
    treeShaking: true,
  });
  const name = entry.replace(/\.ts$/u, ".js");
  await writeFile(getTransferFixtureBundleFile(name), output.outputFiles[0].contents);
}
process.stdout.write("Browser fixture bundles ready in OS temp.\n");
