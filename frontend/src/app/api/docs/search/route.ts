import { NextRequest, NextResponse } from "next/server";
import {
  and,
  asc,
  desc,
  eq,
  ilike,
  inArray,
  isNotNull,
  isNull,
  or,
  sql,
} from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodeShares,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSearchIndex,
  knowledgeSupertags,
  projects,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  ensureDocsWorkspace,
  getDocsNodeAccessMap,
  effectiveDocsSearchBodyText,
  getDocsLibraryIdsForReadableProjects,
} from "@/lib/server/knowledge-docs-utils";
import {
  collectDocsSearchPage,
  docsSearchIndexJoin,
  docsSearchSnippet,
  docsSearchTagCondition,
  docsSearchTextConditions,
  loadDocsSearchBreadcrumbs,
  parseDocsSearchQuery,
} from "@/lib/server/docs-search";
import { normalizeDocsNodeType } from "@/lib/docs-model";
import { getAccessibleProject } from "@/lib/server/project-access";
import {
  canonicalEntityId,
  compactEntityId,
  isFullUuid,
  isUuidPrefixQuery,
} from "@/lib/entity-id";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const searchParams = request.nextUrl.searchParams;
  const parsedQuery = parseDocsSearchQuery(cleanOptionalString(searchParams.get("q"), 200) ?? "");
  const query = parsedQuery.text;
  const explicitTag = cleanOptionalString(searchParams.get("tag"), 200);
  const tagFilters = [...parsedQuery.tags, ...(explicitTag ? [explicitTag] : [])];
  const nodeType = cleanOptionalString(searchParams.get("node_type"), 40);
  const includeArchived = searchParams.get("include_archived") === "1";
  const archivedOnly = searchParams.get("archived_only") === "1";
  const rootsOnly = searchParams.get("roots_only") === "1";
  const projectRef = cleanOptionalString(searchParams.get("project"), 200);
  const rawLimit = Number(searchParams.get("limit"));
  const limit = Number.isFinite(rawLimit)
    ? Math.min(Math.max(Math.floor(rawLimit) || 40, 1), 500)
    : 40;
  const rawCursor = Number(searchParams.get("cursor") ?? 0);
  const cursor = Number.isSafeInteger(rawCursor) && rawCursor >= 0 ? rawCursor : 0;
  // Apply lifecycle visibility while building the search candidate set, not
  // only after fetching rows. This prevents archived nodes from consuming the
  // bounded candidate window and keeps search/live reads consistent.
  const archiveCondition = archivedOnly
    ? isNotNull(knowledgeNodes.archivedAt)
    : includeArchived
      ? undefined
      : isNull(knowledgeNodes.archivedAt);

  // Generic search is rooted in the actor's Personal Docs Library and shared
  // personal libraries. A supplied `project` is resolved and applied on the
  // server before candidate search; clients must not fetch a broad result set
  // and filter project identity in the browser.
  const workspace = await ensureDocsWorkspace(user);
  if (!workspace) {
    return NextResponse.json({ detail: "Docs workspaceへのアクセス権がありません" }, { status: 403 });
  }

  let workspaceIds = [workspace.id];
  // Search spans explicit personal shares; no secondary library is created or
  // selected by a generic query.
  const sharedWorkspaceRows = await db
    .select({ docsLibraryId: knowledgeNodes.docsLibraryId })
    .from(knowledgeNodeShares)
    .innerJoin(knowledgeNodes, eq(knowledgeNodeShares.nodeId, knowledgeNodes.id))
    .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
    .where(
      and(
        eq(knowledgeNodeShares.userId, user.id),
        eq(docsLibraries.libraryType, "personal"),
      ),
    );
  workspaceIds = await getDocsLibraryIdsForReadableProjects(user.id, [
    ...workspaceIds,
    ...sharedWorkspaceRows.map((row) => row.docsLibraryId),
  ]);

  let projectId: string | null = null;
  if (projectRef) {
    const canonicalProjectId = canonicalEntityId(projectRef);
    if (canonicalProjectId && isFullUuid(projectRef)) {
      projectId = canonicalProjectId;
    } else {
      const projectRows = await db
        .select({ id: projects.id })
        .from(projects)
        .where(
          and(
            isNull(projects.deletedAt),
            or(
              eq(projects.slug, projectRef),
              eq(projects.name, projectRef),
              ilike(projects.name, `%${projectRef}%`),
            ),
          ),
        )
        .limit(2);
      if (projectRows.length !== 1) {
        return NextResponse.json({ results: [], next_cursor: null });
      }
      projectId = projectRows[0].id;
    }

    // Keep project membership/permission semantics in the same ACL path as
    // the rest of Docs. A project reference never broadens Personal access.
    if (!(await getAccessibleProject(projectId, user.id))) {
      return NextResponse.json({ results: [], next_cursor: null });
    }
  }

  let directNodeId: string | null = null;
  let nodeIdPrefix: string | null = null;
  const notLegacyEmailBlank = sql<boolean>`NOT (
    ${knowledgeNodes.title} = '（空行）'
    AND EXISTS (
      WITH RECURSIVE email_ancestors AS (
        SELECT id, parent_id, system_key, docs_library_id,
               ARRAY[id]::uuid[] AS visited_path, 0 AS depth
        FROM knowledge_nodes
        WHERE id = ${knowledgeNodes.id}
          AND docs_library_id = ${knowledgeNodes.docsLibraryId}
        UNION ALL
        SELECT parent.id, parent.parent_id, parent.system_key,
               parent.docs_library_id,
               child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
        WHERE parent.docs_library_id = ${knowledgeNodes.docsLibraryId}
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT 1 FROM email_ancestors WHERE system_key LIKE 'project_mail:%'
    )
  )`;
  if (isFullUuid(query)) {
    // A pasted ID remains directly addressable; the ACL resolver below is
    // authoritative even when its library is outside the broad candidate set.
    directNodeId = canonicalEntityId(query);
  } else if (isUuidPrefixQuery(query)) {
    nodeIdPrefix = compactEntityId(query);
  }
  const textConditions = query && !directNodeId ? docsSearchTextConditions(query) : null;
  const prefixCondition = nodeIdPrefix
    ? sql<boolean>`replace(${knowledgeNodes.id}::text, '-', '') like ${`${nodeIdPrefix}%`}`
    : undefined;

  const conditions = [
    directNodeId ? undefined : inArray(knowledgeNodes.docsLibraryId, workspaceIds),
    projectId ? eq(knowledgeNodes.projectId, projectId) : undefined,
    sql<boolean>`regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''`,
    notLegacyEmailBlank,
    archiveCondition,
    nodeType
      ? eq(knowledgeNodes.nodeType, normalizeDocsNodeType(nodeType))
      : undefined,
    rootsOnly ? isNull(knowledgeNodes.parentId) : undefined,
    ...tagFilters.map((tag) => docsSearchTagCondition(tag)),
    directNodeId
      ? eq(knowledgeNodes.id, directNodeId)
      : textConditions
        ? or(prefixCondition, textConditions.match)
        : undefined,
  ];

  // Ranking and all text/tag/lifecycle predicates are applied before the
  // candidate scan. ACL filtering happens before the visible page limit,
  // rather than letting private matches consume an arbitrary overfetch cap.
  const order = [
    ...(prefixCondition ? [sql`case when ${prefixCondition} then -1 else 0 end`] : []),
    ...(textConditions ? [asc(textConditions.rank)] : []),
    desc(knowledgeNodes.updatedAt),
    asc(knowledgeNodes.sortOrder),
    asc(knowledgeNodes.createdAt),
    asc(knowledgeNodes.id),
  ];
  const page = await collectDocsSearchPage({
    cursor,
    limit,
    fetchCandidates: async (offset, size) => db
      .select({ id: knowledgeNodes.id })
      .from(knowledgeNodes)
      .leftJoin(knowledgeSearchIndex, docsSearchIndexJoin)
      .where(and(...conditions))
      .orderBy(...order)
      .limit(size)
      .offset(offset),
    resolveVisible: async (candidates) => {
      const access = await getDocsNodeAccessMap(candidates.map((node) => node.id), user, {
        includeArchived: includeArchived || archivedOnly,
      });
      return candidates.map((candidate) => {
        const node = access.get(candidate.id)?.node;
        // Re-check lifecycle on the authorized row in case it changed while
        // the candidate query was running.
        if (!node || (archivedOnly ? !node.archivedAt : !includeArchived && node.archivedAt)) return null;
        return node;
      });
    },
  });
  const visibleNodes = page.nodes;

  const tagRows = visibleNodes.length
    ? await db
        .select({
          nodeId: knowledgeNodeSupertags.nodeId,
          name: knowledgeSupertags.name,
          docsLibraryId: knowledgeSupertags.docsLibraryId,
        })
        .from(knowledgeNodeSupertags)
        .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
        .where(inArray(knowledgeNodeSupertags.nodeId, visibleNodes.map((node) => node.id)))
    : [];
  const nodeLibraryById = new Map(visibleNodes.map((node) => [node.id, node.docsLibraryId]));
  const tagsByNode = new Map<string, string[]>();
  for (const row of tagRows) {
    if (row.docsLibraryId !== nodeLibraryById.get(row.nodeId)) continue;
    const tags = tagsByNode.get(row.nodeId) ?? [];
    if (!tags.includes(row.name)) tags.push(row.name);
    tagsByNode.set(row.nodeId, tags);
  }

  const breadcrumbs = await loadDocsSearchBreadcrumbs(visibleNodes, async (ids) => {
    const access = await getDocsNodeAccessMap(ids, user, { includeArchived: false });
    return new Map([...access].map(([id, entry]) => [id, entry.node]));
  });

  // Existing result fields stay compatible with mention pickers and callers;
  // context fields are derived only from authorized, live rows.
  return NextResponse.json({
    results: visibleNodes.map((node) => {
      const breadcrumb = breadcrumbs.get(node.id) ?? [];
      let snippet = "";
      try {
        snippet = docsSearchSnippet(effectiveDocsSearchBodyText(node), query);
      } catch {
        // An unreadable encrypted body must never fall back to ciphertext or
        // potentially stale plaintext from the search index.
      }
      return {
        id: node.id,
        title: node.title,
        tags: (tagsByNode.get(node.id) ?? []).sort((a, b) => a.localeCompare(b)),
        project_id: node.projectId,
        parent_title: breadcrumb.at(-1) ?? null,
        breadcrumb,
        snippet,
      };
    }),
    next_cursor: page.nextCursor,
  });
}
