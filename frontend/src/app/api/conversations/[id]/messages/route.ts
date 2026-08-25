import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationMessages, conversationSessions } from "@/db/schema";
import { and, asc, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  canWriteConversationSession,
  getLiveConversationSession,
  messageToSnake,
} from "@/lib/server/conversation-route-utils";
import { encryptText } from "@/lib/server/field-crypto";
import { jsonWithConditional } from "@/lib/server/http-cache";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

type BranchMetadataRow = Pick<
  typeof conversationMessages.$inferSelect,
  | "id"
  | "parentMessageId"
  | "role"
  | "branchIndex"
  | "createdAt"
  | "isActiveBranch"
>;

type LegacyRepairRow = Pick<
  typeof conversationMessages.$inferSelect,
  | "id"
  | "parentMessageId"
  | "role"
  | "branchIndex"
  | "createdAt"
  | "isActiveBranch"
>;

type BranchGroupKey = `parent:${string}` | `root:${string}`;

function branchGroupKey(
  row: Pick<BranchMetadataRow, "parentMessageId" | "role">,
): BranchGroupKey {
  return row.parentMessageId != null
    ? `parent:${row.parentMessageId}`
    : `root:${row.role}`;
}

/**
 * Mirror the repository's read-time repair for old flat transcripts without
 * writing from a GET handler.  Newer rows already have parent links and pass
 * through unchanged.  The returned map is only used for the rows already
 * loaded by the active-path query; it does not preload the conversation tree.
 */
function getLegacyParentLinks(
  rows: LegacyRepairRow[],
): Map<string, string | null> | null {
  const parentById = new Map<string, string | null>();
  const rootRoles = new Set<string>();
  let previousId: string | null = null;
  let changed = false;

  const orderedRows = [...rows].sort((left, right) => {
    const leftTime = left.createdAt?.getTime() ?? Number.NEGATIVE_INFINITY;
    const rightTime = right.createdAt?.getTime() ?? Number.NEGATIVE_INFINITY;
    return leftTime - rightTime || left.id.localeCompare(right.id);
  });

  for (const row of orderedRows) {
    let parentMessageId = row.parentMessageId;
    const explicitRootSibling =
      parentMessageId == null &&
      row.branchIndex != null &&
      row.branchIndex !== 0 &&
      rootRoles.has(row.role);

    if (previousId && parentMessageId == null && !explicitRootSibling) {
      parentMessageId = previousId;
      changed = true;
    }

    parentById.set(row.id, parentMessageId);
    if (parentMessageId == null) rootRoles.add(row.role);
    if (row.isActiveBranch !== false) previousId = row.id;
  }

  return changed ? parentById : null;
}

type BranchProjectionResult = {
  branchInfo: Map<string, { branch_count: number; branch_index: number }>;
  parentLinks?: Map<string, string | null>;
};

/**
 * Project branch metadata onto the rows returned by the active-path query.
 *
 * The metadata query deliberately does not filter is_active_branch: a cold
 * active-path response still needs to know about inactive siblings so that
 * the client can render its branch navigator without loading sibling bodies.
 */
async function projectBranchInfo(
  sessionId: string,
  rows: BranchMetadataRow[],
  options?: {
    legacyParentLinks?: ReadonlyMap<string, string | null>;
    repairRows?: LegacyRepairRow[];
  },
): Promise<BranchProjectionResult> {
  if (rows.length === 0) return { branchInfo: new Map() };

  const parentIds = Array.from(
    new Set(
      rows
        .map((row) => row.parentMessageId)
        .filter((value): value is string => value != null),
    ),
  );
  const rootRoles = Array.from(
    new Set(
      rows
        .filter((row) => row.parentMessageId == null)
        .map((row) => row.role),
    ),
  );

  const siblingConditions = [];
  if (parentIds.length > 0) {
    siblingConditions.push(
      inArray(conversationMessages.parentMessageId, parentIds),
    );
  }
  if (rootRoles.length > 0 || options?.legacyParentLinks) {
    siblingConditions.push(
      options?.legacyParentLinks
        ? isNull(conversationMessages.parentMessageId)
        : and(
            isNull(conversationMessages.parentMessageId),
            inArray(conversationMessages.role, rootRoles),
          ),
    );
  }

  // `rows` always contains either a parent id or a root role, so this is one
  // batched metadata query rather than one count query per returned message.
  const siblingRows = await db
    .select({
      id: conversationMessages.id,
      parentMessageId: conversationMessages.parentMessageId,
      role: conversationMessages.role,
      branchIndex: conversationMessages.branchIndex,
      createdAt: conversationMessages.createdAt,
      isActiveBranch: conversationMessages.isActiveBranch,
    })
    .from(conversationMessages)
    .where(
      and(
        eq(conversationMessages.sessionId, sessionId),
        or(...siblingConditions)!,
      ),
    )
    .orderBy(asc(conversationMessages.branchIndex), asc(conversationMessages.id));

  let parentLinks = options?.legacyParentLinks
    ? new Map(options.legacyParentLinks)
    : undefined;
  if (options?.legacyParentLinks) {
    const rowsById = new Map<string, LegacyRepairRow>();
    for (const row of options.repairRows ?? []) rowsById.set(row.id, row);
    for (const row of siblingRows) {
      rowsById.set(row.id, row);
    }
    const repairedLinks = getLegacyParentLinks([...rowsById.values()]);
    if (repairedLinks) parentLinks = repairedLinks;
  }

  const siblingGroups = new Map<BranchGroupKey, string[]>();
  for (const sibling of siblingRows) {
    // When a cold load repaired an old flat transcript in memory, raw
    // parent-null rows after the first root are no longer root siblings.  Do
    // not let them create a false root navigator.  Explicit root branches
    // that are still in the returned active path remain eligible.
    if (
      parentLinks &&
      sibling.parentMessageId == null &&
      parentLinks.has(sibling.id) &&
      parentLinks.get(sibling.id) != null
    ) {
      continue;
    }
    const key = branchGroupKey(sibling);
    const group = siblingGroups.get(key);
    if (group) {
      group.push(sibling.id);
    } else {
      siblingGroups.set(key, [sibling.id]);
    }
  }

  const projection = new Map<
    string,
    { branch_count: number; branch_index: number }
  >();
  for (const row of rows) {
    const siblings = siblingGroups.get(branchGroupKey(row)) ?? [];
    const branchIndex = siblings.indexOf(row.id);
    projection.set(row.id, {
      branch_count: siblings.length || 1,
      // A legacy row missing from the metadata result remains addressable as
      // the only branch, matching FastAPI's default projection.
      branch_index: branchIndex >= 0 ? branchIndex : 0,
    });
  }
  return { branchInfo: projection, parentLinks };
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

  // ?since=<ISO8601 UTC>: updated_at（無ければ created_at）が since より新しい差分のみ返す。
  const sinceParam = request.nextUrl.searchParams.get("since");
  let sinceDate: Date | null = null;
  if (sinceParam !== null) {
    const parsed = new Date(sinceParam);
    if (Number.isNaN(parsed.getTime())) {
      return NextResponse.json(
        { detail: "since は ISO8601 形式で指定してください" },
        { status: 400 },
      );
    }
    sinceDate = parsed;
  }

  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }

  const conditions = [
    eq(conversationMessages.sessionId, id),
  ];
  if (sinceDate) {
    // コミット待ちの行が cursor より古い timestamp を持つ競合でも取りこぼさないよう、
    // 5 秒だけ重ねて取得する。クライアントは id でマージするため重複表示は起きない。
    const sinceFloor = new Date(sinceDate.getTime() - 5_000);
    conditions.push(
      or(
        gt(conversationMessages.updatedAt, sinceFloor),
        and(
          isNull(conversationMessages.updatedAt),
          gt(conversationMessages.createdAt, sinceFloor),
        ),
      )!,
    );
  } else {
    // 初回全量は現在のbranchだけ。差分時はinactive化された行もtombstoneとして返し、
    // クライアントが旧branchを永続キャッシュから除去できるようにする。
    conditions.push(
      or(
        eq(conversationMessages.isActiveBranch, true),
        isNull(conversationMessages.isActiveBranch),
      )!,
    );
  }

  const rows = await db
    .select()
    .from(conversationMessages)
    .where(and(...conditions))
    .orderBy(asc(conversationMessages.createdAt), asc(conversationMessages.id));

  // FastAPI repairs legacy flat rows before projecting branch metadata.  For
  // the BFF's cold GET, apply the same repair to the already-loaded active
  // path so the two transports agree without a write or a tree-wide preload.
  const legacyParentLinks = sinceDate ? null : getLegacyParentLinks(rows);
  const projectionRows: BranchMetadataRow[] = rows.map((row) => ({
    id: row.id,
    parentMessageId:
      legacyParentLinks?.get(row.id) ?? row.parentMessageId,
    role: row.role,
    branchIndex: row.branchIndex,
    createdAt: row.createdAt,
    isActiveBranch: row.isActiveBranch,
  }));
  const projection = await projectBranchInfo(
    id,
    projectionRows,
    legacyParentLinks
      ? { legacyParentLinks, repairRows: rows }
      : undefined,
  );
  const parentLinks = projection.parentLinks ?? legacyParentLinks;
  const messages = rows.map((row) => ({
    ...messageToSnake(row),
    ...(parentLinks?.has(row.id)
      ? { parent_message_id: parentLinks.get(row.id) }
      : {}),
    ...projection.branchInfo.get(row.id),
  }));

  // server_time は「返した行の最新タイムスタンプ」。差分が無ければ since をそのまま返し、
  // URL（?since=）を安定させてブラウザの ETag 304 が効くようにする。
  // 取得条件には上記の重なりを持たせているため、同時 commit との境界でも取りこぼさない。
  let serverTime = sinceParam ?? new Date().toISOString();
  // overlap で since より古い行だけが返った場合も cursor を後退させない。
  let latestMs = sinceDate?.getTime() ?? Number.NEGATIVE_INFINITY;
  for (const row of rows) {
    const stamp = row.updatedAt ?? row.createdAt;
    if (stamp) {
      const ms = stamp.getTime();
      if (ms > latestMs) {
        latestMs = ms;
        serverTime = stamp.toISOString();
      }
    }
  }

  // ETag は server_time を除いた messages のみから算出（304 が壊れないようにする）。
  return jsonWithConditional(
    request,
    { messages, server_time: serverTime },
    { etagSource: { messages } },
  );
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json().catch(() => null);
  const role = body?.role;
  const content = typeof body?.content === "string" ? body.content : "";

  if (role !== "user" && role !== "assistant") {
    return NextResponse.json(
      { detail: "role は user または assistant を指定してください" },
      { status: 400 },
    );
  }

  if (!content.trim()) {
    return NextResponse.json({ detail: "content は必須です" }, { status: 400 });
  }

  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canWriteConversationSession(id, user))) {
    return NextResponse.json(
      { detail: "会話への書き込み権限がありません" },
      { status: 403 },
    );
  }

  const now = new Date();
  const [message] = await db
    .insert(conversationMessages)
    .values({
      sessionId: id,
      role,
      content: encryptText(content, "conversation_messages.content"),
      messageMetadata: {},
      senderType: role === "user" ? "user" : null,
      senderId: role === "user" ? user.id : null,
      senderDisplayName:
        role === "user" ? user.displayName || user.username || user.email || user.id : null,
      createdAt: now,
      branchIndex: 0,
      isActiveBranch: true,
    })
    .returning();

  await db
    .update(conversationSessions)
    .set({
      // A delayed/duplicated writer must not regress the canonical activity
      // marker used by the history ordering.
      lastActivity: sql`case
        when ${conversationSessions.lastActivity} is null
          or ${conversationSessions.lastActivity} < ${now}
        then ${now}
        else ${conversationSessions.lastActivity}
      end`,
      messageCount: sql`coalesce(${conversationSessions.messageCount}, 0) + 1`,
    })
    .where(eq(conversationSessions.id, id));

  return NextResponse.json({ success: true, message: messageToSnake(message) });
}
