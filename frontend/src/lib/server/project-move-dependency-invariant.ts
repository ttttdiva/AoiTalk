import { and, asc, eq, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, taskDependencies, tasks } from "@/db/schema";
import { hasEffectiveProjectPermission } from "@/lib/server/project-permissions";

type ProjectMoveTransaction = Pick<typeof db, "execute" | "select">;

type ProjectMoveActor = {
  id?: string | null;
  role?: string | null;
};

type ProjectMoveProject = {
  id: string;
  spaceId: string | null;
};

export const PROJECT_MOVE_DEPENDENCY_CONFLICT_DETAIL =
  "依存関係があるタスクは別のプロジェクトへ移動できません。先に依存関係を明示的に削除してください";

// Keep this exact namespace in sync with
// src/services/task_project_invariants.py. Every task/dependency/schedule
// write path acquires these project locks in lexical order before row locks.
export const TASK_PROJECT_LOCK_NAMESPACE = "aoi-task-project-invariant:";

export class TaskProjectMoveInvariantError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "TaskProjectMoveInvariantError";
  }
}

/**
 * Dependency creation/deletion and project moves share this lock namespace.
 * Keep the order as project advisory lock -> task row lock so neither side can
 * observe a task between its dependency check and the corresponding write.
 */
export async function lockTaskProjectIds(
  transaction: ProjectMoveTransaction,
  projectIds: Iterable<string | null | undefined>,
): Promise<string[]> {
  const ordered = [...new Set([...projectIds].filter((id): id is string => Boolean(id)))]
    .sort();
  for (const projectId of ordered) {
    await transaction.execute(
      sql`select pg_advisory_xact_lock(hashtext(${`${TASK_PROJECT_LOCK_NAMESPACE}${projectId}`}))`,
    );
  }
  return ordered;
}

/**
 * Lock a task and its requested parent for a same-project reparent.
 *
 * Parent updates must use the same project advisory lock namespace as project
 * moves.  Re-read both rows after the lock so a concurrent parent project
 * move cannot be validated against a stale project id and then committed as
 * a cross-project hierarchy.
 */
export async function lockTaskParentUpdate(
  transaction: ProjectMoveTransaction,
  options: {
    taskId: string;
    expectedProjectId: string;
    parentTaskId: string | null | undefined;
  },
): Promise<{
  task: typeof tasks.$inferSelect;
  parent: typeof tasks.$inferSelect | null;
}> {
  await lockTaskProjectIds(transaction, [options.expectedProjectId]);

  if (options.parentTaskId === options.taskId) {
    throw new TaskProjectMoveInvariantError(
      400,
      "task_parent_self",
      "Task cannot be its own parent",
    );
  }

  const taskIds = [
    options.taskId,
    ...(options.parentTaskId ? [options.parentTaskId] : []),
  ];
  const lockedRows = await transaction
    .select()
    .from(tasks)
    .where(and(inArray(tasks.id, taskIds), isNull(tasks.deletedAt)))
    .orderBy(asc(tasks.id))
    .for("update");
  const lockedTask = lockedRows.find((row) => row.id === options.taskId);
  if (!lockedTask) {
    throw new TaskProjectMoveInvariantError(
      404,
      "task_not_found",
      "タスクが見つかりません",
    );
  }
  if (String(lockedTask.projectId) !== options.expectedProjectId) {
    throw new TaskProjectMoveInvariantError(
      409,
      "task_project_move_state_changed",
      "タスク情報が更新されました。もう一度お試しください",
    );
  }

  const parent = options.parentTaskId
    ? lockedRows.find((row) => row.id === options.parentTaskId) ?? null
    : null;
  if (options.parentTaskId && !parent) {
    throw new TaskProjectMoveInvariantError(
      404,
      "task_parent_not_found",
      "親タスクが見つかりません",
    );
  }
  if (parent && String(parent.projectId) !== options.expectedProjectId) {
    throw new TaskProjectMoveInvariantError(
      400,
      "task_parent_project_mismatch",
      "Subtask parent must belong to the same project",
    );
  }

  return { task: lockedTask, parent };
}

async function lockTaskProjectMove(
  transaction: ProjectMoveTransaction,
  options: {
    taskId: string;
    expectedProjectId: string;
    targetProjectId?: string;
    relatedTaskIds?: string[];
  },
): Promise<typeof tasks.$inferSelect> {
  await lockTaskProjectIds(transaction, [
    options.expectedProjectId,
    options.targetProjectId,
  ]);

  const taskIds = [...new Set([options.taskId, ...(options.relatedTaskIds ?? [])])];
  const lockedTaskQuery = transaction
    .select()
    .from(tasks)
    .where(
      and(inArray(tasks.id, taskIds), isNull(tasks.deletedAt)),
    )
    .orderBy(asc(tasks.id))
    .for("update");
  const lockedRows = options.relatedTaskIds?.length
    ? await lockedTaskQuery
    : await lockedTaskQuery.limit(1);
  const lockedTask = lockedRows.find(
    (row) => String(row.id) === options.taskId,
  );
  if (!lockedTask) {
    throw new TaskProjectMoveInvariantError(
      404,
      "task_not_found",
      "タスクが見つかりません",
    );
  }
  if (String(lockedTask.projectId) !== options.expectedProjectId) {
    throw new TaskProjectMoveInvariantError(
      409,
      "task_project_move_state_changed",
      "タスク情報が更新されました。もう一度お試しください",
    );
  }

  return lockedTask;
}

async function assertTaskHasNoDependencies(
  transaction: ProjectMoveTransaction,
  taskId: string,
): Promise<void> {
  const [dependency] = await transaction
    .select({ id: taskDependencies.id })
    .from(taskDependencies)
    .where(
      or(
        eq(taskDependencies.taskId, taskId),
        eq(taskDependencies.dependsOnTaskId, taskId),
      ),
    )
    .limit(1);
  if (dependency) {
    throw new TaskProjectMoveInvariantError(
      409,
      "task_project_move_has_dependencies",
      PROJECT_MOVE_DEPENDENCY_CONFLICT_DETAIL,
    );
  }
}

async function assertTaskHasNoChildren(
  transaction: ProjectMoveTransaction,
  taskId: string,
): Promise<void> {
  const [child] = await transaction
    .select({ id: tasks.id })
    .from(tasks)
    .where(and(eq(tasks.parentTaskId, taskId), isNull(tasks.deletedAt)))
    .limit(1);
  if (child) {
    throw new TaskProjectMoveInvariantError(
      409,
      "task_project_move_has_children",
      "子タスクがある親タスクは別のプロジェクトへ移動できません",
    );
  }
}

async function requireWritableProject(
  transaction: ProjectMoveTransaction,
  options: {
    actor: ProjectMoveActor;
    projectId: string;
    target: boolean;
  },
): Promise<ProjectMoveProject> {
  if (!options.actor.id) {
    throw new TaskProjectMoveInvariantError(
      options.target ? 403 : 404,
      options.target
        ? "task_project_move_target_forbidden"
        : "task_not_found",
      options.target
        ? "移動先プロジェクトの権限がありません"
        : "タスクが見つかりません",
    );
  }

  const [project] = await transaction
    .select({
      id: projects.id,
      ownerId: projects.ownerId,
      spaceId: projects.spaceId,
      memberPermissions: projectMembers.permissions,
    })
    .from(projects)
    .leftJoin(
      projectMembers,
      and(
        eq(projectMembers.projectId, projects.id),
        eq(projectMembers.userId, options.actor.id),
      ),
    )
    .where(and(eq(projects.id, options.projectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) {
    throw new TaskProjectMoveInvariantError(
      404,
      options.target ? "task_project_move_target_not_found" : "task_not_found",
      options.target
        ? "移動先プロジェクトが見つかりません"
        : "タスクが見つかりません",
    );
  }
  if (
    !hasEffectiveProjectPermission({
      userId: options.actor.id,
      userRole: options.actor.role,
      projectOwnerId: project.ownerId,
      memberPermissions: project.memberPermissions,
      permission: "write",
    })
  ) {
    throw new TaskProjectMoveInvariantError(
      options.target ? 403 : 404,
      options.target
        ? "task_project_move_target_forbidden"
        : "task_not_found",
      options.target
        ? "移動先プロジェクトの権限がありません"
        : "タスクが見つかりません",
    );
  }

  return { id: project.id, spaceId: project.spaceId };
}

export async function lockTaskProjectMoveAndAssertNoDependencies(
  transaction: ProjectMoveTransaction,
  options: {
    taskId: string;
    expectedProjectId: string;
    targetProjectId?: string;
    relatedTaskIds?: string[];
  },
): Promise<typeof tasks.$inferSelect> {
  const lockedTask = await lockTaskProjectMove(transaction, options);
  await assertTaskHasNoDependencies(transaction, options.taskId);

  return lockedTask;
}

export async function lockTaskProjectMoveAndAssertAuthorized(
  transaction: ProjectMoveTransaction,
  options: {
    taskId: string;
    expectedProjectId: string;
    targetProjectId: string;
    actor: ProjectMoveActor;
    relatedTaskIds?: string[];
    rejectChildren?: boolean;
  },
): Promise<{
  task: typeof tasks.$inferSelect;
  sourceProject: ProjectMoveProject;
  targetProject: ProjectMoveProject;
}> {
  const task = await lockTaskProjectMove(transaction, options);
  const sourceProject = await requireWritableProject(transaction, {
    actor: options.actor,
    projectId: options.expectedProjectId,
    target: false,
  });
  const targetProject = await requireWritableProject(transaction, {
    actor: options.actor,
    projectId: options.targetProjectId,
    target: true,
  });
  await assertTaskHasNoDependencies(transaction, options.taskId);
  if (options.rejectChildren) {
    await assertTaskHasNoChildren(transaction, options.taskId);
  }
  return { task, sourceProject, targetProject };
}
