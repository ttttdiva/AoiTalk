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
  getDocsNodeAccess,
  getDocsLibraryIdsForReadableProjects,
} from "@/lib/server/knowledge-docs-utils";
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
  const query = cleanOptionalString(searchParams.get("q"), 200);
  const nodeType = cleanOptionalString(searchParams.get("node_type"), 40);
  const includeArchived = searchParams.get("include_archived") === "1";
  const archivedOnly = searchParams.get("archived_only") === "1";
  const rootsOnly = searchParams.get("roots_only") === "1";
  const projectRef = cleanOptionalString(searchParams.get("project"), 200);
  const limit = Math.min(
    Math.max(Number(searchParams.get("limit")) || 40, 1),
    500,
  );
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
        return NextResponse.json({ results: [] });
      }
      projectId = projectRows[0].id;
    }

    // Keep project membership/permission semantics in the same ACL path as
    // the rest of Docs. A project reference never broadens Personal access.
    if (!(await getAccessibleProject(projectId, user.id))) {
      return NextResponse.json({ results: [] });
    }
  }

  let searchNodeIds: string[] | null = null;
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
  if (query) {
    if (isFullUuid(query)) {
      // @Docs でIDを貼り付けた場合も、タイトル検索へ戻さず対象を
      // 直接候補化する。認可・workspace/project境界は下のnode queryで再検証する。
      // UUID列に対する単一ID検索は `inArray` の配列バインドに任せず、
      // typed な `eq` 条件として組み立てる。これにより、単一候補の
      // @Docs ID検索でドライバごとの配列キャスト差異を避けられる。
      directNodeId = canonicalEntityId(query);
    } else {
      if (isUuidPrefixQuery(query)) nodeIdPrefix = compactEntityId(query);
      const matched = await db
        .select({ nodeId: knowledgeSearchIndex.nodeId })
        .from(knowledgeSearchIndex)
        .innerJoin(knowledgeNodes, eq(knowledgeSearchIndex.nodeId, knowledgeNodes.id))
        .where(
          and(
            inArray(knowledgeSearchIndex.docsLibraryId, workspaceIds),
            projectId ? eq(knowledgeSearchIndex.projectId, projectId) : undefined,
            archiveCondition,
            sql<boolean>`regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''`,
            notLegacyEmailBlank,
            or(
              ilike(knowledgeSearchIndex.titleText, `%${query}%`),
              ilike(knowledgeSearchIndex.bodyTextPlain, `%${query}%`),
            ),
          ),
        )
        .limit(limit);
      searchNodeIds = matched.map((row) => row.nodeId);
    }
    if (searchNodeIds && searchNodeIds.length === 0 && !nodeIdPrefix)
      return NextResponse.json({ results: [] });
  }

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
    directNodeId
      ? eq(knowledgeNodes.id, directNodeId)
      : nodeIdPrefix
        ? or(
            sql<boolean>`replace(${knowledgeNodes.id}::text, '-', '') like ${`${nodeIdPrefix}%`}`,
            searchNodeIds && searchNodeIds.length > 0
              ? inArray(knowledgeNodes.id, searchNodeIds)
              : undefined,
          )
        : searchNodeIds
          ? inArray(knowledgeNodes.id, searchNodeIds)
          : undefined,
  ].filter(Boolean);

  const nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...conditions))
    .orderBy(
      ...(nodeIdPrefix
        ? [
            sql`case when replace(${knowledgeNodes.id}::text, '-', '') like ${`${nodeIdPrefix}%`} then 0 else 1 end`,
            asc(knowledgeNodes.sortOrder),
            asc(knowledgeNodes.createdAt),
            asc(knowledgeNodes.id),
            desc(knowledgeNodes.updatedAt),
          ]
        : [
            asc(knowledgeNodes.sortOrder),
            asc(knowledgeNodes.createdAt),
            asc(knowledgeNodes.id),
            desc(knowledgeNodes.updatedAt),
          ]),
    )
    .limit(limit * 4);

  const visibleNodes = (
    await Promise.all(nodes.map((node) => getDocsNodeAccess(node.id, user)))
  )
    .map((access) => access?.node)
    .filter((node): node is NonNullable<typeof node> => Boolean(node))
    .slice(0, limit);

  const tagRows = visibleNodes.length
    ? await db
        .select({ nodeId: knowledgeNodeSupertags.nodeId, name: knowledgeSupertags.name })
        .from(knowledgeNodeSupertags)
        .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
        .where(inArray(knowledgeNodeSupertags.nodeId, visibleNodes.map((node) => node.id)))
    : [];
  const tagsByNode = new Map<string, string[]>();
  for (const row of tagRows) {
    const tags = tagsByNode.get(row.nodeId) ?? [];
    tags.push(row.name);
    tagsByNode.set(row.nodeId, tags);
  }

  const parentIds = Array.from(
    new Set(
      visibleNodes
        .map((node) => node.parentId)
        .filter((id): id is string => Boolean(id)),
    ),
  );
  const parentRows = parentIds.length
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(inArray(knowledgeNodes.id, parentIds))
    : [];
  const parentAccessRows = await Promise.all(
    parentRows.map((parent) => getDocsNodeAccess(parent.id, user)),
  );
  const parentTitlesById = new Map(
    parentAccessRows
      .map((access) => access?.node)
      .filter((node): node is NonNullable<typeof node> => Boolean(node))
      .map((node) => [node.id, node.title] as const),
  );

  // Keep the response aligned with the Python Docs API contract.  Body and
  // internal node metadata are intentionally omitted from search candidates.
  return NextResponse.json({
    results: visibleNodes.map((node) => ({
      id: node.id,
      title: node.title,
      tags: tagsByNode.get(node.id) ?? [],
      project_id: node.projectId,
      parent_title: node.parentId
        ? parentTitlesById.get(node.parentId) ?? null
        : null,
    })),
  });
}
