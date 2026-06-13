/**
 * Huggingface Hub サーバーサイドクライアント
 * 60_huggingface-sync/src/lib/hf-client.ts を AoiTalk 用に移植
 */

import "server-only";
import {
  whoAmI,
  listModels,
  listDatasets,
  listFiles,
} from "@huggingface/hub";
import type { RepoDesignation } from "@huggingface/hub";
import {
  isRateLimitError,
  isNetworkError,
  parseRateLimitWait,
  errorToString,
} from "./api-utils";

export const HF_BASE_URL = "https://huggingface.co";
const MAX_RETRIES = 3;

export type RepoType = "model" | "dataset";

export interface HfUserInfo {
  id: string;
  name: string;
  fullname: string;
  avatarUrl: string;
}

export interface RepoInfo {
  id: string;
  name: string;
  owner: string;
  private: boolean;
  lastModified: string;
  type: RepoType;
  description?: string;
}

export interface FileEntry {
  path: string;
  name: string;
  size?: number;
  type: "file" | "directory";
  lastModified?: string;
  oid?: string;
  xetHash?: string;
  lfs?: {
    oid: string;
    size: number;
    pointerSize: number;
  };
}

export async function verifyToken(accessToken: string): Promise<HfUserInfo> {
  const info = await whoAmI({ accessToken });
  const infoAny = info as unknown as Record<string, unknown>;
  return {
    id: String(infoAny.id ?? ""),
    name: info.name,
    fullname: String(infoAny.fullname ?? info.name),
    avatarUrl: String(infoAny.avatarUrl ?? ""),
  };
}

export async function listUserRepos(
  accessToken: string,
  owner: string,
): Promise<RepoInfo[]> {
  const repos: RepoInfo[] = [];

  for await (const model of listModels({
    search: { owner },
    accessToken,
  })) {
    const any = model as unknown as Record<string, unknown>;
    const full = model.name; // "owner/name"
    const [modelOwner, ...rest] = full.split("/");
    repos.push({
      id: full,
      name: rest.join("/") || full,
      owner: modelOwner || owner,
      private: !!model.private,
      lastModified: model.updatedAt?.toString() ?? "",
      type: "model",
      description: any.description ? String(any.description) : undefined,
    });
  }

  for await (const dataset of listDatasets({
    search: { owner },
    accessToken,
  })) {
    const any = dataset as unknown as Record<string, unknown>;
    const full = dataset.name;
    const [dsOwner, ...rest] = full.split("/");
    repos.push({
      id: full,
      name: rest.join("/") || full,
      owner: dsOwner || owner,
      private: !!dataset.private,
      lastModified: dataset.updatedAt?.toString() ?? "",
      type: "dataset",
      description: any.description ? String(any.description) : undefined,
    });
  }

  repos.sort((a, b) => {
    const ta = a.lastModified ? new Date(a.lastModified).getTime() : 0;
    const tb = b.lastModified ? new Date(b.lastModified).getTime() : 0;
    return tb - ta;
  });

  return repos;
}

export async function listRepoTree(
  accessToken: string | undefined,
  repoId: string,
  repoType: RepoType = "model",
  path?: string,
  options?: { recursive?: boolean; expand?: boolean },
): Promise<FileEntry[]> {
  const repo: RepoDesignation = { type: repoType, name: repoId };
  const entries: FileEntry[] = [];

  for await (const file of listFiles({
    repo,
    accessToken,
    path,
    recursive: options?.recursive,
    expand: options?.expand,
  })) {
    const segments = file.path.split("/");
    const name = segments[segments.length - 1] || file.path;
    entries.push({
      path: file.path,
      name,
      size: file.size,
      type: file.type === "directory" ? "directory" : "file",
      lastModified: file.lastCommit?.date
        ? new Date(file.lastCommit.date).toISOString()
        : undefined,
      oid: file.oid,
      xetHash: (file as unknown as { xetHash?: string }).xetHash,
      lfs: file.lfs
        ? {
            oid: file.lfs.oid,
            size: file.lfs.size,
            pointerSize: file.lfs.pointerSize,
          }
        : undefined,
    });
  }

  // directory を先、次に name 昇順
  entries.sort((a, b) => {
    if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return entries;
}

export function buildFileUrl(
  repoId: string,
  path: string,
  repoType: RepoType = "model",
  revision: string = "main",
): string {
  const typePrefix = repoType === "dataset" ? "datasets/" : "";
  const encodedPath = path
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `${HF_BASE_URL}/${typePrefix}${repoId}/resolve/${encodeURIComponent(revision)}/${encodedPath}`;
}

export function buildAuthHeaders(
  accessToken?: string,
): Record<string, string> {
  if (!accessToken) return {};
  return { Authorization: `Bearer ${accessToken}` };
}

export async function withRetry<T>(
  fn: () => Promise<T>,
  maxRetries: number = MAX_RETRIES,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (isRateLimitError(error) && attempt < maxRetries - 1) {
        const waitSec = parseRateLimitWait(errorToString(error));
        await sleep(Math.min(waitSec, 60) * 1000); // 上限1分（UI応答性確保）
        continue;
      }
      if (isNetworkError(error) && attempt < maxRetries - 1) {
        const backoff = Math.pow(2, attempt + 1) * 1000;
        await sleep(backoff);
        continue;
      }
      throw error;
    }
  }
  throw lastError;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
