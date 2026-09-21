import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import { projectQaEntries, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getProjectSettingsProject } from "@/lib/server/project-access";
import { serializeQaCandidate } from "../qa-candidates/_lib";

type Params = { params: Promise<{ id: string }> };

const REVIEWABLE_STATES = ["candidate", "rejected"] as const;

function jsonError(detail: string, status: number, code?: string) {
  return NextResponse.json(code ? { detail, code } : { detail }, { status });
}

function positiveLimit(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? Math.min(500, Math.max(1, Math.floor(parsed)))
    : 200;
}

function isAutomatic(entry: typeof projectQaEntries.$inferSelect) {
  // Semantic completed-turn rows carry durable scoped-memory provenance.  A
  // stale origin from a rolling deployment must not make those rows eligible
  // for the legacy auto-Q&A cleanup sweep.
  let refs = entry.answerSourceRefs;
  if (typeof refs === "string") {
    try {
      refs = JSON.parse(refs) as unknown;
    } catch {
      refs = null;
    }
  }
  const hasSemanticProvenance =
    Array.isArray(refs)
    && refs.some((ref) => {
      if (!ref || typeof ref !== "object") return false;
      const type = String((ref as { type?: unknown }).type || "")
        .trim()
        .toLowerCase()
        .replace(/-/g, "_");
      return [
        "scoped_memory_job",
        "project_qa_candidate",
        "memory_job",
      ].includes(type);
    });
  if (hasSemanticProvenance) {
    return false;
  }
  // ``createdByAgent`` is only a compatibility signal when a rolling
  // deployment has not hydrated the new provenance column yet. Once origin
  // is present it is authoritative: an explicit/manual candidate must never
  // be swept up merely because an old writer left the legacy boolean true.
  const origin = String(entry.origin || "").trim().toLowerCase();
  return origin ? origin === "legacy_auto" : Boolean(entry.createdByAgent);
}

/**
 * Safely clean historical auto-generated Q&A candidates.  Preview/dry-run is
 * the default; callers must explicitly send ``{dry_run:false}`` to archive
 * rows.  Accepted/manual rows are excluded in SQL and rechecked while locked
 * immediately before each soft-delete.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const user = await getSession();
  if (!user) return jsonError("認証が必要です", 401);

  const { id: projectId } = await params;
  // Bulk cleanup is a destructive settings operation, not ordinary Project
  // content editing.  Keep preview and execution on the same stronger ACL.
  const access = await getProjectSettingsProject(projectId, user);
  if (access === undefined) return jsonError("プロジェクトが見つかりません", 404);
  if (access === null) return jsonError("権限がありません", 403);
  if (access.project.isCompleted) {
    return jsonError("完了済みProjectのQ&Aはクリーンアップできません", 409, "project_completed");
  }

  const body = await request.json().catch(() => ({}));
  const dryRun = body.dry_run !== false;
  const limit = positiveLimit(body.limit);
  const eligibleWhere = and(
    eq(projectQaEntries.projectId, projectId),
    isNull(projectQaEntries.deletedAt),
    inArray(projectQaEntries.reviewState, [...REVIEWABLE_STATES]),
    or(
      eq(projectQaEntries.origin, "legacy_auto"),
      and(isNull(projectQaEntries.origin), eq(projectQaEntries.createdByAgent, true)),
    ),
  );

  try {
    if (dryRun) {
      const rows = await db
        .select()
        .from(projectQaEntries)
        .where(eligibleWhere)
        .orderBy(projectQaEntries.updatedAt);
      // Defense in depth for rolling deployments or malformed legacy rows:
      // only return rows that still satisfy the automatic signal in memory.
      const eligible = rows.filter(
        (row) =>
          isAutomatic(row)
          && REVIEWABLE_STATES.includes(
            String(row.reviewState || "").toLowerCase() as (typeof REVIEWABLE_STATES)[number],
          ),
      ).slice(0, limit);
      return NextResponse.json({
        ok: true,
        dry_run: true,
        eligible_count: eligible.length,
        eligible: eligible.map(serializeQaCandidate),
      });
    }

    const archivedIds = await db.transaction(async (tx) => {
      const [project] = await tx
        .select({ id: projects.id, isCompleted: projects.isCompleted })
        .from(projects)
        .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
        .limit(1)
        .for("update");
      if (!project || project.isCompleted) return null;

      const rows = await tx
        .select()
        .from(projectQaEntries)
        .where(eligibleWhere)
        .orderBy(projectQaEntries.updatedAt)
        .for("update");
      const ids: string[] = [];
      const now = new Date();
      for (const row of rows) {
        // Recheck all safety gates after the row lock.  In particular, a
        // candidate accepted by another tab cannot be archived just because
        // it appeared in the pre-lock query snapshot.
        if (!isAutomatic(row) || !REVIEWABLE_STATES.includes(String(row.reviewState || "").toLowerCase() as (typeof REVIEWABLE_STATES)[number])) {
          continue;
        }
        if (ids.length >= limit) break;
        const currentVersion = Math.max(1, Number(row.version || 1));
        const [archived] = await tx
          .update(projectQaEntries)
          .set({
            status: "archived",
            deletedAt: now,
            updatedAt: now,
            updatedBy: user.id,
            version: currentVersion + 1,
          })
          .where(
            and(
              eq(projectQaEntries.id, row.id),
              eq(projectQaEntries.projectId, projectId),
              isNull(projectQaEntries.deletedAt),
              inArray(projectQaEntries.reviewState, [...REVIEWABLE_STATES]),
              or(
                eq(projectQaEntries.origin, "legacy_auto"),
                and(isNull(projectQaEntries.origin), eq(projectQaEntries.createdByAgent, true)),
              ),
              eq(projectQaEntries.version, currentVersion),
            ),
          )
          .returning({ id: projectQaEntries.id });
        if (archived) ids.push(archived.id);
      }
      return ids;
    });

    if (archivedIds === null) {
      return jsonError("プロジェクトが見つかりません", 404);
    }
    return NextResponse.json({
      ok: true,
      dry_run: false,
      archived_count: archivedIds.length,
      archived_ids: archivedIds,
    });
  } catch (error) {
    console.error("Project Q&A cleanup failed", { projectId, dryRun, error });
    return jsonError("Q&A候補をクリーンアップできませんでした", 500, "qa_cleanup_unavailable");
  }
}
