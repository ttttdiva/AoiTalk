"use client";

import { readCachedSnapshot, writeCachedSnapshot } from "@/lib/persistent-cache";
import type { DocsNode } from "./types";

export type DocsLocationScope = {
  actorId: string | null | undefined;
  source: string;
  // The bootstrap library identifies the browsing context. A readable shared
  // page may belong to another library; its own id is verified again on resume.
  libraryId: string | null;
};

export type DocsLastLocation = { nodeId: string; libraryId: string };

export function docsLastLocationKey(scope: DocsLocationScope): string | null {
  if (!scope.actorId || !scope.libraryId) return null;
  return `docs/last-location/${[scope.actorId, scope.source, scope.libraryId].map(encodeURIComponent).join("/")}`;
}

export async function readDocsLastLocation(scope: DocsLocationScope): Promise<DocsLastLocation | null> {
  const key = docsLastLocationKey(scope);
  if (!key) return null;
  const value = await readCachedSnapshot<DocsLastLocation>(key);
  return value && typeof value.nodeId === "string" && value.nodeId.trim()
    && typeof value.libraryId === "string" && value.libraryId.trim()
    ? { nodeId: value.nodeId, libraryId: value.libraryId }
    : null;
}

/** Only call with a node from a successful, current, authorized API response. */
export function writeDocsLastLocation(scope: DocsLocationScope, node: DocsNode | undefined): Promise<void> {
  const key = docsLastLocationKey(scope);
  if (!key || !node?.id || !node.docs_library_id || node.archived_at) return Promise.resolve();
  return writeCachedSnapshot(key, { nodeId: node.id, libraryId: node.docs_library_id });
}
