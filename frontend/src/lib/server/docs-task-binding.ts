import { and, eq, inArray, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeSupertags,
  taskSchedulePlacements,
  tasks,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { fetchPythonApi, type InternalPythonUser } from "@/lib/server/python-api-proxy";
import { ManagedDocsMutationError } from "@/lib/server/managed-docs-policy";
import {
  getReadableProjectIds,
  type SessionUser,
} from "@/lib/server/task-route-utils";
import {
  lockTaskProjectMoveAndAssertAuthorized,
  TaskProjectMoveInvariantError,
} from "@/lib/server/project-move-dependency-invariant";

type DocsNodeForTaskBinding = {
  id: string;
  projectId: string | null;
  title: string;
};

type DocsFieldForTaskProxy = {
  id: string;
  systemKey: string | null;
};

type DocsTaskProxyTransaction = Pick<
  typeof db,
  "delete" | "execute" | "select" | "update"
>;

export type DocsTaskSyntheticFieldValue = {
  nodeId: string;
  fieldId: string;
  valueJson: unknown;
  valueText: string | null;
  valueNumber: number | null;
  valueDatetime: Date | null;
  targetNodeId: string | null;
  updatedAt: Date;
  updatedBy: string | null;
};

async function hasTaskSystemTag(docsLibraryId: string, tagIds: string[]) {
  if (tagIds.length === 0) return false;
  const [tag] = await db
    .select({ id: knowledgeSupertags.id })
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.docsLibraryId, docsLibraryId),
        eq(knowledgeSupertags.systemKey, "task"),
        inArray(knowledgeSupertags.id, tagIds),
      ),
    )
    .limit(1);
  return Boolean(tag);
}

async function assertPythonOk(response: Response, action: string) {
  if (response.ok) return;
  const detail = await response.text().catch(() => "");
  throw new Error(`${action} failed: ${response.status} ${detail}`);
}

export async function reconcileDocsTaskBinding(options: {
  user: InternalPythonUser;
  docsLibraryId: string;
  node: DocsNodeForTaskBinding;
  previousSupertagIds: string[];
  nextSupertagIds: string[];
}) {
  const [hadTaskTag, hasTaskTag] = await Promise.all([
    hasTaskSystemTag(options.docsLibraryId, options.previousSupertagIds),
    hasTaskSystemTag(options.docsLibraryId, options.nextSupertagIds),
  ]);
  if (hadTaskTag === hasTaskTag) return;

  const linkedTasks = await db
    .select({ id: tasks.id })
    .from(tasks)
    .where(and(eq(tasks.knowledgeNodeId, options.node.id), isNull(tasks.deletedAt)))
    .limit(10);

  if (hasTaskTag) {
    if (linkedTasks.length > 0) return;
    const response = await fetchPythonApi("/api/tasks", {
      method: "POST",
      user: options.user,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        project_id: options.node.projectId,
        knowledge_node_id: options.node.id,
        title: options.node.title || "Untitled task",
        status: "todo",
        source: "docs",
        task_metadata: {
          source: "docs",
          knowledge_node_id: options.node.id,
        },
      }),
    });
    await assertPythonOk(response, "Docs task binding create");
    return;
  }

  await Promise.all(
    linkedTasks.map(async (task) => {
      const response = await fetchPythonApi(`/api/tasks/${task.id}`, {
        method: "PATCH",
        user: options.user,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ knowledge_node_id: null }),
      });
      await assertPythonOk(response, "Docs task binding unlink");
    }),
  );
}

function fieldValueText(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const key of ["value", "text", "id", "target_node_id"]) {
      const nested = fieldValueText(record[key]);
      if (nested) return nested;
    }
  }
  return null;
}

export async function applyDocsTaskFieldProxies(options: {
  user: InternalPythonUser;
  nodeId: string;
  fieldsById: Map<string, DocsFieldForTaskProxy>;
  requestedValues: unknown[];
  transaction?: DocsTaskProxyTransaction;
}): Promise<Set<string>> {
  const systemFieldIds = new Set<string>();
  const patch: Partial<typeof tasks.$inferInsert> = {};

  for (const item of options.requestedValues) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const fieldId = typeof record.field_id === "string" ? record.field_id : "";
    const field = options.fieldsById.get(fieldId);
    if (!field?.systemKey) continue;
    systemFieldIds.add(fieldId);
    const value = fieldValueText(record.value);
    // status / priority は NOT NULL 列のため、値が無い場合は更新対象にしない
    if (field.systemKey === "task_status" && value !== null) patch.status = value;
    if (field.systemKey === "task_due") patch.endAt = value;
    if (field.systemKey === "task_start") patch.startAt = value;
    if (field.systemKey === "task_priority" && value !== null) patch.priority = value;
    if (field.systemKey === "task_project" && value) patch.projectId = value;
  }

  if (Object.keys(patch).length === 0) return systemFieldIds;

  const queryClient = options.transaction ?? db;
  const [linkedTask] = await queryClient
    .select({ id: tasks.id, projectId: tasks.projectId })
    .from(tasks)
    .where(and(eq(tasks.knowledgeNodeId, options.nodeId), isNull(tasks.deletedAt)))
    .limit(1);
  if (!linkedTask) return systemFieldIds;

  if (patch.projectId && patch.projectId !== linkedTask.projectId) {
    try {
      const moveTask = async (tx: DocsTaskProxyTransaction) => {
        const move = await lockTaskProjectMoveAndAssertAuthorized(tx, {
          taskId: linkedTask.id,
          expectedProjectId: linkedTask.projectId,
          targetProjectId: patch.projectId as string,
          actor: options.user,
        });
        const childRows =
          typeof (tx as { execute?: unknown }).execute === "function"
            ? await tx
                .select({ childId: tasks.id })
                .from(tasks)
                .where(
                  and(
                    eq(tasks.parentTaskId, linkedTask.id),
                    isNull(tasks.deletedAt),
                  ),
                )
                .limit(1)
            : [];
        const child = childRows[0] as { childId?: string } | undefined;
        // Legacy transaction test doubles expose the linked task as ``id``;
        // only a real child projection (``childId``) is an invariant hit.
        if (child?.childId) {
          throw new TaskProjectMoveInvariantError(
            409,
            "task_project_move_has_children",
            "子タスクがある親タスクは別のプロジェクトへ移動できません",
          );
        }
        const movePatch: Partial<typeof tasks.$inferInsert> = {
          ...patch,
          parentTaskId: null,
          updatedAt: new Date(),
        };
        if (
          move.task.parentTaskId ||
          move.sourceProject.spaceId !== move.targetProject.spaceId
        ) {
          const [maxRow] = await tx
            .select({ maxSort: max(tasks.sortOrder) })
            .from(tasks)
            .where(
              and(
                eq(tasks.projectId, patch.projectId as string),
                isNull(tasks.parentTaskId),
                isNull(tasks.deletedAt),
              ),
            );
          movePatch.sortOrder = (maxRow?.maxSort ?? 0) + 1;
        }
        await tx
          .update(tasks)
          .set(movePatch)
          .where(
            and(
              eq(tasks.id, linkedTask.id),
              eq(tasks.projectId, linkedTask.projectId),
              isNull(tasks.deletedAt),
            ),
          );
        // Schedule placement is scoped to the task's project.  Clear it in
        // the same transaction as a Docs-driven project move so a stale
        // phase from the source project can never survive the move.
        await tx
          .delete(taskSchedulePlacements)
          .where(eq(taskSchedulePlacements.taskId, linkedTask.id));
      };
      if (options.transaction) {
        await moveTask(options.transaction);
      } else {
        await db.transaction(moveTask);
      }
    } catch (error) {
      if (error instanceof TaskProjectMoveInvariantError) {
        const docsConflict = new ManagedDocsMutationError("task_project");
        docsConflict.message = error.message;
        throw docsConflict;
      }
      throw error;
    }
  } else {
    await queryClient
      .update(tasks)
      .set({ ...patch, updatedAt: new Date() })
      .where(eq(tasks.id, linkedTask.id));
  }
  return systemFieldIds;
}

export async function listDocsTaskSyntheticFieldValues(options: {
  nodeIds: string[];
  fields: DocsFieldForTaskProxy[];
  user: SessionUser;
}): Promise<DocsTaskSyntheticFieldValue[]> {
  const nodeIds = Array.from(new Set(options.nodeIds.filter(Boolean)));
  if (nodeIds.length === 0) return [];

  const taskFields = new Map(
    options.fields
      .filter((field) => field.systemKey?.startsWith("task_"))
      .map((field) => [field.systemKey as string, field]),
  );
  if (taskFields.size === 0) return [];

  const linkedTasks = await db
    .select()
    .from(tasks)
    .where(and(inArray(tasks.knowledgeNodeId, nodeIds), isNull(tasks.deletedAt)));

  // A Docs node can be readable through its own ACL while the linked Task's
  // Project is not readable by the actor.  Resolve the direct Task read scope
  // once, then suppress all synthetic status/date/project values for tasks
  // outside that scope.  Project-less legacy rows are retained only for the
  // owner of a personal Docs workspace (the normal schema now requires a
  // project_id, but this keeps old rows safe and deterministic).
  const readableProjectIds = new Set(await getReadableProjectIds(options.user.id));
  const projectlessNodeIds = linkedTasks
    .filter((task) => !task.projectId && task.knowledgeNodeId)
    .map((task) => task.knowledgeNodeId as string);
  const ownedProjectlessNodeIds = new Set<string>();
  if (projectlessNodeIds.length > 0) {
    const ownerRows = await db
      .select({ nodeId: knowledgeNodes.id })
      .from(knowledgeNodes)
      .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
      .where(
        and(
          inArray(knowledgeNodes.id, Array.from(new Set(projectlessNodeIds))),
          eq(docsLibraries.libraryType, "personal"),
          eq(docsLibraries.ownerUserId, options.user.id),
        ),
      );
    for (const row of ownerRows) ownedProjectlessNodeIds.add(row.nodeId);
  }
  const visibleLinkedTasks = linkedTasks.filter((task) =>
    task.projectId
      ? readableProjectIds.has(task.projectId)
      : Boolean(task.knowledgeNodeId && ownedProjectlessNodeIds.has(task.knowledgeNodeId)),
  );

  const values: DocsTaskSyntheticFieldValue[] = [];
  const pushValue = (
    nodeId: string,
    systemKey: string,
    value: string | Date | null | undefined,
    updatedAt: Date | string | null | undefined,
    updatedBy: string | null | undefined,
  ) => {
    const field = taskFields.get(systemKey);
    if (!field || value === null || value === undefined || value === "") return;
    const isDate = systemKey === "task_due" || systemKey === "task_start";
    const dateText = isDate ? String(value).slice(0, 10) : null;
    const updatedAtDate = updatedAt ? new Date(updatedAt) : new Date();
    values.push({
      nodeId,
      fieldId: field.id,
      valueJson: null,
      valueText: isDate ? dateText : systemKey === "task_project" ? null : String(value),
      valueNumber: null,
      valueDatetime: isDate ? new Date(value) : null,
      targetNodeId: systemKey === "task_project" ? String(value) : null,
      updatedAt: Number.isFinite(updatedAtDate.getTime()) ? updatedAtDate : new Date(),
      updatedBy: updatedBy ?? null,
    });
  };

  for (const task of visibleLinkedTasks) {
    if (!task.knowledgeNodeId) continue;
    pushValue(task.knowledgeNodeId, "task_status", task.status, task.updatedAt, task.createdBy);
    pushValue(task.knowledgeNodeId, "task_due", task.endAt, task.updatedAt, task.createdBy);
    pushValue(task.knowledgeNodeId, "task_start", task.startAt, task.updatedAt, task.createdBy);
    pushValue(task.knowledgeNodeId, "task_priority", task.priority, task.updatedAt, task.createdBy);
    pushValue(task.knowledgeNodeId, "task_project", task.projectId, task.updatedAt, task.createdBy);
  }

  return values;
}

export async function syncDocsTaskTitle(options: {
  user: InternalPythonUser;
  nodeId: string;
  title: string;
}) {
  const linkedTasks = await db
    .select({ id: tasks.id, title: tasks.title })
    .from(tasks)
    .where(and(eq(tasks.knowledgeNodeId, options.nodeId), isNull(tasks.deletedAt)))
    .limit(10);
  await Promise.all(
    linkedTasks
      .filter((task) => task.title !== options.title)
      .map(async (task) => {
        const response = await fetchPythonApi(`/api/tasks/${task.id}`, {
          method: "PATCH",
          user: options.user,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ title: options.title }),
        });
        await assertPythonOk(response, "Docs task title sync");
      }),
  );
}

export async function unlinkDocsTaskBinding(options: {
  user: InternalPythonUser;
  nodeId: string;
}) {
  const linkedTasks = await db
    .select({ id: tasks.id })
    .from(tasks)
    .where(and(eq(tasks.knowledgeNodeId, options.nodeId), isNull(tasks.deletedAt)))
    .limit(10);
  await Promise.all(
    linkedTasks.map(async (task) => {
      const response = await fetchPythonApi(`/api/tasks/${task.id}`, {
        method: "PATCH",
        user: options.user,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ knowledge_node_id: null }),
      });
      await assertPythonOk(response, "Docs task binding unlink");
    }),
  );
}
