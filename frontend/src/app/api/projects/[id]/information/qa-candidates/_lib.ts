import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { projectQaEntries, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded } from "@/lib/server/field-crypto";
import { getWritableProject } from "@/lib/server/project-access";

export type QaCandidateAction = "accept" | "reject" | "delete";

const REVIEWABLE_STATES = new Set(["candidate", "rejected"]);

function responseFor(status: number, detail: string, code?: string) {
  return NextResponse.json(code ? { detail, code } : { detail }, { status });
}

function parsePositiveVersion(value: unknown): number | null {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) return null;
  return parsed;
}

/**
 * Keep the queue DTO body-safe.  Q&A payloads are encrypted at rest; only the
 * two user-facing text fields are decrypted and no raw transcript is exposed.
 */
export function serializeQaCandidate(
  entry: typeof projectQaEntries.$inferSelect,
) {
  return {
    id: entry.id,
    project_id: entry.projectId,
    knowledge_node_id: entry.knowledgeNodeId,
    question:
      decryptTextIfNeeded(entry.question, "project_qa_entries.question") || "",
    answer:
      entry.answer == null
        ? null
        : decryptTextIfNeeded(entry.answer, "project_qa_entries.answer") || "",
    status: entry.status,
    review_state: entry.reviewState,
    confidence: entry.confidence,
    asked_count: entry.askedCount,
    source_session_id: entry.sourceSessionId,
    source_message_ids: Array.isArray(entry.sourceMessageIds)
      ? entry.sourceMessageIds
      : [],
    source_agent_run_ids: Array.isArray(entry.sourceAgentRunIds)
      ? entry.sourceAgentRunIds
      : [],
    source_tool_call_ids: Array.isArray(entry.sourceToolCallIds)
      ? entry.sourceToolCallIds
      : [],
    answer_source_refs: Array.isArray(entry.answerSourceRefs)
      ? entry.answerSourceRefs
      : [],
    // Be defensive for rolling deployments where an old row may still be
    // hydrated without the new column.
    origin:
      entry.origin
      || (entry.createdByAgent
        && (entry.reviewState === "candidate" || entry.reviewState === "rejected")
        ? "legacy_auto"
        : "manual"),
    created_by: entry.createdBy,
    updated_by: entry.updatedBy,
    created_by_agent: Boolean(entry.createdByAgent),
    version: Math.max(1, Number(entry.version || 1)),
    created_at:
      entry.createdAt instanceof Date
        ? entry.createdAt.toISOString()
        : entry.createdAt,
    updated_at:
      entry.updatedAt instanceof Date
        ? entry.updatedAt.toISOString()
        : entry.updatedAt,
    last_asked_at:
      entry.lastAskedAt instanceof Date
        ? entry.lastAskedAt.toISOString()
        : entry.lastAskedAt,
    deleted_at:
      entry.deletedAt instanceof Date
        ? entry.deletedAt.toISOString()
        : entry.deletedAt,
  };
}

/**
 * Mutate one reviewable Q&A row with an optimistic version check.  The row is
 * locked after the Project scope has been checked, and every transition is a
 * soft delete/versioned update; no accepted/manual row is eligible here.
 */
export async function mutateQaCandidate(
  request: NextRequest,
  projectId: string,
  entryId: string,
  forcedAction?: QaCandidateAction,
) {
  const user = await getSession();
  if (!user) return responseFor(401, "認証が必要です");

  const access = await getWritableProject(projectId, user);
  if (access === undefined) return responseFor(404, "プロジェクトが見つかりません");
  if (access === null) return responseFor(403, "権限がありません");
  if (access.project.isCompleted) {
    return responseFor(409, "完了済みProjectのQ&A候補は変更できません", "project_completed");
  }

  const body = await request.json().catch(() => ({}));
  const requestedAction = body.action === "soft_delete" || body.action === "archive"
    ? "delete"
    : body.action === "approve"
      ? "accept"
      : body.action;
  const action = forcedAction
    || (requestedAction === "accept" || requestedAction === "reject" || requestedAction === "delete"
      ? requestedAction
      : null);
  if (!action) return responseFor(400, "action must be accept, reject, or delete");

  const expectedVersion = parsePositiveVersion(
    body.expected_version
      ?? body.version
      ?? request.nextUrl.searchParams.get("expected_version")
      ?? request.nextUrl.searchParams.get("version"),
  );
  if (expectedVersion === null) {
    return responseFor(400, "expected_version is required", "version_required");
  }

  let outcome:
    | { kind: "updated"; entry: typeof projectQaEntries.$inferSelect }
    | { kind: "not_found" }
    | { kind: "state_conflict" }
    | { kind: "version_conflict" };
  try {
    outcome = await db.transaction(async (tx) => {
      // Lock the parent first.  This prevents a concurrent Project completion
      // or deletion from racing the candidate transition.
      // Lock after bounding the lookup; this mirrors the repository's
      // canonical row-lock ordering and avoids dialect-specific builder
      // restrictions on applying LIMIT after FOR UPDATE.
      const lockedProject = await tx
        .select({ id: projects.id, isCompleted: projects.isCompleted })
        .from(projects)
        .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
        .limit(1)
        .for("update");
      const [project] = lockedProject;
      if (!project || project.isCompleted) return { kind: "not_found" };

      const [entry] = await tx
        .select()
        .from(projectQaEntries)
        .where(
          and(
            eq(projectQaEntries.id, entryId),
            eq(projectQaEntries.projectId, projectId),
            isNull(projectQaEntries.deletedAt),
          ),
        )
        .limit(1)
        .for("update");
      if (!entry) return { kind: "not_found" };

      const currentVersion = Math.max(1, Number(entry.version || 1));
      if (currentVersion !== expectedVersion) return { kind: "version_conflict" };
      if (!REVIEWABLE_STATES.has(String(entry.reviewState || "").toLowerCase())) {
        return { kind: "state_conflict" };
      }

      // Explicit/manual rows can be reviewed, but automatic cleanup must not
      // accidentally erase them.  The dedicated bulk cleanup route applies
      // the same origin guard; an explicit per-row delete remains available
      // for a user who intentionally removes a reviewable manual draft.
      const now = new Date();
      const values: Partial<typeof projectQaEntries.$inferInsert> = {
        updatedBy: user.id,
        updatedAt: now,
        version: currentVersion + 1,
      };
      if (action === "accept") {
        values.reviewState = "accepted";
      } else if (action === "reject") {
        values.reviewState = "rejected";
      } else {
        values.status = "archived";
        values.deletedAt = now;
      }

      const [updated] = await tx
        .update(projectQaEntries)
        .set(values)
        .where(
          and(
            eq(projectQaEntries.id, entryId),
            eq(projectQaEntries.projectId, projectId),
            isNull(projectQaEntries.deletedAt),
            eq(projectQaEntries.version, expectedVersion),
          ),
        )
        .returning();
      if (!updated) return { kind: "version_conflict" };
      return { kind: "updated", entry: updated };
    });
  } catch (error) {
    // Do not send SQL/crypto/provider details to the browser.  The server log
    // retains the operator evidence while the stable code is UI-safe.
    console.error("Project Q&A candidate mutation failed", {
      projectId,
      entryId,
      action,
      error,
    });
    return responseFor(500, "Q&A候補を更新できませんでした", "qa_candidate_unavailable");
  }

  if (outcome.kind === "not_found") return responseFor(404, "Q&A候補が見つかりません");
  if (outcome.kind === "version_conflict") {
    return responseFor(409, "Q&A候補が更新されています。再読み込みしてください", "version_conflict");
  }
  if (outcome.kind === "state_conflict") {
    return responseFor(409, "accepted済みQ&Aは候補レビュー経路では変更できません", "qa_state_conflict");
  }
  return NextResponse.json({ qa_entry: serializeQaCandidate(outcome.entry) });
}
