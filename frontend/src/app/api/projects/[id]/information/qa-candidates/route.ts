import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import { projectQaEntries } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import { serializeQaCandidate } from "./_lib";

type Params = { params: Promise<{ id: string }> };

function errorResponse(detail: string, status: number, code?: string) {
  return NextResponse.json(code ? { detail, code } : { detail }, { status });
}

/**
 * List the Project Q&A review queue.  The normal information GET deliberately
 * returns accepted rows only; this endpoint is the explicit candidate/reject
 * review surface and therefore never returns accepted or deleted entries.
 */
export async function GET(request: NextRequest, { params }: Params) {
  const user = await getSession();
  if (!user) return errorResponse("認証が必要です", 401);

  const { id: projectId } = await params;
  // Candidate text is a review surface, not ordinary project context. Keep
  // the read behind the same write ACL required by accept/reject/delete so a
  // read-only project member cannot enumerate inferred artifacts.
  const access = await getWritableProject(projectId, user);
  if (access === undefined) return errorResponse("プロジェクトが見つかりません", 404);
  if (access === null) return errorResponse("権限がありません", 403);

  const query = request.nextUrl.searchParams;
  const requestedState = query.get("review_state") || query.get("state");
  const stateFilter = requestedState === "candidate" || requestedState === "rejected"
    ? eq(projectQaEntries.reviewState, requestedState)
    : or(
      eq(projectQaEntries.reviewState, "candidate"),
      eq(projectQaEntries.reviewState, "rejected"),
    );
  const requestedStatus = query.get("status");
  const statusFilter = requestedStatus
    && ["unanswered", "answered", "stale", "cancelled", "archived"].includes(requestedStatus)
    ? eq(projectQaEntries.status, requestedStatus)
    : undefined;
  const parsedLimit = Number(query.get("limit") || 100);
  const limit = Number.isFinite(parsedLimit)
    ? Math.min(200, Math.max(1, Math.floor(parsedLimit)))
    : 100;
  const parsedOffset = Number(query.get("offset") || 0);
  const offset = Number.isFinite(parsedOffset)
    ? Math.max(0, Math.floor(parsedOffset))
    : 0;

  try {
    const entries = await db
      .select()
      .from(projectQaEntries)
      .where(
        and(
          eq(projectQaEntries.projectId, projectId),
          isNull(projectQaEntries.deletedAt),
          stateFilter,
          statusFilter,
        ),
      )
      .orderBy(desc(projectQaEntries.updatedAt))
      .limit(limit)
      .offset(offset);
    // Keep a second in-memory boundary for rolling deployments/compatibility
    // mocks: accepted or tombstoned rows must never escape the review queue
    // even if a future query refactor accidentally broadens the SQL clause.
    const eligible = entries.filter(
      (entry) =>
        entry.deletedAt == null
        && (entry.reviewState === "candidate" || entry.reviewState === "rejected")
        && (!statusFilter || entry.status === requestedStatus),
    );
    return NextResponse.json({
      items: eligible.map(serializeQaCandidate),
      total: eligible.length,
      limit,
      offset,
    });
  } catch (error) {
    console.error("Project Q&A candidate queue read failed", { projectId, error });
    return errorResponse("Q&A候補を読み込めませんでした", 500, "qa_candidate_unavailable");
  }
}
