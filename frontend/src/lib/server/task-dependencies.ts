import { and, asc, eq, inArray, isNull, or } from "drizzle-orm";
import { alias } from "drizzle-orm/pg-core";
import { db } from "@/db";
import { taskDependencies, tasks } from "@/db/schema";
import { serializeDbTimestamp } from "@/lib/server/db-time";
import {
  getProjectAccess,
  normalizeOptionalUuid,
  type SessionUser,
} from "@/lib/server/task-route-utils";
import { lockTaskProjectIds } from "@/lib/server/project-move-dependency-invariant";

export type DependencyTaskRow = {
  id: string;
  projectId: string;
  source: string | null;
  deletedAt: Date | string | null;
};

export type TaskDependency = {
  id: string;
  task_id: string;
  depends_on_task_id: string;
  created_at: string | null;
};

export type TaskDependencyEdge = {
  taskId: string;
  dependsOnTaskId: string;
};

export class TaskDependencyServiceError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "TaskDependencyServiceError";
  }
}

export function requireTaskDependencyUuid(
  value: unknown,
  label: string,
): string {
  const normalized = normalizeOptionalUuid(value);
  if (!normalized) {
    throw new TaskDependencyServiceError(
      400,
      `${label}はUUID形式で指定してください`,
    );
  }
  return normalized;
}

export function wouldCreateTaskDependencyCycle(
  edges: readonly TaskDependencyEdge[],
  candidate: TaskDependencyEdge,
): boolean {
  const outgoing = new Map<string, string[]>();
  for (const edge of edges) {
    const targets = outgoing.get(edge.dependsOnTaskId) ?? [];
    targets.push(edge.taskId);
    outgoing.set(edge.dependsOnTaskId, targets);
  }

  const pending = [candidate.taskId];
  const visited = new Set<string>();
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current || visited.has(current)) continue;
    if (current === candidate.dependsOnTaskId) return true;
    visited.add(current);
    pending.push(...(outgoing.get(current) ?? []));
  }
  return false;
}

function taskRowSelection() {
  return {
    id: tasks.id,
    projectId: tasks.projectId,
    source: tasks.source,
    deletedAt: tasks.deletedAt,
  };
}

export function validateTaskDependencyTaskPair(
  rows: readonly DependencyTaskRow[],
  taskId: string,
  dependsOnTaskId: string,
): { task: DependencyTaskRow; prerequisite: DependencyTaskRow } {
  if (taskId === dependsOnTaskId) {
    throw new TaskDependencyServiceError(
      400,
      "タスクを自分自身の前提タスクには設定できません",
    );
  }
  const byId = new Map(rows.map((row) => [row.id, row]));
  const task = byId.get(taskId);
  const prerequisite = byId.get(dependsOnTaskId);
  if (!task || !prerequisite) {
    throw new TaskDependencyServiceError(
      404,
      "対象タスクが見つかりません",
    );
  }
  if (task.deletedAt || prerequisite.deletedAt) {
    throw new TaskDependencyServiceError(
      400,
      "削除済みのタスクには依存関係を設定できません",
    );
  }
  if (task.source === "remote" || prerequisite.source === "remote") {
    throw new TaskDependencyServiceError(
      400,
      "参照専用のリモートタスクには依存関係を設定できません",
    );
  }
  if (task.projectId !== prerequisite.projectId) {
    throw new TaskDependencyServiceError(
      400,
      "異なるプロジェクトのタスク間には依存関係を設定できません",
    );
  }
  return { task, prerequisite };
}

export function validateTaskDependencyGraph(
  edges: readonly TaskDependencyEdge[],
  candidate: TaskDependencyEdge,
): void {
  if (
    edges.some(
      (edge) =>
        edge.taskId === candidate.taskId &&
        edge.dependsOnTaskId === candidate.dependsOnTaskId,
    )
  ) {
    throw new TaskDependencyServiceError(
      409,
      "同じ依存関係が既に登録されています",
    );
  }
  if (wouldCreateTaskDependencyCycle(edges, candidate)) {
    throw new TaskDependencyServiceError(
      409,
      "この依存関係を追加すると循環が発生します",
    );
  }
}

async function authorizeTaskPair(
  user: SessionUser,
  pair: { task: DependencyTaskRow; prerequisite: DependencyTaskRow },
  write: boolean,
): Promise<void> {
  const projectIds = [...new Set([pair.task.projectId, pair.prerequisite.projectId])];
  for (const projectId of projectIds) {
    const access = await getProjectAccess(user, projectId);
    if (!access) {
      throw new TaskDependencyServiceError(404, "プロジェクトが見つかりません");
    }
    if (!access.canRead || (write && !access.canWrite)) {
      throw new TaskDependencyServiceError(403, "権限がありません");
    }
  }
}

function toTaskDependency(row: typeof taskDependencies.$inferSelect): TaskDependency {
  return {
    id: row.id,
    task_id: row.taskId,
    depends_on_task_id: row.dependsOnTaskId,
    created_at: serializeDbTimestamp(row.createdAt),
  };
}

async function requireReadableProject(user: SessionUser, projectId: string) {
  const access = await getProjectAccess(user, projectId);
  if (!access) {
    throw new TaskDependencyServiceError(404, "プロジェクトが見つかりません");
  }
  if (!access.canRead) {
    throw new TaskDependencyServiceError(403, "権限がありません");
  }
}

export async function listTaskDependencies(
  user: SessionUser,
  filters: { projectId?: string; taskId?: string },
): Promise<TaskDependency[]> {
  if (!filters.projectId && !filters.taskId) {
    throw new TaskDependencyServiceError(
      400,
      "project_idまたはtask_idを指定してください",
    );
  }

  let projectId = filters.projectId;
  if (filters.taskId) {
    const [task] = await db
      .select(taskRowSelection())
      .from(tasks)
      .where(and(eq(tasks.id, filters.taskId), isNull(tasks.deletedAt)))
      .limit(1);
    if (!task) {
      throw new TaskDependencyServiceError(404, "タスクが見つかりません");
    }
    if (projectId && task.projectId !== projectId) {
      throw new TaskDependencyServiceError(
        400,
        "指定したタスクは対象プロジェクトに属していません",
      );
    }
    projectId = task.projectId;
  }

  await requireReadableProject(user, projectId!);

  const successor = alias(tasks, "dependency_successor");
  const prerequisite = alias(tasks, "dependency_prerequisite");
  const conditions = [
    eq(successor.projectId, projectId!),
    eq(prerequisite.projectId, projectId!),
    isNull(successor.deletedAt),
    isNull(prerequisite.deletedAt),
  ];
  if (filters.taskId) {
    conditions.push(
      or(
        eq(taskDependencies.taskId, filters.taskId),
        eq(taskDependencies.dependsOnTaskId, filters.taskId),
      )!,
    );
  }

  const rows = await db
    .select({
      id: taskDependencies.id,
      taskId: taskDependencies.taskId,
      dependsOnTaskId: taskDependencies.dependsOnTaskId,
      createdAt: taskDependencies.createdAt,
    })
    .from(taskDependencies)
    .innerJoin(successor, eq(successor.id, taskDependencies.taskId))
    .innerJoin(
      prerequisite,
      eq(prerequisite.id, taskDependencies.dependsOnTaskId),
    )
    .where(and(...conditions))
    .orderBy(asc(taskDependencies.createdAt), asc(taskDependencies.id));

  return rows.map(toTaskDependency);
}

function isUniqueViolation(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const record = error as { code?: unknown; cause?: unknown };
  return record.code === "23505" || isUniqueViolation(record.cause);
}

export async function createTaskDependency(
  user: SessionUser,
  input: TaskDependencyEdge,
): Promise<TaskDependency> {
  const initialRows = await db
    .select(taskRowSelection())
    .from(tasks)
    .where(inArray(tasks.id, [input.taskId, input.dependsOnTaskId]));
  const initialPair = validateTaskDependencyTaskPair(
    initialRows,
    input.taskId,
    input.dependsOnTaskId,
  );
  await authorizeTaskPair(user, initialPair, true);
  const initialProjectId = initialPair.task.projectId;

  try {
    return await db.transaction(async (tx) => {
      // Dependency CRUD and project moves share the same sorted advisory lock
      // namespace. Re-read both task rows only after all involved project
      // locks are held, then validate their current project identity.
      await lockTaskProjectIds(tx, [
        initialPair.task.projectId,
        initialPair.prerequisite.projectId,
      ]);

      const finalRows = await tx
        .select(taskRowSelection())
        .from(tasks)
        .where(inArray(tasks.id, [input.taskId, input.dependsOnTaskId]))
        .orderBy(asc(tasks.id))
        .for("update");
      const finalTask = finalRows.find((row) => row.id === input.taskId);
      const finalPrerequisite = finalRows.find(
        (row) => row.id === input.dependsOnTaskId,
      );
      if (
        finalTask?.projectId !== initialPair.task.projectId ||
        finalPrerequisite?.projectId !== initialPair.prerequisite.projectId
      ) {
        throw new TaskDependencyServiceError(
          409,
          "タスク情報が更新されました。もう一度お試しください",
        );
      }
      const finalPair = validateTaskDependencyTaskPair(
        finalRows,
        input.taskId,
        input.dependsOnTaskId,
      );
      await authorizeTaskPair(user, finalPair, true);
      const finalProjectId = finalPair.task.projectId;
      if (
        finalProjectId !== initialProjectId ||
        finalPair.prerequisite.projectId !== initialPair.prerequisite.projectId
      ) {
        throw new TaskDependencyServiceError(
          409,
          "タスク情報が更新されました。もう一度お試しください",
        );
      }

      const successor = alias(tasks, "cycle_successor");
      const prerequisite = alias(tasks, "cycle_prerequisite");
      const edges = await tx
        .select({
          taskId: taskDependencies.taskId,
          dependsOnTaskId: taskDependencies.dependsOnTaskId,
        })
        .from(taskDependencies)
        .innerJoin(successor, eq(successor.id, taskDependencies.taskId))
        .innerJoin(
          prerequisite,
          eq(prerequisite.id, taskDependencies.dependsOnTaskId),
        )
        .where(
          and(
            eq(successor.projectId, finalProjectId),
            eq(prerequisite.projectId, finalProjectId),
            isNull(successor.deletedAt),
            isNull(prerequisite.deletedAt),
          ),
        );

      validateTaskDependencyGraph(edges, input);

      const [created] = await tx
        .insert(taskDependencies)
        .values({
          taskId: input.taskId,
          dependsOnTaskId: input.dependsOnTaskId,
        })
        .returning();
      if (!created) {
        throw new Error("task dependency insert returned no row");
      }
      return toTaskDependency(created);
    });
  } catch (error) {
    if (error instanceof TaskDependencyServiceError) throw error;
    if (isUniqueViolation(error)) {
      throw new TaskDependencyServiceError(
        409,
        "同じ依存関係が既に登録されています",
      );
    }
    throw error;
  }
}

export async function deleteTaskDependency(
  user: SessionUser,
  dependencyId: string,
): Promise<void> {
  const [initialDependency] = await db
    .select()
    .from(taskDependencies)
    .where(eq(taskDependencies.id, dependencyId))
    .limit(1);
  if (!initialDependency) {
    throw new TaskDependencyServiceError(404, "依存関係が見つかりません");
  }

  const initialRows = await db
    .select(taskRowSelection())
    .from(tasks)
    .where(
      inArray(tasks.id, [
        initialDependency.taskId,
        initialDependency.dependsOnTaskId,
      ]),
    );
  const initialPair = validateTaskDependencyTaskPair(
    initialRows,
    initialDependency.taskId,
    initialDependency.dependsOnTaskId,
  );
  await authorizeTaskPair(user, initialPair, true);
  const initialProjectId = initialPair.task.projectId;

  await db.transaction(async (tx) => {
    await lockTaskProjectIds(tx, [
      initialPair.task.projectId,
      initialPair.prerequisite.projectId,
    ]);
    const [dependencySnapshot] = await tx
      .select()
      .from(taskDependencies)
      .where(eq(taskDependencies.id, dependencyId))
      .limit(1);
    if (!dependencySnapshot) {
      throw new TaskDependencyServiceError(404, "依存関係が見つかりません");
    }

    const finalRows = await tx
      .select(taskRowSelection())
      .from(tasks)
      .where(
        inArray(tasks.id, [
          dependencySnapshot.taskId,
          dependencySnapshot.dependsOnTaskId,
        ]),
      )
      .orderBy(asc(tasks.id))
      .for("update");
    const [dependency] = await tx
      .select()
      .from(taskDependencies)
      .where(eq(taskDependencies.id, dependencyId))
      .for("update")
      .limit(1);
    if (!dependency) {
      throw new TaskDependencyServiceError(404, "依存関係が見つかりません");
    }
    const finalTask = finalRows.find((row) => row.id === dependency.taskId);
    const finalPrerequisite = finalRows.find(
      (row) => row.id === dependency.dependsOnTaskId,
    );
    if (
      finalTask?.projectId !== initialPair.task.projectId ||
      finalPrerequisite?.projectId !== initialPair.prerequisite.projectId
    ) {
      throw new TaskDependencyServiceError(
        409,
        "タスク情報が更新されました。もう一度お試しください",
      );
    }
    const finalPair = validateTaskDependencyTaskPair(
      finalRows,
      dependency.taskId,
      dependency.dependsOnTaskId,
    );
    await authorizeTaskPair(user, finalPair, true);
    const finalProjectId = finalPair.task.projectId;
    if (
      finalProjectId !== initialProjectId ||
      finalPair.prerequisite.projectId !== initialPair.prerequisite.projectId
    ) {
      throw new TaskDependencyServiceError(
        409,
        "タスク情報が更新されました。もう一度お試しください",
      );
    }

    const [deleted] = await tx
      .delete(taskDependencies)
      .where(eq(taskDependencies.id, dependencyId))
      .returning({ id: taskDependencies.id });
    if (!deleted) {
      throw new TaskDependencyServiceError(404, "依存関係が見つかりません");
    }
  });
}
