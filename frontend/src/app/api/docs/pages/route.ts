import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, ilike, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { plainDocsTitle } from "@/lib/docs-title";
import { ensureDocsWorkspace, getUserProjects } from "@/lib/server/knowledge-docs-utils";

function aliasesOf(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string").map((item) => item.trim()).filter(Boolean)
    : [];
}

function plain(value: string) {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const workspace = await ensureDocsWorkspace(user);
  const { searchParams } = new URL(request.url);
  const q = plain(searchParams.get("q") ?? "");
  const limit = Math.min(Math.max(Number(searchParams.get("limit") ?? 20) || 20, 1), 50);
  const projects = await getUserProjects(user.id);
  const accessibleProjectIds = projects.map((project) => project.id);

  const accessCondition = accessibleProjectIds.length > 0
    ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
    : isNull(knowledgeNodes.projectId);
  // Exact title/alias hits must not disappear behind the broad candidate cap.
  // Fetch them separately, then merge them into the scored candidate set.
  const exactRows = q
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(and(
          eq(knowledgeNodes.workspaceId, workspace.id),
          isNull(knowledgeNodes.archivedAt),
          accessCondition,
          or(
            sql<boolean>`lower(regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', ' ', 'g')) = ${q}`,
            sql<boolean>`exists (
              select 1
              from jsonb_array_elements_text(coalesce(${knowledgeNodes.aliases}, '[]'::jsonb)) as alias(value)
              where lower(regexp_replace(trim(alias.value), '[[:space:]]+', ' ', 'g')) = ${q}
            )`,
          ),
        ))
        .limit(limit)
    : [];
  const rows = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.workspaceId, workspace.id),
        isNull(knowledgeNodes.archivedAt),
        accessCondition,
        q
          ? or(
              ilike(knowledgeNodes.title, `%${q}%`),
              sql<boolean>`coalesce(${knowledgeNodes.aliases}::text, '') ilike ${`%${q}%`}`,
            )
          : undefined,
      ),
    )
    .orderBy(asc(knowledgeNodes.title), asc(knowledgeNodes.sortOrder))
    .limit(Math.max(50, limit * 5));

  const active = Array.from(new Map([...exactRows, ...rows].map((node) => [node.id, node])).values());
  const byId = new Map(active.map((node) => [node.id, node]));
  let parentIds = Array.from(new Set(active.map((node) => node.parentId).filter((id): id is string => Boolean(id))));
  for (let depth = 0; depth < 8 && parentIds.length > 0; depth += 1) {
    const parents = await db
      .select()
      .from(knowledgeNodes)
      .where(and(
        eq(knowledgeNodes.workspaceId, workspace.id),
        inArray(knowledgeNodes.id, parentIds),
        isNull(knowledgeNodes.archivedAt),
        accessCondition,
      ));
    const nextIds: string[] = [];
    for (const parent of parents) {
      if (byId.has(parent.id)) continue;
      byId.set(parent.id, parent);
      if (parent.parentId) nextIds.push(parent.parentId);
    }
    parentIds = Array.from(new Set(nextIds));
  }
  const scored = active
    .map((node) => {
      const aliases = aliasesOf(node.aliases);
      const displayTitle = plainDocsTitle(node.title) || "Untitled";
      const titleHaystacks = [displayTitle, node.title].map(plain);
      const aliasHaystacks = aliases.map((alias) => ({ alias, plain: plain(alias) }));
      const haystacks = [...titleHaystacks, ...aliasHaystacks.map((item) => item.plain)];
      const exact = haystacks.some((item) => item === q);
      const starts = haystacks.some((item) => item.startsWith(q));
      const includes = !q || haystacks.some((item) => item.includes(q));
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
      while (cursor && !seen.has(cursor.id) && breadcrumb.length < 8) {
        seen.add(cursor.id);
        breadcrumb.unshift(cursor.title || "Untitled");
        cursor = cursor.parentId ? byId.get(cursor.parentId) : null;
      }
      return {
        id: node.id,
        system_key: node.systemKey,
        title: displayTitle,
        aliases,
        matched_alias: matchedAlias,
        node_type: node.nodeType ?? "node",
        project_id: node.projectId,
        breadcrumb: breadcrumb.map((title) => plainDocsTitle(title) || "Untitled"),
        score: exact ? 0 : starts ? 1 : q ? 2 : 3,
      };
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
    .sort((a, b) => a.score - b.score || a.title.localeCompare(b.title))
    .slice(0, limit);

  return NextResponse.json({ pages: scored });
}
