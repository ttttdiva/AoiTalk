import { and, eq, ilike, or, sql } from "drizzle-orm";
import { knowledgeNodes, knowledgeSearchIndex } from "@/db/schema";

/** Hashtags are exact, ANDed filters; a hash inside a word stays ordinary text. */
export function parseDocsSearchQuery(input: string) {
  const tags: string[] = [];
  const textParts: string[] = [];
  let end = 0;
  const hashtagPattern = /(^|\s)#(?:"((?:\\.|[^"\\])+)"|'((?:\\.|[^'\\])+)'|([^\s"']+))/gu;
  for (const match of input.matchAll(hashtagPattern)) {
    const tag = (match[2] ?? match[3] ?? match[4] ?? "").replace(/\\([\\"'])/g, "$1").trim();
    if (!tag) continue;
    if (!tags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) tags.push(tag);
    textParts.push(input.slice(end, match.index).trim());
    end = match.index + match[0].length;
  }
  textParts.push(input.slice(end).trim());
  // Only normalize the gap where a filter was removed. Whitespace inside an
  // ordinary text fragment is literal searchable content (including code).
  return { text: textParts.filter(Boolean).join(" "), tags };
}

export function escapeDocsSearchLike(value: string) {
  return value.replace(/[\\%_]/g, "\\$&");
}

/** A tag definition must belong to the candidate's own library, even for shares. */
export function docsSearchTagCondition(value: string, exact = true) {
  const pattern = exact ? value : `%${escapeDocsSearchLike(value)}%`;
  return sql<boolean>`exists (
    select 1 from knowledge_node_supertags docs_search_nst
    inner join knowledge_supertags docs_search_tags
      on docs_search_tags.id = docs_search_nst.supertag_id
    where docs_search_nst.node_id = ${knowledgeNodes.id}
      and docs_search_tags.docs_library_id = ${knowledgeNodes.docsLibraryId}
      and ${exact
        ? sql`lower(docs_search_tags.name) = lower(${pattern})`
        : sql`docs_search_tags.name ilike ${pattern}`}
  )`;
}

function aliasCondition(pattern: string, exact = false) {
  return sql<boolean>`exists (
    select 1 from jsonb_array_elements_text(
      case when jsonb_typeof(${knowledgeNodes.aliases}::jsonb) = 'array'
        then ${knowledgeNodes.aliases}::jsonb else '[]'::jsonb end
    ) as docs_search_alias(value)
    where ${exact
      ? sql`lower(docs_search_alias.value) = lower(${pattern})`
      : sql`docs_search_alias.value ilike ${pattern}`}
  )`;
}

/** Used in the candidate query itself, before scanning or choosing a page. */
export function docsSearchTextConditions(text: string) {
  const contains = `%${escapeDocsSearchLike(text)}%`;
  const prefix = `${escapeDocsSearchLike(text)}%`;
  const exactTitle = sql<boolean>`lower(${knowledgeNodes.title}) = lower(${text})`;
  const prefixTitle = ilike(knowledgeNodes.title, prefix);
  const title = ilike(knowledgeNodes.title, contains);
  const exactAlias = aliasCondition(text, true);
  const prefixAlias = aliasCondition(prefix);
  const alias = aliasCondition(contains);
  const exactTag = docsSearchTagCondition(text);
  const tag = docsSearchTagCondition(text, false);
  return {
    match: or(title, alias, tag, ilike(knowledgeSearchIndex.bodyTextPlain, contains)),
    rank: sql<number>`case
      when ${exactTitle} then 0
      when ${prefixTitle} then 1
      when ${exactAlias} then 2
      when ${prefixAlias} then 3
      when ${title} then 4
      when ${alias} then 5
      when ${exactTag} then 6
      when ${tag} then 7
      else 8 end`,
  };
}

export const docsSearchIndexJoin = and(
  eq(knowledgeSearchIndex.nodeId, knowledgeNodes.id),
  eq(knowledgeSearchIndex.docsLibraryId, knowledgeNodes.docsLibraryId),
);

/** Keep snippets plain text. Never return the index or an encrypted body. */
export function docsSearchSnippet(body: string, text: string, maxLength = 160) {
  const plain = body.replace(/\s+/gu, " ").trim();
  if (plain.length <= maxLength) return plain;
  const match = text ? plain.toLowerCase().indexOf(text.toLowerCase()) : -1;
  const start = Math.max(0, match - Math.floor(maxLength / 3));
  const end = Math.min(plain.length, start + maxLength);
  return `${start > 0 ? "…" : ""}${plain.slice(start, end)}${end < plain.length ? "…" : ""}`;
}

/**
 * The public cursor counts only readable results. Scan from the beginning,
 * skipping that many authorized rows, until a full page plus one readable
 * lookahead exists. Raw candidate offsets would expose private match counts;
 * keep them request-local and never impose a fixed ACL overfetch cap.
 */
export async function collectDocsSearchPage<Candidate, Visible>({
  cursor,
  limit,
  batchSize = Math.max(100, limit + 1),
  fetchCandidates,
  resolveVisible,
}: {
  cursor: number;
  limit: number;
  batchSize?: number;
  fetchCandidates: (offset: number, size: number) => Promise<Candidate[]>;
  resolveVisible: (candidates: Candidate[]) => Promise<Array<Visible | null>>;
}): Promise<{ nodes: Visible[]; nextCursor: string | null }> {
  const nodes: Visible[] = [];
  let offset = 0;
  let skippedVisible = 0;
  for (;;) {
    const candidates = await fetchCandidates(offset, batchSize);
    if (candidates.length === 0) return { nodes, nextCursor: null };
    const visible = await resolveVisible(candidates);
    for (let index = 0; index < candidates.length; index += 1) {
      const node = visible[index];
      if (!node) continue;
      if (skippedVisible < cursor) {
        skippedVisible += 1;
        continue;
      }
      if (nodes.length === limit) return { nodes, nextCursor: String(cursor + nodes.length) };
      nodes.push(node);
    }
    offset += candidates.length;
    if (candidates.length < batchSize) return { nodes, nextCursor: null };
  }
}

type BreadcrumbNode = {
  id: string;
  docsLibraryId: string;
  parentId: string | null;
  title: string;
  archivedAt: unknown;
};

/** Batch each ancestor level and stop at ACL/lifecycle/library boundaries. */
export async function loadDocsSearchBreadcrumbs(
  nodes: BreadcrumbNode[],
  loadReadable: (ids: string[]) => Promise<Map<string, BreadcrumbNode>>,
) {
  const readable = new Map<string, BreadcrumbNode>();
  const visited = new Set(nodes.map((node) => node.id));
  let pending = nodes.map((node) => node.parentId).filter((id): id is string => Boolean(id));
  for (let depth = 0; depth < 512 && pending.length > 0; depth += 1) {
    const ids = [...new Set(pending)].filter((id) => !visited.has(id));
    if (ids.length === 0) break;
    ids.forEach((id) => visited.add(id));
    const ancestors = await loadReadable(ids);
    pending = [];
    for (const [id, ancestor] of ancestors) {
      if (ancestor.archivedAt) continue;
      readable.set(id, ancestor);
      if (ancestor.parentId) pending.push(ancestor.parentId);
    }
  }
  // A result can itself be another result's ancestor.
  for (const node of nodes) if (!node.archivedAt) readable.set(node.id, node);
  return new Map(nodes.map((node) => {
    const titles: string[] = [];
    const path = new Set([node.id]);
    let parentId = node.parentId;
    for (let depth = 0; parentId && depth < 512; depth += 1) {
      if (path.has(parentId)) break;
      path.add(parentId);
      const parent = readable.get(parentId);
      if (!parent || parent.docsLibraryId !== node.docsLibraryId) break;
      if (parent.title.trim()) titles.push(parent.title);
      parentId = parent.parentId;
    }
    return [node.id, titles.reverse()] as const;
  }));
}
