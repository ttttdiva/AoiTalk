export type DocsChildrenCursor = {
  sortOrder: number;
  itemId: string;
};

export const DEFAULT_DOCS_CHILD_PAGE_SIZE = 80;
export const MAX_DOCS_CHILD_PAGE_SIZE = 200;

export function docsChildPageSize(raw: string | null) {
  if (raw == null || raw.trim() === "") return DEFAULT_DOCS_CHILD_PAGE_SIZE;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) return DEFAULT_DOCS_CHILD_PAGE_SIZE;
  return Math.max(1, Math.min(MAX_DOCS_CHILD_PAGE_SIZE, Math.trunc(parsed)));
}

export function encodeDocsChildrenCursor(cursor: DocsChildrenCursor) {
  return Buffer.from(JSON.stringify(cursor), "utf8").toString("base64url");
}

export function decodeDocsChildrenCursor(raw: string | null): DocsChildrenCursor | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(Buffer.from(raw, "base64url").toString("utf8")) as Partial<DocsChildrenCursor>;
    if (
      typeof parsed.sortOrder !== "number"
      || !Number.isFinite(parsed.sortOrder)
      || typeof parsed.itemId !== "string"
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(parsed.itemId)
    ) {
      return null;
    }
    return { sortOrder: parsed.sortOrder, itemId: parsed.itemId };
  } catch {
    return null;
  }
}
