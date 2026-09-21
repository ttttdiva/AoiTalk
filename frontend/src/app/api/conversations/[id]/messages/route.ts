import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationMessages, conversationSessions } from "@/db/schema";
import { and, asc, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  canWriteLockedConversationSession,
  canWriteConversationSession,
  getLiveConversationSession,
  messageToSnake,
} from "@/lib/server/conversation-route-utils";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import { toDbLocalTimestamp } from "@/lib/server/db-time";
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

type RouteErrorCode =
  | "unauthenticated"
  | "invalid_request"
  | "session_not_found"
  | "forbidden"
  | "idempotency_conflict"
  | "message_persistence_failed";

class ConversationMessageRouteError extends Error {
  constructor(
    readonly code: RouteErrorCode,
    readonly status: number,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "ConversationMessageRouteError";
  }
}

function requestIdFor(request: NextRequest): string {
  const supplied = request.headers.get("x-request-id")?.trim() ?? "";
  // Preserve a trusted correlation id when it is already present, while
  // preventing arbitrary control characters/oversized values from being
  // reflected into a response body or header.
  if (/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(supplied)) return supplied;
  return crypto.randomUUID();
}

function structuredErrorResponse(
  requestId: string,
  status: number,
  code: RouteErrorCode,
  message: string,
  retryable = false,
) {
  return NextResponse.json(
    {
      error: {
        code,
        message,
        retryable,
        request_id: requestId,
      },
    },
    {
      status,
      headers: { "x-request-id": requestId },
    },
  );
}

function routeErrorResponse(requestId: string, error: ConversationMessageRouteError) {
  return structuredErrorResponse(
    requestId,
    error.status,
    error.code,
    error.message,
    error.retryable,
  );
}

function isUniqueViolation(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const code = (error as { code?: unknown }).code;
  const constraint = (error as { constraint?: unknown }).constraint;
  return (
    code === "23505" &&
    (constraint === undefined ||
      constraint === "uq_conversation_messages_session_client_message_id")
  );
}

function messageContentMatches(
  row: typeof conversationMessages.$inferSelect,
  role: string,
  content: string,
  actorId: string,
): boolean {
  // client_message_id is scoped to both the conversation and the
  // authenticated actor.  A missing sender id is intentionally not treated
  // as a match: replaying a legacy/foreign row without actor provenance
  // would let one participant claim another participant's message.
  if (row.role !== role || row.senderId !== actorId) return false;
  try {
    return decryptTextIfNeeded(
      row.content,
      "conversation_messages.content",
    ) === content;
  } catch {
    // An existing row that cannot be decrypted is not safe to treat as an
    // idempotent replay.  The caller receives a conflict rather than a raw
    // crypto/storage error.
    return false;
  }
}

function messagePayload(row: typeof conversationMessages.$inferSelect) {
  const payload = messageToSnake(row) as Record<string, unknown>;
  if (!row.clientMessageId) return payload;
  const metadata =
    payload.metadata && typeof payload.metadata === "object"
      ? { ...(payload.metadata as Record<string, unknown>) }
      : {};
  metadata.client_message_id = row.clientMessageId;
  return {
    ...payload,
    metadata,
    client_message_id: row.clientMessageId,
  };
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const requestId = requestIdFor(request);
  const user = await getSession();
  if (!user) {
    return structuredErrorResponse(
      requestId,
      401,
      "unauthenticated",
      "認証が必要です",
    );
  }

  const { id } = await params;

  // ?since=<ISO8601 UTC>: updated_at（無ければ created_at）が since より新しい差分のみ返す。
  const sinceParam = request.nextUrl.searchParams.get("since");
  let sinceDate: Date | null = null;
  if (sinceParam !== null) {
    const parsed = new Date(sinceParam);
    if (Number.isNaN(parsed.getTime())) {
      return structuredErrorResponse(
        requestId,
        400,
        "invalid_request",
        "since は ISO8601 形式で指定してください",
      );
    }
    sinceDate = parsed;
  }

  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return structuredErrorResponse(
      requestId,
      404,
      "session_not_found",
      "セッションが見つかりません",
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
    ...messagePayload(row),
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
  const requestId = requestIdFor(request);
  const user = await getSession();
  if (!user) {
    return structuredErrorResponse(
      requestId,
      401,
      "unauthenticated",
      "認証が必要です",
    );
  }

  const { id } = await params;
  const body = await request.json().catch(() => null);
  const role = body?.role;
  const content = typeof body?.content === "string" ? body.content : "";
  const rawClientMessageId = body?.client_message_id ?? body?.clientMessageId;
  const clientMessageId =
    typeof rawClientMessageId === "string" && rawClientMessageId.trim()
      ? rawClientMessageId.trim()
      : null;

  if (role !== "user" && role !== "assistant") {
    return structuredErrorResponse(
      requestId,
      400,
      "invalid_request",
      "role は user または assistant を指定してください",
    );
  }

  if (!content.trim()) {
    return structuredErrorResponse(
      requestId,
      400,
      "invalid_request",
      "content は必須です",
    );
  }

  if (rawClientMessageId != null && typeof rawClientMessageId !== "string") {
    return structuredErrorResponse(
      requestId,
      400,
      "invalid_request",
      "client_message_id は文字列で指定してください",
    );
  }

  if (clientMessageId && clientMessageId.length > 512) {
    return structuredErrorResponse(
      requestId,
      400,
      "invalid_request",
      "client_message_id は512文字以内で指定してください",
    );
  }

  try {
    const session = await getLiveConversationSession(id, user.id);
    if (!session) {
      return structuredErrorResponse(
        requestId,
        404,
        "session_not_found",
        "セッションが見つかりません",
      );
    }
    if (!(await canWriteConversationSession(id, user))) {
      return structuredErrorResponse(
        requestId,
        403,
        "forbidden",
        "会話への書き込み権限がありません",
      );
    }
  } catch {
    return structuredErrorResponse(
      requestId,
      503,
      "message_persistence_failed",
      "会話の状態を確認できませんでした",
      true,
    );
  }

  const now = new Date();
  const nowSql = toDbLocalTimestamp(now);

  try {
    const result = await db.transaction(async (tx) => {
      // Lock the canonical session row and revalidate it inside the same
      // transaction as the message write.  This serializes retries and
      // prevents a concurrent delete/restore from producing a partial write.
      const [lockedSession] = await tx
        .select()
        .from(conversationSessions)
        .where(
          and(
            eq(conversationSessions.id, id),
            isNull(conversationSessions.deletedAt),
          ),
        )
        .limit(1)
        .for("update");
      if (!lockedSession) {
        throw new ConversationMessageRouteError(
          "session_not_found",
          404,
          "セッションが見つかりません",
        );
      }
      // The request-level ACL check above is only an early rejection.  Re-run
      // the authoritative participant/project check after the session lock so
      // a permission revocation racing this write cannot still insert a row.
      if (
        !(
          await canWriteLockedConversationSession(
            tx as unknown as typeof db,
            lockedSession,
            user,
          )
        )
      ) {
        throw new ConversationMessageRouteError(
          "forbidden",
          403,
          "会話への書き込み権限がありません",
        );
      }

      let existing:
        | typeof conversationMessages.$inferSelect
        | undefined;
      if (clientMessageId) {
        [existing] = await tx
          .select()
          .from(conversationMessages)
          .where(
            and(
              eq(conversationMessages.sessionId, id),
              eq(conversationMessages.clientMessageId, clientMessageId),
            ),
          )
          .limit(1);
        if (existing) {
          if (!messageContentMatches(existing, role, content, user.id)) {
            throw new ConversationMessageRouteError(
              "idempotency_conflict",
              409,
              "同じclient_message_idの内容が一致しません",
            );
          }
          return { message: existing, replayed: true };
        }
      }

      const [message] = await tx
        .insert(conversationMessages)
        .values({
          sessionId: id,
          role,
          content: encryptText(content, "conversation_messages.content"),
          messageMetadata: clientMessageId
            ? { client_message_id: clientMessageId }
            : {},
          senderType: role === "user" ? "user" : null,
          senderId: (clientMessageId || role === "user") ? user.id : null,
          senderDisplayName:
            role === "user"
              ? user.displayName || user.username || user.email || user.id
              : null,
          clientMessageId,
          createdAt: now,
          branchIndex: 0,
          isActiveBranch: true,
        })
        .returning();
      if (!message) {
        throw new ConversationMessageRouteError(
          "message_persistence_failed",
          500,
          "メッセージを保存できませんでした",
          true,
        );
      }

      await tx
        .update(conversationSessions)
        .set({
          // A delayed/duplicated writer must not regress the canonical
          // activity marker used by the history ordering.  Convert the
          // timestamp at the DB boundary instead of interpolating a Date
          // object into postgres-js' raw SQL template.
          lastActivity: sql`case
            when ${conversationSessions.lastActivity} is null
              or ${conversationSessions.lastActivity} < ${nowSql}
            then ${nowSql}
            else ${conversationSessions.lastActivity}
          end`,
          messageCount: sql`coalesce(${conversationSessions.messageCount}, 0) + 1`,
        })
        .where(eq(conversationSessions.id, id));

      return { message, replayed: false };
    });

    return NextResponse.json(
      {
        success: true,
        replayed: result.replayed,
        message: messagePayload(result.message),
      },
      { headers: { "x-request-id": requestId } },
    );
  } catch (error) {
    if (error instanceof ConversationMessageRouteError) {
      return routeErrorResponse(requestId, error);
    }

    // A legacy writer may race the new partial unique index without taking
    // the session lock.  Resolve that race into the same replay/conflict
    // contract rather than leaking a postgres error to the browser.
    if (clientMessageId && isUniqueViolation(error)) {
      try {
        const [existing] = await db
          .select()
          .from(conversationMessages)
          .where(
            and(
              eq(conversationMessages.sessionId, id),
              eq(conversationMessages.clientMessageId, clientMessageId),
            ),
          )
          .limit(1);
        if (existing && messageContentMatches(existing, role, content, user.id)) {
          return NextResponse.json(
            {
              success: true,
              replayed: true,
              message: messagePayload(existing),
            },
            { headers: { "x-request-id": requestId } },
          );
        }
        if (existing) {
          return structuredErrorResponse(
            requestId,
            409,
            "idempotency_conflict",
            "同じclient_message_idの内容が一致しません",
          );
        }
      } catch {
        // Fall through to the generic sanitized persistence failure.
      }
    }

    console.error("conversation message persistence failed", {
      requestId,
      sessionId: id,
    });
    return structuredErrorResponse(
      requestId,
      500,
      "message_persistence_failed",
      "メッセージを保存できませんでした",
      true,
    );
  }
}
