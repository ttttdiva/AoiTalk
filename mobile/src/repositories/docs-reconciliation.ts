export type DocsNodeRef = {
  id: string;
  parentId: string | null;
};

/** dirty/outbox node を残す時に、SQLite cascade で消えないよう祖先も保護する。 */
export function expandProtectedDocsNodeAncestors(
  staleRows: DocsNodeRef[],
  directlyProtectedIds: Iterable<string>,
): Set<string> {
  const staleById = new Map(staleRows.map((row) => [row.id, row]));
  const protectedIds = new Set(directlyProtectedIds);
  for (const id of [...protectedIds]) {
    let current = staleById.get(id);
    const visited = new Set<string>();
    while (current?.parentId && !visited.has(current.parentId)) {
      visited.add(current.parentId);
      const parent = staleById.get(current.parentId);
      if (!parent) break;
      protectedIds.add(parent.id);
      current = parent;
    }
  }
  return protectedIds;
}

/** SQLite側に自己FK/cascadeはないため、保護対象以外のstale nodeを全件返す。 */
export function docsNodeDeletionIds(
  staleRows: DocsNodeRef[],
  protectedIds: ReadonlySet<string>,
): string[] {
  return staleRows.filter((row) => !protectedIds.has(row.id)).map((row) => row.id);
}
