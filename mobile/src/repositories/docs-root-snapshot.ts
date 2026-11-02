import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Crypto from "expo-crypto";
import { getConfiguredApiServerFingerprint } from "../lib/api-client";
import type { DocsNode } from "../types/api";

const STORAGE_PREFIX = "@aoitalk/docs-root-snapshot:v1:";
const SNAPSHOT_VERSION = 1;
const MAX_ROOTS = 10_000;

export type DocsRootSnapshotEntry = {
  id: string;
  title: string;
  sort_order: number | null;
};

type StoredDocsRootSnapshot = {
  version: number;
  scope_hash: string;
  saved_at: number;
  roots: DocsRootSnapshotEntry[];
};

async function getScopeHash(userId: string): Promise<string> {
  const serverFingerprint = await getConfiguredApiServerFingerprint();
  return Crypto.digestStringAsync(
    Crypto.CryptoDigestAlgorithm.SHA256,
    `${serverFingerprint}\n${userId}`,
  );
}

function parseRootEntries(value: unknown): DocsRootSnapshotEntry[] | null {
  if (!Array.isArray(value) || value.length > MAX_ROOTS) return null;
  const roots: DocsRootSnapshotEntry[] = [];
  for (const candidate of value) {
    if (!candidate || typeof candidate !== "object") return null;
    const row = candidate as Record<string, unknown>;
    if (
      typeof row.id !== "string" || row.id.length === 0
      || typeof row.title !== "string"
      || !(row.sort_order == null || typeof row.sort_order === "number")
    ) return null;
    roots.push({
      id: row.id,
      title: row.title,
      sort_order: row.sort_order == null ? null : row.sort_order as number,
    });
  }
  return roots;
}

/**
 * Read only the title/id/order projection saved for this user and API server.
 * The key and payload contain no token, body, or raw account identifier.
 */
export async function readDocsRootSnapshot(
  userId: string | null | undefined,
): Promise<DocsRootSnapshotEntry[] | null> {
  if (!userId) return null;
  try {
    const scopeHash = await getScopeHash(userId);
    const raw = await AsyncStorage.getItem(`${STORAGE_PREFIX}${scopeHash}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredDocsRootSnapshot>;
    if (parsed.version !== SNAPSHOT_VERSION || parsed.scope_hash !== scopeHash) return null;
    return parseRootEntries(parsed.roots);
  } catch {
    return null;
  }
}

/** Persist a small, body-free projection after a successful scoped local read. */
export async function saveDocsRootSnapshot(
  userId: string | null | undefined,
  pages: readonly DocsNode[],
): Promise<void> {
  if (!userId || pages.length > MAX_ROOTS) return;
  const scopeHash = await getScopeHash(userId);
  const roots = parseRootEntries(pages.map((page) => ({
    id: page.id,
    title: page.title,
    sort_order: page.sort_order,
  })));
  if (!roots) return;
  const payload: StoredDocsRootSnapshot = {
    version: SNAPSHOT_VERSION,
    scope_hash: scopeHash,
    saved_at: Date.now(),
    roots,
  };
  await AsyncStorage.setItem(`${STORAGE_PREFIX}${scopeHash}`, JSON.stringify(payload));
}

/** Build a display-only node; callers replace it with SQLite rows when ready. */
export function docsRootSnapshotNodes(roots: readonly DocsRootSnapshotEntry[]): DocsNode[] {
  return roots.map((root) => ({
    id: root.id,
    workspace_id: null,
    parent_id: null,
    root_page_id: root.id,
    project_id: null,
    source: null,
    access: "read",
    read_only: true,
    system_key: null,
    is_explicit_blank: false,
    canonical_title: null,
    lifecycle: null,
    title: root.title,
    aliases: [],
    description: null,
    body_json: null,
    body_text: null,
    node_type: "node",
    display_props: null,
    query_json: null,
    view_json: null,
    day_date: null,
    sort_order: root.sort_order,
    created_by: null,
    updated_by: null,
    created_at: null,
    updated_at: null,
    archived_at: null,
  }));
}
