import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, ilike, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodeShares, knowledgeNodes } from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import { plainDocsTitle } from "@/lib/docs-title";
import {
  compactEntityId,
  isUuidPrefixQuery,
  matchEntityId,
} from "@/lib/entity-id";
import {
  ensureDocsWorkspace,
  getDocsNodeAccess,
  getDocsLibraryIdsForReadableProjects,
} from "@/lib/server/knowledge-docs-utils";

function aliasesOf(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string").map((item) => item.trim()).filter(Boolean)
    : [];
}

function plain(value: string) {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

function nonBlankTitleCondition() {
  // Blank rows are editor-only state. Exclude legacy rows from the page
  // switcher before scoring so they cannot consume the candidate limit.
  return sql<boolean>`
    regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
    AND NOT (
      ${knowledgeNodes.title} = '（空行）'
      AND EXISTS (
        WITH RECURSIVE email_ancestors AS (
          SELECT id, parent_id, system_key, body_json, docs_library_id,
                 ARRAY[id]::uuid[] AS visited_path, 0 AS depth
          FROM knowledge_nodes
          WHERE id = ${knowledgeNodes.id}
            AND docs_library_id = ${knowledgeNodes.docsLibraryId}
          UNION ALL
          SELECT parent.id, parent.parent_id, parent.system_key, parent.body_json,
                 parent.docs_library_id,
                 child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
          FROM knowledge_nodes AS parent
          INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
          WHERE parent.docs_library_id = ${knowledgeNodes.docsLibraryId}
            AND child.depth < 512
            AND NOT parent.id = ANY(child.visited_path)
        )
        SELECT 1
        FROM email_ancestors
        WHERE system_key LIKE 'project_mail:%'
          OR body_json::jsonb ->> 'format' = 'email'
      )
    )`;
}

function isEmailOriginNode(node: { systemKey?: string | null; bodyJson?: unknown }) {
  const body = node.bodyJson;
  return node.systemKey?.startsWith("project_mail:") === true
    || Boolean(body && typeof body === "object" && !Array.isArray(body) && (body as Record<string, unknown>).format === "email");
}

const FILM_ROOT_SYSTEM_KEY = "foam_source_grounded_v1:root.Film";

/**
 * Resolve Film ancestry in the database rather than trusting the abbreviated
 * breadcrumb sent to the browser.  The recursive query follows both the
 * normal parent chain and root_page_id so deeply nested nodes cannot bypass
 * the picker policy merely because their Film root is more than eight levels
 * above the candidate.
 */
async function getFilmNodeIds(
  nodes: Array<{ id: string; docsLibraryId: string }>,
): Promise<Set<string>> {
  if (nodes.length === 0) return new Set();
  try {
    const ids = sql.join(nodes.map((node) => sql`${node.id}`), sql`, `);
    const libraries = Array.from(new Set(nodes.map((node) => node.docsLibraryId)));
    const libraryIds = sql.join(libraries.map((id) => sql`${id}`), sql`, `);
    const result = await db.execute(sql`
      WITH RECURSIVE ancestors AS (
        SELECT
          n.id AS candidate_id,
          n.id AS ancestor_id,
          n.parent_id,
          n.root_page_id,
          n.docs_library_id,
          n.system_key,
          ARRAY[n.id]::uuid[] AS visited_path,
          0 AS depth
        FROM knowledge_nodes AS n
        WHERE n.id IN (${ids})
          AND n.docs_library_id IN (${libraryIds})
        UNION ALL
        SELECT
          child.candidate_id,
          parent.id,
          parent.parent_id,
          parent.root_page_id,
          parent.docs_library_id,
          parent.system_key,
          child.visited_path || ARRAY[parent.id]::uuid[],
          child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN ancestors AS child
          ON parent.id = child.parent_id OR parent.id = child.root_page_id
        WHERE parent.docs_library_id = child.docs_library_id
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT DISTINCT candidate_id
      FROM ancestors
      WHERE system_key = ${FILM_ROOT_SYSTEM_KEY}
    `);
    const rows = Array.isArray(result)
      ? result
      : Array.isArray((result as { rows?: unknown[] })?.rows)
        ? (result as { rows: unknown[] }).rows
        : [];
    return new Set(
      rows.flatMap((row) => {
        const id = row && typeof row === "object"
          ? (row as Record<string, unknown>).candidate_id
          : null;
        return typeof id === "string" ? [id] : [];
      }),
    );
  } catch {
    // Candidate visibility must fail closed if an adapter lacks recursive SQL
    // support or the query is temporarily unavailable.  The regular Docs
    // page endpoint remains usable; only writable ingest targets disappear.
    return new Set(nodes.map((node) => node.id));
  }
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const workspace = await ensureDocsWorkspace(user);
  const { searchParams } = new URL(request.url);
  const q = plain(searchParams.get("q") ?? "");
  const limit = Math.min(Math.max(Number(searchParams.get("limit") ?? 20) || 20, 1), 50);
  const writableOnly = ["1", "true", "yes"].includes(
    (searchParams.get("writable") ?? "").trim().toLowerCase(),
  );
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
  const workspaceIds = await getDocsLibraryIdsForReadableProjects(user.id, [
    workspace.id,
    ...sharedWorkspaceRows.map((row) => row.docsLibraryId),
  ]);

  // Workspace/node ACL is authoritative. Do not pre-filter by project
  // membership here: an explicitly shared personal node may carry a
  // project_id that the recipient cannot otherwise access.
  const accessCondition = undefined;
  // ID candidates are fetched independently of title/alias candidates so a
  // candidate cap for a common word cannot hide an exact or prefix UUID hit.
  // The compact expression supports canonical and hyphenless UUID queries;
  // short arbitrary text is intentionally not treated as an ID prefix.
  const idQuery = isUuidPrefixQuery(q) ? compactEntityId(q) : null;
  const idRows = idQuery
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(and(
          inArray(knowledgeNodes.docsLibraryId, workspaceIds),
          isNull(knowledgeNodes.archivedAt),
          nonBlankTitleCondition(),
          accessCondition,
          sql<boolean>`replace(${knowledgeNodes.id}::text, '-', '') like ${`${idQuery}%`}`,
        ))
        // ACL is evaluated after candidate retrieval.  Keep a bounded
        // overscan so unauthorized prefix matches do not consume the entire
        // visible ID candidate cap.
        .limit(Math.max(50, limit * 5))
    : [];
  // Exact title/alias hits must not disappear behind the broad candidate cap.
  // Fetch them separately, then merge them into the scored candidate set.
  const exactRows = q
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(and(
          inArray(knowledgeNodes.docsLibraryId, workspaceIds),
          isNull(knowledgeNodes.archivedAt),
          nonBlankTitleCondition(),
          accessCondition,
          or(
            sql<boolean>`lower(regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', ' ', 'g')) = ${q}`,
            // aliases カラムは json 型なので、jsonb 関数へ渡す前に明示的にキャストする。
            // キャストを省くと coalesce の型不一致で SQL 例外になり、検索全体が 500 になる。
            sql<boolean>`exists (
              select 1
              from jsonb_array_elements_text(coalesce(${knowledgeNodes.aliases}::jsonb, '[]'::jsonb)) as alias(value)
              where lower(regexp_replace(trim(alias.value), '[[:space:]]+', ' ', 'g')) = ${q}
            )`,
          ),
        ))
        .limit(limit)
    : [];
  // 候補は上限で打ち切るため、タイトル昇順ではなく「良いマッチ順」で並べる。
  // 昇順のままだと「倉庫」のような一般語で候補が上限を超えたとき、前方一致する
  // ページが並び順の後ろへ落ち、スコアリングに届く前に捨てられてしまう。
  const candidateOrder = q
    ? [
        sql`case
          when lower(regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', ' ', 'g')) = ${q} then 0
          when lower(${knowledgeNodes.title}) like ${`${q}%`} then 1
          else 2
        end`,
        sql`case when exists (
          select 1 from ${knowledgeNodes} as child
          where child.parent_id = ${knowledgeNodes.id}
            and child.docs_library_id = ${knowledgeNodes.docsLibraryId}
            and child.archived_at is null
        ) then 0 else 1 end`,
        sql`length(${knowledgeNodes.title})`,
        asc(knowledgeNodes.title),
        asc(knowledgeNodes.sortOrder),
      ]
    : [asc(knowledgeNodes.title), asc(knowledgeNodes.sortOrder)];

  const rows = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        inArray(knowledgeNodes.docsLibraryId, workspaceIds),
        isNull(knowledgeNodes.archivedAt),
        nonBlankTitleCondition(),
        accessCondition,
        q
          ? or(
              ilike(knowledgeNodes.title, `%${q}%`),
              sql<boolean>`coalesce(${knowledgeNodes.aliases}::text, '') ilike ${`%${q}%`}`,
            )
          : undefined,
      ),
    )
    .orderBy(...candidateOrder)
    .limit(Math.max(50, limit * 5));

  const candidateRows = Array.from(new Map([...idRows, ...exactRows, ...rows].map((node) => [node.id, node])).values());
  const visibleRows = await Promise.all(candidateRows.map((node) => getDocsNodeAccess(node.id, user)));
  const writableRows = writableOnly
    ? visibleRows.filter((access) => {
        if (!access || (access.permission !== "owner" && access.permission !== "write")) {
          return false;
        }
        // A shared write ACL on another user's Personal Library is not an
        // explicit-ingest target.  Project-canonical nodes are the only
        // cross-owner exception; their project ACL is evaluated by
        // getDocsNodeAccess and remains authoritative.
        const workspace = access.workspace;
        const personal = (workspace.libraryType ?? "personal") === "personal";
        return !personal
          || workspace.ownerUserId === user.id
          || Boolean(access.node.projectId);
      })
    : visibleRows;
  const filmNodeIds = writableOnly
    ? await getFilmNodeIds(
        writableRows
          .map((access) => access?.node)
          .filter((node): node is NonNullable<typeof node> => Boolean(node))
          .map((node) => ({ id: node.id, docsLibraryId: node.docsLibraryId })),
      )
    : new Set<string>();
  const active = writableRows
    .map((access) => access?.node)
    .filter((node): node is NonNullable<typeof node> => Boolean(node))
    .filter((node) => !filmNodeIds.has(node.id));
  const byId = new Map(active.map((node) => [node.id, node]));
  let parentIds = Array.from(new Set(active.map((node) => node.parentId).filter((id): id is string => Boolean(id))));
  for (let depth = 0; depth < 8 && parentIds.length > 0; depth += 1) {
    const parents = await db
      .select()
      .from(knowledgeNodes)
      .where(and(
        inArray(knowledgeNodes.docsLibraryId, workspaceIds),
        inArray(knowledgeNodes.id, parentIds),
        isNull(knowledgeNodes.archivedAt),
        accessCondition,
      ));
    const parentAccessRows = await Promise.all(parents.map((parent) => getDocsNodeAccess(parent.id, user)));
    const nextIds: string[] = [];
    for (const access of parentAccessRows) {
      const parent = access?.node;
      if (!parent) continue;
      if (byId.has(parent.id)) continue;
      byId.set(parent.id, parent);
      if (parent.parentId) nextIds.push(parent.parentId);
    }
    parentIds = Array.from(new Set(nextIds));
  }
  // 「ページらしさ」は node_type では判定できない。page は旧データにしか無く、
  // 現在の作成経路は常に node を書き込むため（normalizeDocsNodeType）。
  // 代わりに子を持つかどうかで見る。倉庫やまとめノードは子を持ち、本文行は持たない。
  const parentIdsWithChildren = new Set(
    active.length > 0
      ? (await db
          .selectDistinct({ parentId: knowledgeNodes.parentId })
          .from(knowledgeNodes)
          .where(and(
            inArray(knowledgeNodes.docsLibraryId, workspaceIds),
            isNull(knowledgeNodes.archivedAt),
            inArray(knowledgeNodes.parentId, active.map((node) => node.id)),
          ))).map((row) => row.parentId)
      : [],
  );
  const scored = active
    .map((node) => {
      const aliases = aliasesOf(node.aliases);
      const displayTitle = plainDocsTitle(node.title) || "Untitled";
      const titleHaystacks = [displayTitle, node.title].map(plain);
      const aliasHaystacks = aliases.map((alias) => ({ alias, plain: plain(alias) }));
      const haystacks = [...titleHaystacks, ...aliasHaystacks.map((item) => item.plain)];
      const exact = haystacks.some((item) => item === q);
      const starts = haystacks.some((item) => item.startsWith(q));
      const idMatch = idQuery ? matchEntityId(node.id, q) : null;
      const includes = Boolean(idMatch) || !q || haystacks.some((item) => item.includes(q));
      if (!includes) return null;
      // タイトルではヒットせずエイリアスでヒットした場合、どのエイリアスで
      // マッチしたかをエディタ側が表示できるように matched_alias を返す。
      const titleHit = !q || titleHaystacks.some((item) => item.includes(q));
      const matchedAlias = q && !titleHit
        ? (aliasHaystacks.find((item) => item.plain === q)
            ?? aliasHaystacks.find((item) => item.plain.startsWith(q))
            ?? aliasHaystacks.find((item) => item.plain.includes(q)))?.alias ?? null
        : null;
      const breadcrumb: string[] = [];
      let cursor = node.parentId ? byId.get(node.parentId) : null;
      const seen = new Set<string>();
      let emailContext = isEmailOriginNode(node);
      while (cursor && !seen.has(cursor.id) && breadcrumb.length < 8) {
        seen.add(cursor.id);
        emailContext = emailContext || isEmailOriginNode(cursor);
        const breadcrumbTitle = plainDocsTitle(cursor.title);
        // Blank legacy parents are retained only as structural bridges.  Keep
        // walking through them so a grandparent remains reachable, but never
        // expose the bridge itself.  The literal （空行） marker is hidden
        // only in an email-origin subtree; ordinary user titles remain valid.
        if (breadcrumbTitle && !(cursor.title === "（空行）" && emailContext)) {
          breadcrumb.unshift(breadcrumbTitle);
        }
        cursor = cursor.parentId ? byId.get(cursor.parentId) : null;
      }
      const nodeType = node.nodeType ?? "node";
      return {
        id: node.id,
        system_key: node.systemKey,
        title: displayTitle,
        aliases,
        matched_alias: matchedAlias,
        node_type: nodeType,
        project_id: node.projectId,
        breadcrumb,
        // Keep the historical title scores (exact=0, prefix=1,
        // partial=2) while reserving negative scores for ID hits.  This
        // preserves existing consumers' ordering and makes ID priority
        // explicit without changing title/alias semantics.
        score: idMatch === "exact"
          ? -2
          : idMatch === "prefix"
            ? -1
            : exact
              ? 0
              : starts
                ? 1
                : q
                  ? 2
                  : 3,
        // ページスイッチャーはページを開くための機能なので、同じマッチ度なら
        // ページらしいもの（子を持つ / live query / 旧page）を優先し、
        // 次にタイトルが短い（＝語そのものに近い）ものを上げる。
        typeScore: parentIdsWithChildren.has(node.id) || nodeType === "page" || nodeType === "search" ? 0 : 1,
        titleLength: displayTitle.length,
      };
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
    .sort((a, b) =>
      a.score - b.score
      || a.typeScore - b.typeScore
      || a.titleLength - b.titleLength
      || a.title.localeCompare(b.title))
    .slice(0, limit)
    .map(({ typeScore: _typeScore, titleLength: _titleLength, ...page }) => page);

  return NextResponse.json({ pages: scored });
}
