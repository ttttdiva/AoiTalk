import "server-only";

/**
 * @deprecated Legacy process/.env adapter retained only for migrations.
 * Production HF integrations use `user-store.ts` and never read or write
 * process-wide HF tokens.  Do not call this module from request handlers.
 */

import { promises as fs } from "node:fs";
import fsSync from "node:fs";
import path from "node:path";
import type { RepoType } from "./client";
import { updateEnvText } from "./env-text";

export { updateEnvText } from "./env-text";

const REFERENCES_KEY = "HF_REFERENCE_REPOS";

export interface HfReferenceRepo {
  repoId: string;
  repoType: RepoType;
  accountId?: string;
}

let writeQueue: Promise<void> = Promise.resolve();

function repoRoot(): string {
  let current = path.resolve(process.cwd());
  for (;;) {
    if (
      fsSync.existsSync(path.join(current, "pyproject.toml")) &&
      fsSync.existsSync(path.join(current, "frontend", "package.json"))
    ) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  throw new Error("AoiTalkリポジトリのルートを特定できません");
}

export function hfEnvPath(): string {
  return path.join(repoRoot(), ".env");
}

async function persistEnv(createUpdates: () => Record<string, string>): Promise<void> {
  const envPath = hfEnvPath();
  const run = async () => {
    const updates = createUpdates();
    const source = await fs.readFile(envPath, "utf8").catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return "";
      throw error;
    });
    const content = updateEnvText(source, updates);
    const tempPath = `${envPath}.hf-${process.pid}-${Date.now()}.tmp`;
    const existing = await fs.stat(envPath).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return null;
      throw error;
    });
    const mode = existing?.mode ?? 0o600;
    await fs.writeFile(tempPath, content, { encoding: "utf8", flag: "wx", mode });
    try {
      await fs.rename(tempPath, envPath);
      await fs.chmod(envPath, mode);
    } finally {
      await fs.rm(tempPath, { force: true }).catch(() => undefined);
    }
    for (const [key, value] of Object.entries(updates)) process.env[key] = value;
  };
  const queued = writeQueue.then(run, run);
  writeQueue = queued.catch(() => undefined);
  await queued;
}

export function listReferenceRepos(): HfReferenceRepo[] {
  const raw = process.env[REFERENCES_KEY];
  if (!raw) return [];
  try {
    const value = JSON.parse(raw) as unknown;
    if (!Array.isArray(value)) return [];
    const seen = new Set<string>();
    const result: HfReferenceRepo[] = [];
    for (const item of value) {
      if (!item || typeof item !== "object") continue;
      const row = item as Record<string, unknown>;
      const repoId = typeof row.repoId === "string" ? row.repoId.trim() : "";
      const repoType = row.repoType === "dataset" ? "dataset" : row.repoType === "model" ? "model" : null;
      const accountId = typeof row.accountId === "string" && row.accountId ? row.accountId : undefined;
      if (!repoId || !repoId.includes("/") || !repoType) continue;
      const key = `${repoType}:${repoId.toLowerCase()}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.push({ repoId, repoType, accountId });
    }
    return result;
  } catch {
    return [];
  }
}

export async function saveHfToken(username: string, token: string): Promise<void> {
  void username;
  void token;
  throw new Error("HFのグローバル環境変数資格情報は廃止されています。ユーザー設定から登録してください。");
}

export async function addReferenceRepos(entries: HfReferenceRepo[]): Promise<void> {
  await persistEnv(() => {
    const current = listReferenceRepos();
    const byKey = new Map(
      current.map((entry) => [`${entry.repoType}:${entry.repoId.toLowerCase()}`, entry]),
    );
    for (const entry of entries) {
      byKey.set(`${entry.repoType}:${entry.repoId.toLowerCase()}`, entry);
    }
    return { [REFERENCES_KEY]: JSON.stringify([...byKey.values()]) };
  });
}
