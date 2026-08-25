import { and, asc, eq, isNull } from "drizzle-orm";

import { db } from "@/db";
import {
  projectSchedulePhases,
  taskSchedulePlacements,
  tasks,
} from "@/db/schema";
import { getProjectWithPermission } from "@/lib/server/project-access";
import { normalizeOptionalUuid } from "@/lib/server/task-route-utils";
import { lockTaskProjectIds } from "@/lib/server/project-move-dependency-invariant";

export type ScheduleUser = { id: string; role?: string | null };

export class TaskScheduleServiceError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code = "task_schedule_error",
  ) {
    super(message);
    this.name = "TaskScheduleServiceError";
  }
}

export type SchedulePhaseRecord = {
  id: string;
  project_id: string;
  name: string;
  start_on: string;
  end_on: string;
  sort_order: number;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SchedulePlacementRecord = {
  task_id: string;
  phase_id: string | null;
  x_ratio: number;
  y: number;
  created_at: string | null;
  updated_at: string | null;
};

export type ScheduleData = {
  project_id: string;
  phases: SchedulePhaseRecord[];
  placements: SchedulePlacementRecord[];
};

const DATE_ONLY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

function assertDateOnly(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new TaskScheduleServiceError(400, `${field}はYYYY-MM-DD形式で指定してください`);
  }
  const trimmed = value.trim();
  const match = DATE_ONLY_PATTERN.exec(trimmed);
  if (!match) {
    throw new TaskScheduleServiceError(400, `${field}はYYYY-MM-DD形式で指定してください`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const check = new Date(Date.UTC(year, month - 1, day));
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day
  ) {
    throw new TaskScheduleServiceError(400, `${field}が不正な日付です`);
  }
  return trimmed;
}

function assertDateRange(startOn: string, endOn: string) {
  if (endOn < startOn) {
    throw new TaskScheduleServiceError(
      400,
      "end_onはstart_on以降の日付で指定してください",
      "invalid_phase_date_range",
    );
  }
}

function assertFiniteNumber(
  value: unknown,
  field: string,
  options: { min?: number; max?: number } = {},
): number {
  if (
    typeof value !== "number" &&
    !(typeof value === "string" && value.trim().length > 0)
  ) {
    throw new TaskScheduleServiceError(400, `${field}は有限な数値で指定してください`);
  }
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    throw new TaskScheduleServiceError(400, `${field}は有限な数値で指定してください`);
  }
  if (options.min !== undefined && numeric < options.min) {
    throw new TaskScheduleServiceError(400, `${field}が小さすぎます`);
  }
  if (options.max !== undefined && numeric > options.max) {
    throw new TaskScheduleServiceError(400, `${field}が大きすぎます`);
  }
  return numeric;
}

function asIso(value: Date | string | null | undefined): string | null {
  if (!value) return null;
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

function serializePhase(
  row: typeof projectSchedulePhases.$inferSelect,
): SchedulePhaseRecord {
  return {
    id: row.id,
    project_id: row.projectId,
    name: row.name,
    start_on: row.startOn,
    end_on: row.endOn,
    sort_order: row.sortOrder,
    created_by: row.createdBy,
    created_at: asIso(row.createdAt),
    updated_at: asIso(row.updatedAt),
  };
}

function serializePlacement(
  row: typeof taskSchedulePlacements.$inferSelect,
): SchedulePlacementRecord {
  return {
    task_id: row.taskId,
    phase_id: row.phaseId,
    x_ratio: row.xRatio,
    y: row.y,
    created_at: asIso(row.createdAt),
    updated_at: asIso(row.updatedAt),
  };
}

async function lockPlacementTask(
  transaction: Pick<typeof db, "execute" | "select">,
  projectId: string,
  taskId: string,
) {
  // A move can change the task's project after the route-level ACL check. Read
  // once to discover the lock set, then acquire all project locks in sorted
  // order and re-read the task row FOR UPDATE before any phase/placement write.
  const [initial] = await transaction
    .select({ id: tasks.id, projectId: tasks.projectId, source: tasks.source })
    .from(tasks)
    .where(and(eq(tasks.id, taskId), isNull(tasks.deletedAt)))
    .limit(1);
  if (!initial) {
    throw new TaskScheduleServiceError(404, "タスクが見つかりません", "task_not_found");
  }
  await lockTaskProjectIds(transaction, [projectId, initial.projectId]);
  const [task] = await transaction
    .select({ id: tasks.id, projectId: tasks.projectId, source: tasks.source })
    .from(tasks)
    .where(and(eq(tasks.id, taskId), isNull(tasks.deletedAt)))
    .for("update")
    .limit(1);
  if (!task) {
    throw new TaskScheduleServiceError(404, "タスクが見つかりません", "task_not_found");
  }
  if (String(task.projectId) !== String(projectId)) {
    throw new TaskScheduleServiceError(
      409,
      "タスク情報が更新されました。もう一度お試しください",
      "task_project_changed",
    );
  }
  return task;
}

export async function requireScheduleProject(
  projectId: string,
  user: ScheduleUser,
  permission: "read" | "write" = "read",
) {
  const access = await getProjectWithPermission(projectId, user, permission);
  if (access === undefined) {
    throw new TaskScheduleServiceError(404, "プロジェクトが見つかりません", "project_not_found");
  }
  if (access === null) {
    throw new TaskScheduleServiceError(403, "権限がありません", "project_forbidden");
  }
  const project = access.project as typeof access.project & {
    source?: string | null;
  };
  if (
    project.source === "remote" ||
    projectId.startsWith("remote:") ||
    projectId.startsWith("remote_")
  ) {
    throw new TaskScheduleServiceError(
      400,
      "リモートプロジェクトのスケジュールは未対応",
      "remote_project_unsupported",
    );
  }
  return access;
}

export async function listProjectSchedule(
  projectId: string,
  user: ScheduleUser,
): Promise<ScheduleData> {
  await requireScheduleProject(projectId, user, "read");
  const [phaseRows, placementRows] = await Promise.all([
    db
      .select()
      .from(projectSchedulePhases)
      .where(eq(projectSchedulePhases.projectId, projectId))
      .orderBy(
        asc(projectSchedulePhases.sortOrder),
        asc(projectSchedulePhases.startOn),
        asc(projectSchedulePhases.createdAt),
      ),
    db
      .select({
        taskId: taskSchedulePlacements.taskId,
        phaseId: taskSchedulePlacements.phaseId,
        xRatio: taskSchedulePlacements.xRatio,
        y: taskSchedulePlacements.y,
        createdAt: taskSchedulePlacements.createdAt,
        updatedAt: taskSchedulePlacements.updatedAt,
      })
      .from(taskSchedulePlacements)
      .innerJoin(tasks, eq(tasks.id, taskSchedulePlacements.taskId))
      .where(and(eq(tasks.projectId, projectId), isNull(tasks.deletedAt))),
  ]);
  return {
    project_id: projectId,
    phases: phaseRows.map(serializePhase),
    placements: placementRows.map((row) =>
      serializePlacement(row as typeof taskSchedulePlacements.$inferSelect),
    ),
  };
}

export async function createSchedulePhase(
  projectId: string,
  user: ScheduleUser,
  input: Record<string, unknown>,
): Promise<SchedulePhaseRecord> {
  await requireScheduleProject(projectId, user, "write");
  const name = typeof input.name === "string" ? input.name.trim() : "";
  if (!name) {
    throw new TaskScheduleServiceError(400, "nameは必須です");
  }
  if (name.length > 255) {
    throw new TaskScheduleServiceError(400, "nameは255文字以内で指定してください");
  }
  const startOn = assertDateOnly(input.start_on, "start_on");
  const endOn = assertDateOnly(input.end_on, "end_on");
  assertDateRange(startOn, endOn);
  const sortOrder =
    input.sort_order === undefined
      ? 0
      : assertFiniteNumber(input.sort_order, "sort_order");
  const [row] = await db
    .insert(projectSchedulePhases)
    .values({
      projectId,
      name,
      startOn,
      endOn,
      sortOrder,
      createdBy: user.id,
    })
    .returning();
  if (!row) throw new TaskScheduleServiceError(500, "工程を作成できませんでした");
  return serializePhase(row);
}

export async function updateSchedulePhase(
  projectId: string,
  phaseId: string,
  user: ScheduleUser,
  input: Record<string, unknown>,
): Promise<SchedulePhaseRecord> {
  await requireScheduleProject(projectId, user, "write");
  const normalizedPhaseId = normalizeOptionalUuid(phaseId);
  if (!normalizedPhaseId) {
    throw new TaskScheduleServiceError(400, "phase_idはUUID形式で指定してください");
  }
  const [current] = await db
    .select()
    .from(projectSchedulePhases)
    .where(
      and(
        eq(projectSchedulePhases.id, normalizedPhaseId),
        eq(projectSchedulePhases.projectId, projectId),
      ),
    )
    .limit(1);
  if (!current) throw new TaskScheduleServiceError(404, "工程が見つかりません", "phase_not_found");

  const nextName =
    input.name === undefined
      ? current.name
      : typeof input.name === "string"
        ? input.name.trim()
        : "";
  if (!nextName) throw new TaskScheduleServiceError(400, "nameは必須です");
  if (nextName.length > 255) {
    throw new TaskScheduleServiceError(400, "nameは255文字以内で指定してください");
  }
  const nextStart =
    input.start_on === undefined
      ? current.startOn
      : assertDateOnly(input.start_on, "start_on");
  const nextEnd =
    input.end_on === undefined
      ? current.endOn
      : assertDateOnly(input.end_on, "end_on");
  assertDateRange(nextStart, nextEnd);
  const nextSort =
    input.sort_order === undefined
      ? current.sortOrder
      : assertFiniteNumber(input.sort_order, "sort_order");
  const [row] = await db
    .update(projectSchedulePhases)
    .set({
      name: nextName,
      startOn: nextStart,
      endOn: nextEnd,
      sortOrder: nextSort,
      updatedAt: new Date(),
    })
    .where(
      and(
        eq(projectSchedulePhases.id, normalizedPhaseId),
        eq(projectSchedulePhases.projectId, projectId),
      ),
    )
    .returning();
  if (!row) throw new TaskScheduleServiceError(404, "工程が見つかりません", "phase_not_found");
  return serializePhase(row);
}

export async function deleteSchedulePhase(
  projectId: string,
  phaseId: string,
  user: ScheduleUser,
): Promise<void> {
  await requireScheduleProject(projectId, user, "write");
  const normalizedPhaseId = normalizeOptionalUuid(phaseId);
  if (!normalizedPhaseId) {
    throw new TaskScheduleServiceError(400, "phase_idはUUID形式で指定してください");
  }
  const deleted = await db
    .delete(projectSchedulePhases)
    .where(
      and(
        eq(projectSchedulePhases.id, normalizedPhaseId),
        eq(projectSchedulePhases.projectId, projectId),
      ),
    )
    .returning({ id: projectSchedulePhases.id });
  if (deleted.length === 0) {
    throw new TaskScheduleServiceError(404, "工程が見つかりません", "phase_not_found");
  }
}

export async function upsertSchedulePlacement(
  projectId: string,
  taskId: string,
  user: ScheduleUser,
  input: Record<string, unknown>,
): Promise<SchedulePlacementRecord> {
  await requireScheduleProject(projectId, user, "write");
  if (taskId.startsWith("remote:")) {
    throw new TaskScheduleServiceError(400, "remote taskの配置は変更できません", "remote_task_unsupported");
  }
  const normalizedTaskId = normalizeOptionalUuid(taskId);
  if (!normalizedTaskId) {
    throw new TaskScheduleServiceError(400, "task_idはUUID形式で指定してください");
  }
  const phaseId = input.phase_id === null || input.phase_id === undefined
    ? null
    : normalizeOptionalUuid(input.phase_id);
  if (input.phase_id !== null && input.phase_id !== undefined && !phaseId) {
    throw new TaskScheduleServiceError(400, "phase_idはUUID形式で指定してください");
  }
  const xRatio = assertFiniteNumber(input.x_ratio, "x_ratio", { min: 0, max: 1 });
  const y = assertFiniteNumber(input.y, "y", { min: -100000, max: 100000 });
  return db.transaction(async (tx) => {
    const task = await lockPlacementTask(tx, projectId, normalizedTaskId);
    if (task.source === "remote") {
      throw new TaskScheduleServiceError(400, "remote taskの配置は変更できません", "remote_task_unsupported");
    }
    if (phaseId) {
      const [phase] = await tx
        .select({ id: projectSchedulePhases.id, projectId: projectSchedulePhases.projectId })
        .from(projectSchedulePhases)
        .where(eq(projectSchedulePhases.id, phaseId))
        .for("update")
        .limit(1);
      if (!phase || String(phase.projectId) !== String(projectId)) {
        throw new TaskScheduleServiceError(
          400,
          "phase_idは同一プロジェクトの工程を指定してください",
          "phase_project_mismatch",
        );
      }
    }
    const [row] = await tx
      .insert(taskSchedulePlacements)
      .values({ taskId: normalizedTaskId, phaseId, xRatio, y, updatedAt: new Date() })
      .onConflictDoUpdate({
        target: taskSchedulePlacements.taskId,
        set: { phaseId, xRatio, y, updatedAt: new Date() },
      })
      .returning();
    if (!row) throw new TaskScheduleServiceError(500, "タスク配置を保存できませんでした");
    return serializePlacement(row);
  });
}

export async function deleteSchedulePlacement(
  projectId: string,
  taskId: string,
  user: ScheduleUser,
): Promise<void> {
  await requireScheduleProject(projectId, user, "write");
  const normalizedTaskId = normalizeOptionalUuid(taskId);
  if (!normalizedTaskId) {
    throw new TaskScheduleServiceError(400, "task_idはUUID形式で指定してください");
  }
  await db.transaction(async (tx) => {
    await lockPlacementTask(tx, projectId, normalizedTaskId);
    await tx
      .delete(taskSchedulePlacements)
      .where(eq(taskSchedulePlacements.taskId, normalizedTaskId));
  });
}
