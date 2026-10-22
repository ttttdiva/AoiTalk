import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { readFileSync, statSync } from "node:fs";
import nextEnv from "@next/env";

const { loadEnvConfig, processEnv, updateInitialEnv } = nextEnv;

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const FRONTEND_DIR = resolve(dirname(SCRIPT_PATH), "..");
const REPOSITORY_ROOT = resolve(FRONTEND_DIR, "..");
const BUILD_CONFIG_ROOT_ENV = "AOITALK_BUILD_CONFIG_ROOT";
const NEXT_CLI_PATH = resolve(
  FRONTEND_DIR,
  "node_modules",
  "next",
  "dist",
  "bin",
  "next",
);

const quietEnvLogger = {
  info() {},
  warn() {},
  error() {
    throw new Error("Frontend build environment could not be loaded");
  },
};

function resolveBuildConfigRoot() {
  const requestedRoot = process.env[BUILD_CONFIG_ROOT_ENV]?.trim();
  if (!requestedRoot) return REPOSITORY_ROOT;
  if (!isAbsolute(requestedRoot)) {
    throw new Error(
      `${BUILD_CONFIG_ROOT_ENV} must be an absolute directory`,
    );
  }
  try {
    if (!statSync(requestedRoot).isDirectory()) {
      throw new Error("not a directory");
    }
  } catch {
    throw new Error(`${BUILD_CONFIG_ROOT_ENV} directory is unavailable`);
  }
  return resolve(requestedRoot);
}

function loadRepositoryFallbackEnvironment(projectRoot) {
  let contents;
  try {
    contents = readFileSync(join(projectRoot, ".env"), "utf8");
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return;
    throw new Error("Frontend build environment could not be loaded");
  }
  processEnv(
    [{ path: ".env", contents, env: {} }],
    projectRoot,
    quietEnvLogger,
    true,
  );
}

/**
 * Load the explicitly selected repository dotenv configuration into the build
 * child.  Explicit process environment values remain authoritative because
 * Next's loader only fills keys that are not already present.
 */
export function loadFrontendBuildEnvironment({ frontendDir = FRONTEND_DIR } = {}) {
  const projectRoot = resolveBuildConfigRoot();
  const isDevelopment = process.env.NODE_ENV === "development";
  // Keep Next's standard frontend dotenv precedence first.  This call is
  // forced so an inherited loader marker cannot skip the frontend pass.
  loadEnvConfig(frontendDir, isDevelopment, quietEnvLogger, true);
  // The selected repository root is a fallback only.  Preserve the explicit
  // process environment and frontend dotenv values in Next's initial cache
  // before loading the fallback root's single canonical .env file.
  updateInitialEnv(process.env);
  loadRepositoryFallbackEnvironment(projectRoot);

  const buildEnvironment = process.env;
  // This marker belongs to the loader process.  Let the Next child perform
  // its normal frontend-directory env pass after inheriting the repository
  // values above.
  delete buildEnvironment.__NEXT_PROCESSED_ENV;

  if (!buildEnvironment.NEXTAUTH_SECRET?.trim()) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return buildEnvironment;
}

export function runNextBuild(args = process.argv.slice(2)) {
  const buildEnvironment = loadFrontendBuildEnvironment();
  const result = spawnSync(
    process.execPath,
    [NEXT_CLI_PATH, "build", ...args],
    {
      cwd: FRONTEND_DIR,
      env: buildEnvironment,
      stdio: "inherit",
      windowsHide: true,
    },
  );

  if (result.error) {
    throw new Error("Next.js build could not be started");
  }
  return typeof result.status === "number" ? result.status : 1;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === resolve(SCRIPT_PATH)
) {
  try {
    process.exitCode = runNextBuild();
  } catch (error) {
    process.stderr.write(
      `${error instanceof Error ? error.message : "Frontend build failed"}\n`,
    );
    process.exitCode = 1;
  }
}
