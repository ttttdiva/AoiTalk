import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  tasks,
  taskAssignees,
  taskTags,
  tags,
  taskComments,
  taskActivities,
  taskRecurrenceRules,
  users,
  timeEntries,
  projects,
  taskSchedulePlacements,
} from "@/db/schema";
import { eq, desc, inArray, sql, isNull, and, max } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded } from "@/lib/server/field-crypto";
import { computeNextRecurringScheduleAfter } from "@/lib/recurrence-schedule";
import { normalizeTaskStatus } from "@/lib/task-status";
import { parseRrule } from "@/lib/recurrence-rrule";
import {
  enqueueAutoSyncGoogleCalendarForTask,
} from "@/lib/server/google-calendar-auto-sync";
import {
  calculateTimerDurationSeconds,
  dbTimestampToLocalDate,
  serializeDbTimestamp,
  serializeTimerTimestamp,
  toDbLocalTimestamp,
  type DbTimestampValue,
} from "@/lib/server/db-time";
import {
  canWriteProjectId,
  canReadProjectId,
  isDateOnlyTaskInput,
  normalizeOptionalUuid,
  parseTaskWallClockDate,
  normalizeTaskTitle,
  resolveProjectTagIds,
  hasTaskBrowseScopeParams,
  resolveReadScope,
  TaskBrowseScopeError,
  stripGoogleCalendarMetadata,
  taskToSnake,
  type SessionUser,
} from "@/lib/server/task-route-utils";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";
import {
  appendKnowledgeRevision,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { updateDocsNode } from "@/lib/server/docs-node-writer";
import {
  assertTaskDocsNodeLinkAllowed,
  assertTaskProjectAccessInTransaction,
  assertTaskDocsNodeLinkAllowedInTransaction,
  TaskDocsNodeInvariantError,
  TaskProjectAccessInvariantError,
} from "@/lib/server/task-docs-node-invariant";
import {
  lockTaskProjectIds,
  lockTaskProjectRows,
  lockTaskParentUpdate,
  lockTaskProjectMoveAndAssertNoDependencies,
  TaskProjectMoveInvariantError,
} from "@/lib/server/project-move-dependency-invariant";
import {
  enqueueKnowledgeCaptureForCompletion,
  recordTaskActivity,
} from "@/lib/server/knowledge-capture-enqueue";

type TaskTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0];

function serializeAutoCloseOnDue(
  row: typeof tasks.$inferSelect,
  result: Record<string, unknown>,
): Record<string, unknown> {
  result.auto_close_on_due = row.autoCloseOnDue === true;
  return result;
}

type IncompleteSubtaskSummary = {
  id: string;
  title: string;
  status: string;
};

class IncompleteSubtasksConfirmationRequired extends Error {
  constructor(readonly subtasks: IncompleteSubtaskSummary[]) {
    super("未完了のサブタスクがあります");
    this.name = "IncompleteSubtasksConfirmationRequired";
  }
}

class IncompleteSubtaskPermissionDenied extends Error {
  constructor() {
    super("サブタスクを更新する権限がありません");
    this.name = "IncompleteSubtaskPermissionDenied";
  }
}

function isSerializationFailure(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code?: unknown }).code === "40001"
  );
}

async function hasWritableProjectAccess(
  user: SessionUser,
  projectId: string,
): Promise<boolean> {
  return canWriteProjectId(user, projectId);
}

async function projectsShareTaskListSpace(
  sourceProjectId: string,
  targetProjectId: string,
): Promise<boolean> {
  if (sourceProjectId === targetProjectId) return true;

  const projectRows = await db
    .select({ id: projects.id, spaceId: projects.spaceId })
    .from(projects)
    .where(inArray(projects.id, [sourceProjectId, targetProjectId]));
  const byId = new Map(projectRows.map((project) => [project.id, project]));
  const sourceProject = byId.get(sourceProjectId);
  const targetProject = byId.get(targetProjectId);
  if (!sourceProject || !targetProject) return false;

  return (sourceProject.spaceId ?? null) === (targetProject.spaceId ?? null);
}

type CalendarRelevantTask = Pick<
  typeof tasks.$inferSelect,
  | "id"
  | "title"
  | "status"
  | "startAt"
  | "endAt"
  | "allDay"
  | "reminderOffsets"
  | "notificationsEnabled"
>;

function dateSignature(value: Date | string | null | undefined): string {
  return serializeDbTimestamp(value) ?? "";
}

function jsonSignature(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function computeNextRecurringScheduleForRule(
  rule: typeof taskRecurrenceRules.$inferSelect,
  startAt: DbTimestampValue,
  endAt: DbTimestampValue,
) {
  const endCountExhausted =
    !rule.recurForever &&
    rule.endCount !== null &&
    rule.endCount !== undefined &&
    rule.endCount <= 1;
  if (endCountExhausted) return null;

  const currentStartAt = dbTimestampToLocalDate(startAt);
  if (!currentStartAt) return null;

  const parsed = parseRrule(rule.rrule);
  return computeNextRecurringScheduleAfter({
    currentStartAt,
    currentEndAt: dbTimestampToLocalDate(endAt),
    config: {
      freq: parsed.freq,
      interval: parsed.interval,
      byDay: parsed.byDay,
      skipWeekend: rule.skipWeekend,
      skipHoliday: rule.skipHoliday,
      skipMode: rule.skipMode,
      endCount: rule.recurForever ? null : rule.endCount,
      endDate: rule.recurForever
        ? null
        : rule.endDate
          ? serializeDbTimestamp(rule.endDate)
          : null,
    },
    after: currentStartAt,
  });
}

function calendarNotificationFieldsChanged(
  before: CalendarRelevantTask,
  after: CalendarRelevantTask,
): boolean {
  return (
    before.title !== after.title ||
    dateSignature(before.startAt) !== dateSignature(after.startAt) ||
    dateSignature(before.endAt) !== dateSignature(after.endAt) ||
    before.allDay !== after.allDay ||
    jsonSignature(before.reminderOffsets) !==
      jsonSignature(after.reminderOffsets) ||
    before.notificationsEnabled !== after.notificationsEnabled
  );
}

function enqueueGoogleCalendarPatchSync(
  before: CalendarRelevantTask,
  after: CalendarRelevantTask,
  user: Parameters<typeof enqueueAutoSyncGoogleCalendarForTask>[1],
) {
  const beforeStatus = normalizeTaskStatus(before.status);
  const afterStatus = normalizeTaskStatus(after.status);

  if (afterStatus === "closed") {
    enqueueAutoSyncGoogleCalendarForTask(after.id, user);
    return;
  }

  if (
    beforeStatus === "closed" ||
    calendarNotificationFieldsChanged(before, after)
  ) {
    enqueueAutoSyncGoogleCalendarForTask(after.id, user);
  }
}

function mapTimeEntry(row: {
  id: string;
  taskId: string;
  userId: string;
  startedAt: Date | string;
  endedAt: Date | string | null;
  source: string | null;
  note: string | null;
  createdAt: Date | string | null;
}) {
  return {
    id: row.id,
    task_id: row.taskId,
    user_id: row.userId,
    started_at: serializeTimerTimestamp(row.startedAt),
    ended_at: serializeTimerTimestamp(row.endedAt),
    duration_seconds:
      row.startedAt && row.endedAt
        ? calculateTimerDurationSeconds(row.startedAt, row.endedAt)
        : null,
    source: row.source,
    note: row.note,
    created_at: serializeDbTimestamp(row.createdAt),
  };
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

  let browseProjectIds: string[] | null = null;
  const searchParams = new URL(request.url).searchParams;
  if (hasTaskBrowseScopeParams(searchParams)) {
    try {
      const scope = await resolveReadScope(user, searchParams);
      if (scope.explicit) browseProjectIds = scope.projectIds;
    } catch (error) {
      if (error instanceof TaskBrowseScopeError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      throw error;
    }
  }

  const [task] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!task) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (browseProjectIds && !browseProjectIds.includes(task.projectId)) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canReadProjectId(user, task.projectId))) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  // assignees
  const assigneeRows = await db
    .select({
      id: taskAssignees.id,
      taskId: taskAssignees.taskId,
      userId: taskAssignees.userId,
      isPrimary: taskAssignees.isPrimary,
      assignedAt: taskAssignees.assignedAt,
      displayName: users.displayName,
      username: users.username,
    })
    .from(taskAssignees)
    .leftJoin(users, eq(taskAssignees.userId, users.id))
    .where(eq(taskAssignees.taskId, id));

  // tags
  const tagRows = await db
    .select({
      id: tags.id,
      spaceId: tags.spaceId,
      name: tags.name,
      color: tags.color,
      createdBy: tags.createdBy,
      createdAt: tags.createdAt,
    })
    .from(taskTags)
    .innerJoin(tags, eq(taskTags.tagId, tags.id))
    .where(eq(taskTags.taskId, id));

  // comments
  const commentRows = await db
    .select({
      id: taskComments.id,
      taskId: taskComments.taskId,
      userId: taskComments.userId,
      content: taskComments.content,
      createdAt: taskComments.createdAt,
      updatedAt: taskComments.updatedAt,
      displayName: users.displayName,
      username: users.username,
    })
    .from(taskComments)
    .leftJoin(users, eq(taskComments.userId, users.id))
    .where(eq(taskComments.taskId, id))
    .orderBy(desc(taskComments.createdAt));

  // active time entries (activities)
  const activityRows = await db
    .select()
    .from(timeEntries)
    .where(eq(timeEntries.taskId, id))
    .orderBy(desc(timeEntries.startedAt));

  const result = serializeAutoCloseOnDue(
    task,
    taskToSnake(task as unknown as Record<string, unknown>),
  );
  result.assignees = assigneeRows.map((a) => ({
    id: a.id,
    task_id: a.taskId,
    user_id: a.userId,
    is_primary: a.isPrimary,
    assigned_at: a.assignedAt,
    display_name: a.displayName,
    username: a.username,
  }));
  result.tags = tagRows.map((t) => ({
    id: t.id,
    space_id: t.spaceId,
    name: t.name,
    color: t.color,
    created_by: t.createdBy,
    created_at: t.createdAt,
  }));
  result.comments = commentRows.map((c) => ({
    id: c.id,
    task_id: c.taskId,
    user_id: c.userId,
    content: decryptTextIfNeeded(c.content, "task_comments.content"),
    created_at: c.createdAt,
    updated_at: c.updatedAt,
    display_name: c.displayName,
    username: c.username,
  }));
  result.activities = activityRows.map(mapTimeEntry);
  const activeEntry = activityRows.find(
    (entry) => entry.userId === user.id && !entry.endedAt,
  );
  result.active_time_entry = activeEntry ? mapTimeEntry(activeEntry) : null;

  // サブタスク一覧
  const subtaskRows = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.parentTaskId, id), isNull(tasks.deletedAt)))
    .orderBy(sql`${tasks.sortOrder} asc nulls last`, tasks.createdAt, tasks.id);
  const subtaskIds = subtaskRows.map((s) => s.id);
  const subtaskRecurrenceIds = new Set<string>();
  if (subtaskIds.length > 0) {
    const subRecRows = await db
      .select({ taskId: taskRecurrenceRules.taskId })
      .from(taskRecurrenceRules)
      .where(inArray(taskRecurrenceRules.taskId, subtaskIds));
    for (const r of subRecRows) subtaskRecurrenceIds.add(r.taskId);
  }
  result.subtasks = subtaskRows.map((s) => {
    const sub = serializeAutoCloseOnDue(
      s,
      taskToSnake(s as unknown as Record<string, unknown>),
    );
    sub.assignees = [];
    sub.tags = [];
    sub.active_time_entry = null;
    sub.subtasks = [];
    sub.has_recurrence = subtaskRecurrenceIds.has(s.id);
    return sub;
  });

  // このタスク自身の has_recurrence
  const [selfRec] = await db
    .select({ taskId: taskRecurrenceRules.taskId })
    .from(taskRecurrenceRules)
    .where(eq(taskRecurrenceRules.taskId, id))
    .limit(1);
  result.has_recurrence = !!selfRec;

  return NextResponse.json(result);
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json();
  const requestedDocsNodeId =
    body.knowledge_node_id !== undefined
      ? normalizeOptionalUuid(body.knowledge_node_id)
      : null;

  let [priorTask] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!priorTask) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canWriteProjectId(user, priorTask.projectId))) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  const updateData: Record<string, unknown> = { updatedAt: new Date() };
  let normalizedBodyStatus: string | undefined;
  let recurrenceRuleForStatusChange:
    | typeof taskRecurrenceRules.$inferSelect
    | null = null;
  let willTriggerRecurrence = false;
  let recurringCreatedTaskId: string | null = null;

  if (body.title !== undefined) {
    const normalizedTitle = normalizeTaskTitle(body.title);
    if (!normalizedTitle) {
      return NextResponse.json(
        { detail: "A non-placeholder title is required" },
        { status: 400 },
      );
    }
    // Validate an existing task↔Docs binding before mutating the Task row.
    // This prevents a legacy foreign/canonical binding from producing a
    // committed task title followed by a predictable 409 during Docs sync.
    if (priorTask.knowledgeNodeId) {
      try {
        await assertTaskDocsNodeLinkAllowed(
          priorTask.knowledgeNodeId,
          String(priorTask.projectId),
          user,
        );
      } catch (error) {
        if (error instanceof TaskDocsNodeInvariantError) {
          return NextResponse.json({ detail: error.message }, { status: error.status });
        }
        throw error;
      }
    }
    updateData.title = normalizedTitle;
  }
  if (body.description !== undefined) updateData.description = body.description;
  if (body.status !== undefined) {
    const nextStatus = normalizeTaskStatus(body.status);
    normalizedBodyStatus = nextStatus;
    updateData.status = nextStatus;
    if (nextStatus === "closed") {
      updateData.completedAt = toDbLocalTimestamp(new Date());
    } else {
      updateData.completedAt = null;
    }
    body.status = nextStatus;
  }
  if (
    normalizedBodyStatus !== undefined &&
    normalizeTaskStatus(priorTask.status) !== normalizedBodyStatus
  ) {
    const [rule] = await db
      .select()
      .from(taskRecurrenceRules)
      .where(eq(taskRecurrenceRules.taskId, id))
      .limit(1);
    recurrenceRuleForStatusChange = rule ?? null;
    willTriggerRecurrence =
      !!rule &&
      normalizeTaskStatus(rule.triggerStatus) === normalizedBodyStatus;
  }
  if (body.status === undefined && body.completed_at !== undefined) {
    const parsedCompletedAt = parseTaskWallClockDate(body.completed_at);
    updateData.completedAt = parsedCompletedAt
      ? toDbLocalTimestamp(parsedCompletedAt)
      : null;
  }
  if (body.priority !== undefined) updateData.priority = body.priority;
  if (body.start_at !== undefined) {
    const parsedStartAt = parseTaskWallClockDate(body.start_at);
    updateData.startAt = parsedStartAt
      ? toDbLocalTimestamp(parsedStartAt)
      : null;
  }
  if (body.end_at !== undefined) {
    const parsedEndAt = parseTaskWallClockDate(body.end_at);
    updateData.endAt = parsedEndAt ? toDbLocalTimestamp(parsedEndAt) : null;
  }
  if (body.all_day !== undefined) updateData.allDay = body.all_day;
  else if (
    (body.start_at !== undefined && isDateOnlyTaskInput(body.start_at)) ||
    (body.end_at !== undefined && isDateOnlyTaskInput(body.end_at))
  ) {
    updateData.allDay = true;
  }
  if (body.reminder_offsets !== undefined)
    updateData.reminderOffsets = Array.isArray(body.reminder_offsets)
      ? body.reminder_offsets
      : null;
  if (body.notifications_enabled !== undefined)
    updateData.notificationsEnabled = !!body.notifications_enabled;
  if (body.auto_close_on_due !== undefined) {
    updateData.autoCloseOnDue = Boolean(body.auto_close_on_due);
  }
  const requestedParentTaskId =
    body.parent_task_id !== undefined
      ? normalizeOptionalUuid(body.parent_task_id)
      : priorTask.parentTaskId;
  if (body.parent_task_id !== undefined) {
    updateData.parentTaskId = requestedParentTaskId;
  }
  const hasRequestedProjectId = Object.prototype.hasOwnProperty.call(
    body,
    "project_id",
  );
  if (hasRequestedProjectId && body.project_id === null) {
    return NextResponse.json(
      { detail: "project_id cannot be null" },
      { status: 400 },
    );
  }
  const requestedProjectId = hasRequestedProjectId
    ? String(body.project_id)
    : null;
  const hasExplicitProjectMoveIntent = body.project_move_intent === true;
  const suppressProjectChange =
    willTriggerRecurrence &&
    requestedProjectId !== null &&
    requestedProjectId !== String(priorTask.projectId);
  const projectWillChange =
    requestedProjectId !== null &&
    !suppressProjectChange &&
    requestedProjectId !== String(priorTask.projectId);
  if (projectWillChange && priorTask.knowledgeNodeId) {
    return NextResponse.json(
      { detail: "Docs連携済みタスクは先にDocs連携を解除してからProjectを変更してください" },
      { status: 409 },
    );
  }
  if (projectWillChange && !hasExplicitProjectMoveIntent) {
    return NextResponse.json(
      {
        detail:
          "project_id変更には明示的なproject_move_intentが必要です",
      },
      { status: 400 },
    );
  }
  if (
    requestedProjectId !== null &&
    !suppressProjectChange &&
    requestedProjectId !== String(priorTask.projectId) &&
    !(await hasWritableProjectAccess(user, requestedProjectId))
  ) {
    return NextResponse.json(
      { detail: "移動先プロジェクトの権限がありません" },
      { status: 403 },
    );
  }
  if (projectWillChange && requestedParentTaskId) {
    const [parentTask] = await db
      .select({ projectId: tasks.projectId })
      .from(tasks)
      .where(and(eq(tasks.id, requestedParentTaskId), isNull(tasks.deletedAt)))
      .limit(1);
    if (!parentTask || String(parentTask.projectId) !== requestedProjectId) {
      return NextResponse.json(
        { detail: "parent_task_id must belong to the target project" },
        { status: 400 },
      );
    }
  }
  if (body.project_id !== undefined && !suppressProjectChange)
    updateData.projectId = body.project_id;
  if (projectWillChange) {
    const nextParentTaskId =
      body.parent_task_id !== undefined ? requestedParentTaskId : null;
    updateData.parentTaskId = nextParentTaskId;

    const shouldKeepTaskListOrder =
      !priorTask.parentTaskId &&
      !nextParentTaskId &&
      (await projectsShareTaskListSpace(
        String(priorTask.projectId),
        requestedProjectId,
      ));

    if (!shouldKeepTaskListOrder) {
      const sortScope = nextParentTaskId
        ? and(
            eq(tasks.projectId, requestedProjectId),
            eq(tasks.parentTaskId, nextParentTaskId),
            isNull(tasks.deletedAt),
          )
        : and(
            eq(tasks.projectId, requestedProjectId),
            isNull(tasks.parentTaskId),
            isNull(tasks.deletedAt),
          );
      const [maxRow] = await db
        .select({ maxSort: max(tasks.sortOrder) })
        .from(tasks)
        .where(sortScope);
      updateData.sortOrder = (maxRow?.maxSort ?? 0) + 1;
    }
  }
  const tagProjectId =
    requestedProjectId !== null && !suppressProjectChange
      ? requestedProjectId
      : String(priorTask.projectId);
  const tagResolution =
    body.tag_ids !== undefined
      ? await resolveProjectTagIds(tagProjectId, body.tag_ids)
      : null;
  if (tagResolution && tagResolution.invalidTagIds.length > 0) {
    return NextResponse.json(
      { detail: "タスクのスペース外タグは指定できません" },
      { status: 400 },
    );
  }
  if (body.estimated_hours !== undefined)
    updateData.estimatedHours =
      body.estimated_hours != null ? Number(body.estimated_hours) : null;
  if (body.knowledge_node_id !== undefined) {
    // The normalized binding id is computed once above so transaction paths
    // can revalidate it immediately before the Task write.
    if (requestedDocsNodeId) {
      try {
        await assertTaskDocsNodeLinkAllowed(
          requestedDocsNodeId,
          String(
            projectWillChange && requestedProjectId
              ? requestedProjectId
              : priorTask.projectId,
          ),
          user,
        );
      } catch (error) {
        if (error instanceof TaskDocsNodeInvariantError) {
          return NextResponse.json({ detail: error.message }, { status: error.status });
        }
        throw error;
      }
    }
    updateData.knowledgeNodeId = requestedDocsNodeId;
  }
  if (
    body.metadata !== undefined &&
    body.metadata &&
    typeof body.metadata === "object" &&
    !Array.isArray(body.metadata)
  ) {
    updateData.taskMetadata = body.metadata;
  }
  // Project moves are incompatible with a Docs binding.  Keep the
  // preflight identity so the transaction can reject a binding that appears
  // after this snapshot but before the locked Task row is updated.
  const preflightKnowledgeNodeId = priorTask.knowledgeNodeId ?? null;

  const syncTaskTitleAndDocsInTransaction = async (
    tx: TaskTransaction,
    nextTask: typeof tasks.$inferSelect,
    previousTask: typeof tasks.$inferSelect,
  ) => {
    if (
      body.title === undefined
      || !nextTask.knowledgeNodeId
      || nextTask.title === previousTask.title
    ) {
      return;
    }
    // Keep the Task title and its future Docs mutation target in one
    // transaction.  The invariant helper rechecks the actor ACL while the
    // node/share/project rows are locked, so a revoked share cannot be used
    // for either a new binding or a title-only update.
    await assertTaskDocsNodeLinkAllowedInTransaction(
      tx,
      nextTask.knowledgeNodeId,
      String(nextTask.projectId),
      user,
    );
    const linkedNode = await updateDocsNode(tx, nextTask.knowledgeNodeId, {
      title: nextTask.title,
      updatedBy: user.id,
      updatedAt: new Date(),
    });
    if (!linkedNode) return;
    await upsertKnowledgeSearchIndex(tx, linkedNode, linkedNode.title);
    await appendKnowledgeRevision(
      tx,
      linkedNode,
      user.id,
      "タスクタイトルをDocs nodeへ同期",
    );
  };

  const recordTaskLifecycleTransition = async (
    tx: TaskTransaction,
    previousTask: typeof tasks.$inferSelect,
    nextTask: typeof tasks.$inferSelect,
  ) => {
    const previousStatus = normalizeTaskStatus(previousTask.status);
    const nextStatus = normalizeTaskStatus(nextTask.status);
    if (previousStatus === nextStatus) return;
    const activity = await recordTaskActivity(tx, {
      taskId: nextTask.id,
      userId: user.id,
      activityType: "task_updated",
      payload: {
        status: nextStatus,
        previous_status: previousStatus,
      },
    });
    if (nextStatus === "closed" && previousStatus !== "cancelled") {
      await enqueueKnowledgeCaptureForCompletion(tx, {
        taskId: nextTask.id,
        projectId: nextTask.projectId,
        status: nextStatus,
        completedAt: nextTask.completedAt,
        activity,
        triggerUserId: user.id,
      });
    }
  };

  let recurringNextScheduleBeforeUpdate: ReturnType<
    typeof computeNextRecurringScheduleAfter
  > | null = null;
  if (willTriggerRecurrence && recurrenceRuleForStatusChange?.createNew) {
    const projectedStartAt = Object.prototype.hasOwnProperty.call(
      updateData,
      "startAt",
    )
      ? (updateData.startAt as DbTimestampValue)
      : priorTask.startAt;
    const projectedEndAt = Object.prototype.hasOwnProperty.call(
      updateData,
      "endAt",
    )
      ? (updateData.endAt as DbTimestampValue)
      : priorTask.endAt;
    recurringNextScheduleBeforeUpdate = computeNextRecurringScheduleForRule(
      recurrenceRuleForStatusChange,
      projectedStartAt,
      projectedEndAt,
    );
    // A create-new recurrence transition inserts a task after the current
    // task update.  Require read access up front only when that next task
    // actually exists, so write-only members cannot create an invisible task
    // while final/expired recurrences remain ordinary write-only updates.
    if (
      recurringNextScheduleBeforeUpdate &&
      !(await canReadProjectId(user, priorTask.projectId))
    ) {
      return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
    }
  }
  let updated: typeof tasks.$inferSelect | undefined;
  if (normalizedBodyStatus === "closed") {
    const closeTaskAndChildren = () =>
      db.transaction(
        async (tx) => {
          // All task writes coordinate with Python/dependency workers through
          // the shared project advisory namespace before any Task row lock.
          await lockTaskProjectIds(tx, [
            priorTask.projectId,
            projectWillChange ? requestedProjectId : undefined,
          ]);
          if (!projectWillChange) {
            await lockTaskProjectRows(tx, [priorTask.projectId]);
          }
          const lockedTask = projectWillChange
            ? await lockTaskProjectMoveAndAssertNoDependencies(tx, {
                taskId: id,
                expectedProjectId: String(priorTask.projectId),
                targetProjectId: projectWillChange
                  ? requestedProjectId ?? undefined
                  : undefined,
                relatedTaskIds:
                  projectWillChange && requestedParentTaskId
                    ? [requestedParentTaskId]
                    : undefined,
              })
            : body.parent_task_id !== undefined
              ? (
                  await lockTaskParentUpdate(tx, {
                    taskId: id,
                    expectedProjectId: String(priorTask.projectId),
                    parentTaskId: requestedParentTaskId,
                  })
                ).task
            : (
                await tx
                  .select()
                  .from(tasks)
                  .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
                  .for("update")
                  .limit(1)
              )[0];
          if (!lockedTask) return undefined;

          if (projectWillChange && (preflightKnowledgeNodeId || lockedTask.knowledgeNodeId)) {
            throw new TaskProjectMoveInvariantError(
              409,
              "task_project_move_docs_binding_changed",
              "Docs連携済みタスクは先にDocs連携を解除してからProjectを変更してください",
            );
          }

          priorTask = lockedTask;
          await assertTaskProjectAccessInTransaction(
            tx,
            [
              lockedTask.projectId,
              projectWillChange ? requestedProjectId : undefined,
            ],
            user,
            {
              statusByProjectId: {
                [String(lockedTask.projectId)]: 404,
                ...(projectWillChange && requestedProjectId
                  ? { [requestedProjectId]: 403 }
                  : {}),
              },
            },
          );
          if (requestedDocsNodeId) {
            await assertTaskDocsNodeLinkAllowedInTransaction(
              tx,
              requestedDocsNodeId,
              String(
                projectWillChange && requestedProjectId
                  ? requestedProjectId
                  : lockedTask.projectId,
              ),
              user,
            );
          }
          if (!(await canWriteProjectId(user, lockedTask.projectId))) {
            throw new IncompleteSubtaskPermissionDenied();
          }
          const directChildren = await tx
            .select({
              id: tasks.id,
              title: tasks.title,
              status: tasks.status,
              projectId: tasks.projectId,
            })
            .from(tasks)
            .where(
              and(eq(tasks.parentTaskId, id), isNull(tasks.deletedAt)),
            )
            .for("update");
          if (projectWillChange && directChildren.length > 0) {
            throw new TaskProjectMoveInvariantError(
              409,
              "task_project_move_has_children",
              "子タスクがある親タスクは別のプロジェクトへ移動できません",
            );
          }
          if (projectWillChange && requestedParentTaskId) {
            const [targetParent] = await tx
              .select({ projectId: tasks.projectId })
              .from(tasks)
              .where(
                and(
                  eq(tasks.id, requestedParentTaskId),
                  isNull(tasks.deletedAt),
                ),
              )
              .for("update")
              .limit(1);
            if (!targetParent || String(targetParent.projectId) !== requestedProjectId) {
              throw new TaskProjectMoveInvariantError(
                409,
                "task_project_move_parent_project_mismatch",
                "移動先の親タスクは移動先プロジェクトに属している必要があります",
              );
            }
          }
          const incompleteChildren = directChildren.filter(
            (child) => normalizeTaskStatus(child.status) !== "closed",
          );

          for (const projectId of new Set(
            incompleteChildren.map((child) => child.projectId),
          )) {
            if (!(await canWriteProjectId(user, projectId))) {
              throw new IncompleteSubtaskPermissionDenied();
            }
          }

          if (
            incompleteChildren.length > 0 &&
            body.close_incomplete_subtasks !== true
          ) {
            throw new IncompleteSubtasksConfirmationRequired(
              incompleteChildren.map((child) => ({
                id: child.id,
                title: child.title,
                status: normalizeTaskStatus(child.status),
              })),
            );
          }

          const completionTime = toDbLocalTimestamp(new Date());
          const parentUpdateData = { ...updateData };
          if (
            normalizeTaskStatus(lockedTask.status) === "closed" &&
            lockedTask.completedAt
          ) {
            parentUpdateData.completedAt = lockedTask.completedAt;
          } else {
            parentUpdateData.completedAt = completionTime;
          }
          const [parent] = await tx
            .update(tasks)
            .set(parentUpdateData)
            .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
            .returning();
          if (parent) {
            await syncTaskTitleAndDocsInTransaction(tx, parent, lockedTask);
          }
           if (parent && projectWillChange) {
            // Schedule placement is project-scoped. Moving a task must not
            // leave a cross-project phase reference behind; datetime fields
            // and dependency rows remain untouched.
            await tx
              .delete(taskSchedulePlacements)
               .where(eq(taskSchedulePlacements.taskId, id));
           }
           if (parent) {
             const previousParentStatus = normalizeTaskStatus(lockedTask.status);
             const parentCompleted =
               previousParentStatus !== "closed" &&
               previousParentStatus !== "cancelled" &&
               normalizeTaskStatus(parent.status) === "closed";
             if (parentCompleted) {
               const activity = await recordTaskActivity(tx, {
                 taskId: parent.id,
                 userId: user.id,
                 activityType: "task_updated",
                 payload: {
                   status: "closed",
                   previous_status: previousParentStatus,
                 },
               });
               await enqueueKnowledgeCaptureForCompletion(tx, {
                 taskId: parent.id,
                 projectId: parent.projectId,
                 status: parent.status,
                 completedAt: parent.completedAt,
                 activity,
                 triggerUserId: user.id,
               });
             }
           }
           if (!parent || incompleteChildren.length === 0) return parent;

          const childIds = incompleteChildren.map((child) => child.id);
          const changedAt = new Date();
          await tx
            .update(tasks)
            .set({
              status: "closed",
              completedAt: completionTime,
              updatedAt: changedAt,
            })
            .where(inArray(tasks.id, childIds));
           for (const child of incompleteChildren) {
             const previousChildStatus = normalizeTaskStatus(child.status);
             const activity = await recordTaskActivity(tx, {
               taskId: child.id,
               userId: user.id,
               activityType: "closed_by_parent",
               payload: {
                 parent_task_id: id,
                 previous_status: previousChildStatus,
               },
             });
             if (
               previousChildStatus !== "closed" &&
               previousChildStatus !== "cancelled"
             ) {
               await enqueueKnowledgeCaptureForCompletion(tx, {
                 taskId: child.id,
                 projectId: child.projectId,
                 status: "closed",
                 completedAt: new Date(),
                 activity,
                 triggerUserId: user.id,
               });
             }
           }
           await tx.insert(taskActivities).values({
             taskId: id,
             userId: user.id,
             activityType: "subtasks_closed_with_parent",
             payload: {
               subtask_ids: childIds,
               subtask_count: childIds.length,
             },
           });
           return parent;
        },
        { isolationLevel: "serializable" },
      );

    try {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          updated = await closeTaskAndChildren();
          break;
        } catch (error) {
          if (isSerializationFailure(error) && attempt < 2) continue;
          throw error;
        }
      }
    } catch (error) {
      if (error instanceof IncompleteSubtasksConfirmationRequired) {
        return NextResponse.json(
          {
            detail: error.message,
            code: "incomplete_subtasks_confirmation_required",
            incomplete_subtasks: error.subtasks,
          },
          { status: 409 },
        );
      }
      if (error instanceof IncompleteSubtaskPermissionDenied) {
        return NextResponse.json(
          { detail: "タスクが見つかりません" },
          { status: 404 },
        );
      }
      if (error instanceof TaskProjectMoveInvariantError) {
        return NextResponse.json(
          { detail: error.message, code: error.code },
          { status: error.status },
        );
      }
      if (error instanceof TaskDocsNodeInvariantError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      if (error instanceof TaskProjectAccessInvariantError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      if (isSerializationFailure(error)) {
        return NextResponse.json(
          {
            detail: "タスクの状態が変更されました。もう一度お試しください",
            code: "task_completion_state_changed",
          },
          { status: 409 },
        );
      }
      throw error;
    }
  } else if (projectWillChange) {
    try {
      updated = await db.transaction(async (tx) => {
        await lockTaskProjectIds(tx, [
          priorTask.projectId,
          requestedProjectId ?? undefined,
        ]);
        priorTask = await lockTaskProjectMoveAndAssertNoDependencies(tx, {
          taskId: id,
          expectedProjectId: String(priorTask.projectId),
          targetProjectId: requestedProjectId ?? undefined,
          relatedTaskIds: requestedParentTaskId
            ? [requestedParentTaskId]
            : undefined,
        });
        if (preflightKnowledgeNodeId || priorTask.knowledgeNodeId) {
          throw new TaskProjectMoveInvariantError(
            409,
            "task_project_move_docs_binding_changed",
            "Docs連携済みタスクは先にDocs連携を解除してからProjectを変更してください",
          );
        }
        await assertTaskProjectAccessInTransaction(
          tx,
          [priorTask.projectId, requestedProjectId],
          user,
          {
            statusByProjectId: {
              [String(priorTask.projectId)]: 404,
              ...(requestedProjectId
                ? { [requestedProjectId]: 403 }
                : {}),
            },
          },
        );
        if (requestedDocsNodeId) {
          await assertTaskDocsNodeLinkAllowedInTransaction(
            tx,
            requestedDocsNodeId,
            String(requestedProjectId),
            user,
          );
        }
        if (requestedParentTaskId) {
          const [targetParent] = await tx
            .select({ projectId: tasks.projectId })
            .from(tasks)
            .where(
              and(
                eq(tasks.id, requestedParentTaskId),
                isNull(tasks.deletedAt),
              ),
            )
            .for("update")
            .limit(1);
          if (!targetParent || String(targetParent.projectId) !== requestedProjectId) {
            throw new TaskProjectMoveInvariantError(
              409,
              "task_project_move_parent_project_mismatch",
              "移動先の親タスクは移動先プロジェクトに属している必要があります",
            );
          }
        }
        const children = await tx
          .select({ id: tasks.id })
          .from(tasks)
          .where(and(eq(tasks.parentTaskId, id), isNull(tasks.deletedAt)))
          .for("update");
        if (children.length > 0) {
          throw new TaskProjectMoveInvariantError(
            409,
            "task_project_move_has_children",
            "子タスクがある親タスクは別のプロジェクトへ移動できません",
          );
        }
        const [movedTask] = await tx
          .update(tasks)
          .set(updateData)
          .where(eq(tasks.id, id))
          .returning();
         if (movedTask) {
           await syncTaskTitleAndDocsInTransaction(tx, movedTask, priorTask);
           await recordTaskLifecycleTransition(tx, priorTask, movedTask);
         }
        if (movedTask) {
          await tx
            .delete(taskSchedulePlacements)
            .where(eq(taskSchedulePlacements.taskId, id));
        }
        return movedTask;
      });
    } catch (error) {
      if (error instanceof TaskProjectMoveInvariantError) {
        return NextResponse.json(
          { detail: error.message, code: error.code },
          { status: error.status },
        );
      }
      if (error instanceof TaskDocsNodeInvariantError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      if (error instanceof TaskProjectAccessInvariantError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      throw error;
    }
  } else {
    if (body.parent_task_id === undefined) {
      try {
        updated = await db.transaction(async (tx) => {
          await lockTaskProjectIds(tx, [priorTask.projectId]);
          await lockTaskProjectRows(tx, [priorTask.projectId]);
          const [lockedTask] = await tx
            .select()
            .from(tasks)
            .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
            .for("update")
            .limit(1);
          if (!lockedTask) return undefined;
          await assertTaskProjectAccessInTransaction(
            tx,
            [lockedTask.projectId],
            user,
            { statusByProjectId: { [String(lockedTask.projectId)]: 404 } },
          );
          if (requestedDocsNodeId) {
            await assertTaskDocsNodeLinkAllowedInTransaction(
              tx,
              requestedDocsNodeId,
              String(lockedTask.projectId),
              user,
            );
          }
          // Reuse the locked snapshot for the post-update title/Docs sync
          // decisions below instead of the earlier preflight row.
          priorTask = lockedTask;
          const [nextTask] = await tx
            .update(tasks)
            .set(updateData)
            .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
            .returning();
           if (nextTask) {
             await syncTaskTitleAndDocsInTransaction(tx, nextTask, lockedTask);
             await recordTaskLifecycleTransition(tx, lockedTask, nextTask);
           }
          return nextTask;
        });
      } catch (error) {
        if (error instanceof TaskDocsNodeInvariantError) {
          return NextResponse.json(
            { detail: error.message },
            { status: error.status },
          );
        }
        if (error instanceof TaskProjectAccessInvariantError) {
          return NextResponse.json(
            { detail: error.message },
            { status: error.status },
          );
        }
        throw error;
      }
    } else {
      try {
        updated = await db.transaction(async (tx) => {
          const { task: lockedTask } = await lockTaskParentUpdate(tx, {
            taskId: id,
            expectedProjectId: String(priorTask.projectId),
            parentTaskId: requestedParentTaskId,
          });
          await assertTaskProjectAccessInTransaction(
            tx,
            [lockedTask.projectId],
            user,
            { statusByProjectId: { [String(lockedTask.projectId)]: 404 } },
          );
          if (requestedDocsNodeId) {
            await assertTaskDocsNodeLinkAllowedInTransaction(
              tx,
              requestedDocsNodeId,
              String(lockedTask.projectId),
              user,
            );
          }
          // The helper has revalidated the project while holding the advisory
          // lock.  Keep the guarded predicate as a final row-identity check.
          priorTask = lockedTask;
          const [nextTask] = await tx
            .update(tasks)
            .set(updateData)
            .where(
              and(
                eq(tasks.id, id),
                eq(tasks.projectId, lockedTask.projectId),
                isNull(tasks.deletedAt),
              ),
            )
            .returning();
           if (nextTask) {
             await syncTaskTitleAndDocsInTransaction(tx, nextTask, lockedTask);
             await recordTaskLifecycleTransition(tx, lockedTask, nextTask);
           }
          return nextTask;
        });
      } catch (error) {
        if (error instanceof TaskProjectMoveInvariantError) {
          return NextResponse.json(
            { detail: error.message, code: error.code },
            { status: error.status },
          );
        }
        if (error instanceof TaskDocsNodeInvariantError) {
          return NextResponse.json(
            { detail: error.message },
            { status: error.status },
          );
        }
        if (error instanceof TaskProjectAccessInvariantError) {
          return NextResponse.json(
            { detail: error.message },
            { status: error.status },
          );
        }
        throw error;
      }
    }
  }
  let responseTask = updated;

  if (!updated) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }

  // 繰り返しトリガー処理: 指定の trigger_status に遷移したら次回分を生成
  if (
    body.status !== undefined &&
    normalizeTaskStatus(priorTask.status) !== body.status
  ) {
    const rule = recurrenceRuleForStatusChange;

    if (
      rule &&
      normalizeTaskStatus(rule.triggerStatus) === body.status &&
      updated.startAt
    ) {
      const nextSchedule =
        recurringNextScheduleBeforeUpdate ??
        computeNextRecurringScheduleForRule(rule, updated.startAt, updated.endAt);

      if (nextSchedule) {
        const newStart = nextSchedule.startAt;
        const newEnd = nextSchedule.endAt;
        const resetStatus = normalizeTaskStatus(rule.resetStatusTo || "open");
        const newEndCount =
          rule.endCount !== null && rule.endCount !== undefined
            ? Math.max(0, rule.endCount - nextSchedule.advancedBy)
            : null;

        if (rule.createNew) {
          try {
            const [newTask] = await db.transaction(async (tx) => {
              await lockTaskProjectIds(tx, [updated.projectId]);
              await lockTaskProjectRows(tx, [updated.projectId]);
              // The source Task, recurrence rule, and copied relations were
              // previously read before this transaction. Lock and re-read
              // them now so a concurrent recurrence edit cannot be silently
              // overwritten by this trigger.
              const [lockedSourceTask] = await tx
                .select()
                .from(tasks)
                .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
                .for("update")
                .limit(1);
              if (!lockedSourceTask) {
                throw new TaskProjectMoveInvariantError(
                  409,
                  "recurrence_source_changed",
                  "繰り返し元タスクが同時変更されたため次回タスクを作成できません",
                );
              }
              // This insert happens after the main task update has committed,
              // so repeat the Project/member ACL check inside the same
              // transaction that owns the new Task row.  The shared advisory
              // lock above ensures Project ACL rows are acquired before the
              // source Task, matching Python and task-move writers.
              await assertTaskProjectAccessInTransaction(
                tx,
                [lockedSourceTask.projectId],
                user,
              );
              const [lockedRule] = await tx
                .select()
                .from(taskRecurrenceRules)
                .where(eq(taskRecurrenceRules.taskId, id))
                .for("update")
                .limit(1);
              if (
                !lockedRule
                || lockedRule.id !== rule.id
                || lockedRule.rrule !== rule.rrule
                || lockedRule.timezone !== rule.timezone
                || lockedRule.triggerStatus !== rule.triggerStatus
                || lockedRule.createNew !== rule.createNew
                || lockedRule.resetStatusTo !== rule.resetStatusTo
                || lockedRule.endCount !== rule.endCount
                || dateSignature(lockedRule.endDate) !== dateSignature(rule.endDate)
                || lockedRule.skipWeekend !== rule.skipWeekend
                || lockedRule.skipHoliday !== rule.skipHoliday
                || lockedRule.skipMode !== rule.skipMode
              ) {
                throw new TaskProjectMoveInvariantError(
                  409,
                  "recurrence_rule_changed",
                  "繰り返し設定が同時変更されたため次回タスクを作成できません",
                );
              }
              if (
                String(lockedSourceTask.projectId) !== String(updated.projectId)
                || lockedSourceTask.title !== updated.title
                || lockedSourceTask.description !== updated.description
                || lockedSourceTask.status !== updated.status
                || lockedSourceTask.priority !== updated.priority
                || dateSignature(lockedSourceTask.startAt) !== dateSignature(updated.startAt)
                || dateSignature(lockedSourceTask.endAt) !== dateSignature(updated.endAt)
                || lockedSourceTask.allDay !== updated.allDay
                || jsonSignature(lockedSourceTask.reminderOffsets) !== jsonSignature(updated.reminderOffsets)
                || lockedSourceTask.notificationsEnabled !== updated.notificationsEnabled
                || lockedSourceTask.sortOrder !== updated.sortOrder
                || lockedSourceTask.parentTaskId !== updated.parentTaskId
              ) {
                throw new TaskProjectMoveInvariantError(
                  409,
                  "recurrence_source_changed",
                  "繰り返し元タスクが同時変更されたため次回タスクを作成できません",
                );
              }
              const origTags = await tx
                .select()
                .from(taskTags)
                .where(eq(taskTags.taskId, id));
              const { tagIds: recurringTagIds } = await resolveProjectTagIds(
                String(lockedSourceTask.projectId),
                origTags.map((tag) => tag.tagId),
              );
              const origAssignees = await tx
                .select()
                .from(taskAssignees)
                .where(eq(taskAssignees.taskId, id));
              const recurringTaskInsertValues = {
                projectId: lockedSourceTask.projectId,
                title: lockedSourceTask.title,
                description: lockedSourceTask.description,
                status: resetStatus,
                priority: lockedSourceTask.priority,
                startAt: toDbLocalTimestamp(newStart),
                endAt: newEnd ? toDbLocalTimestamp(newEnd) : null,
                allDay: lockedSourceTask.allDay,
                reminderOffsets: lockedSourceTask.reminderOffsets,
                notificationsEnabled: lockedSourceTask.notificationsEnabled,
                autoCloseOnDue: lockedSourceTask.autoCloseOnDue,
                source: lockedSourceTask.source,
                createdBy: lockedSourceTask.createdBy,
                completedAt: null,
                taskMetadata: stripGoogleCalendarMetadata(lockedSourceTask.taskMetadata),
                estimatedHours: lockedSourceTask.estimatedHours,
                sortOrder: lockedSourceTask.sortOrder,
                parentTaskId: lockedSourceTask.parentTaskId,
              };
              const [inserted] = await tx
                .insert(tasks)
                .values(recurringTaskInsertValues)
                .returning();
              if (!inserted) throw new Error("繰り返しタスクを作成できません");

              if (recurringTagIds.length > 0) {
                await tx.insert(taskTags).values(
                  recurringTagIds.map((tagId) => ({
                    taskId: inserted.id,
                    tagId,
                  })),
                );
              }

              if (origAssignees.length > 0) {
                await tx.insert(taskAssignees).values(
                  origAssignees.map((a) => ({
                    taskId: inserted.id,
                    userId: a.userId,
                    isPrimary: a.isPrimary,
                  })),
                );
              }

              // task_recurrence_rules.task_id は UNIQUE のため旧側を削除してから移管
              await tx
                .delete(taskRecurrenceRules)
                .where(eq(taskRecurrenceRules.taskId, id));
              await tx.insert(taskRecurrenceRules).values({
                taskId: inserted.id,
                rrule: lockedRule.rrule,
                timezone: lockedRule.timezone,
                horizonDays: lockedRule.horizonDays,
                triggerStatus: lockedRule.triggerStatus,
                createNew: lockedRule.createNew,
                recurForever: lockedRule.recurForever,
                resetStatusTo: lockedRule.resetStatusTo,
                endCount: newEndCount,
                endDate: lockedRule.endDate,
                skipWeekend: lockedRule.skipWeekend,
                skipHoliday: lockedRule.skipHoliday,
                skipMode: lockedRule.skipMode,
              });
              return [inserted] as const;
            });
            recurringCreatedTaskId = newTask.id;
          } catch (error) {
            if (error instanceof TaskProjectAccessInvariantError) {
              return NextResponse.json(
                { detail: error.message },
                { status: error.status },
              );
            }
            if (error instanceof TaskProjectMoveInvariantError) {
              return NextResponse.json(
                { detail: error.message, code: error.code },
                { status: error.status },
              );
            }
            throw error;
          }
        } else {
          try {
            const nextTask = await db.transaction(async (tx) => {
              await lockTaskProjectIds(tx, [priorTask.projectId]);
              await lockTaskProjectRows(tx, [priorTask.projectId]);
              // The reset path also writes a Task after the main transition
              // transaction. Recheck the current Project/member ACL before
              // holding the Task row so a revoke cannot race this mutation.
              const [lockedTask] = await tx
                .select()
                .from(tasks)
                .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
                .for("update")
                .limit(1);
              if (!lockedTask) return undefined;
              await assertTaskProjectAccessInTransaction(
                tx,
                [lockedTask.projectId],
                user,
                { statusByProjectId: { [String(lockedTask.projectId)]: 404 } },
              );
              const [resetTask] = await tx
                .update(tasks)
                .set({
                  status: resetStatus,
                  startAt: toDbLocalTimestamp(newStart),
                  endAt: newEnd ? toDbLocalTimestamp(newEnd) : null,
                  completedAt: null,
                  updatedAt: new Date(),
                })
                .where(
                  and(
                    eq(tasks.id, id),
                    eq(tasks.projectId, lockedTask.projectId),
                    isNull(tasks.deletedAt),
                  ),
                )
                .returning();

              if (newEndCount !== null) {
                await tx
                  .update(taskRecurrenceRules)
                  .set({ endCount: newEndCount, updatedAt: new Date() })
                  .where(eq(taskRecurrenceRules.taskId, id));
              }
              return resetTask;
            });
            if (nextTask) {
              responseTask = nextTask;
            }
          } catch (error) {
            if (error instanceof TaskProjectAccessInvariantError) {
              return NextResponse.json(
                { detail: error.message },
                { status: error.status },
              );
            }
            throw error;
          }
        }
      }
    }
  }

  try {
    await db.transaction(async (tx) => {
      // Relation and activity writes happen after the main Task/recurrence
      // transition, so they need their own lock boundary.  Recheck the
      // current Project ACL before locking the Task and keep every dependent
      // write in this transaction; otherwise a membership revoke between the
      // two phases could still mutate tags/assignees or emit activity rows.
      await lockTaskProjectIds(tx, [priorTask.projectId, updated.projectId]);
      await lockTaskProjectRows(tx, [priorTask.projectId, updated.projectId]);
      const [lockedTask] = await tx
        .select()
        .from(tasks)
        .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
        .for("update")
        .limit(1);
      if (!lockedTask) {
        throw new TaskProjectAccessInvariantError(404, "タスクが見つかりません");
      }
      await assertTaskProjectAccessInTransaction(
        tx,
        [lockedTask.projectId],
        user,
        { statusByProjectId: { [String(lockedTask.projectId)]: 404 } },
      );

      if (body.project_id !== undefined) {
        if (suppressProjectChange) {
          await tx.insert(taskActivities).values({
            taskId: id,
            userId: user.id,
            activityType: "project_change_suppressed",
            payload: {
              from_project_id: String(priorTask.projectId),
              requested_project_id: requestedProjectId,
              reason: "recurrence_trigger_status_update",
              status: normalizedBodyStatus,
            },
          });
        } else if (String(priorTask.projectId) !== String(updated.projectId)) {
          await tx.insert(taskActivities).values({
            taskId: id,
            userId: user.id,
            activityType: "project_changed",
            payload: {
              from_project_id: String(priorTask.projectId),
              to_project_id: String(updated.projectId),
            },
          });
        }
      }

      // tag_ids が指定されていたら差し替え。 The task/project lock above
      // remains held for both the delete and insert so ACL revocation cannot
      // split the relation mutation.
      if (body.tag_ids !== undefined) {
        await tx.delete(taskTags).where(eq(taskTags.taskId, id));
        if (tagResolution && tagResolution.tagIds.length > 0) {
          await tx.insert(taskTags).values(
            tagResolution.tagIds.map((tagId: string) => ({
              taskId: id,
              tagId,
            })),
          );
        }
      } else if (String(lockedTask.projectId) !== String(priorTask.projectId)) {
        const existingTags = await tx
          .select()
          .from(taskTags)
          .where(eq(taskTags.taskId, id));
        const { tagIds: retainedTagIds } = await resolveProjectTagIds(
          String(lockedTask.projectId),
          existingTags.map((tag) => tag.tagId),
        );
        if (retainedTagIds.length !== existingTags.length) {
          await tx.delete(taskTags).where(eq(taskTags.taskId, id));
          if (retainedTagIds.length > 0) {
            await tx.insert(taskTags).values(
              retainedTagIds.map((tagId) => ({
                taskId: id,
                tagId,
              })),
            );
          }
        }
      }

      if (body.assignee_ids !== undefined) {
        await tx.delete(taskAssignees).where(eq(taskAssignees.taskId, id));
        if (body.assignee_ids.length > 0) {
          await tx.insert(taskAssignees).values(
            body.assignee_ids.map((userId: string, index: number) => ({
              taskId: id,
              userId,
              isPrimary: index === 0,
            })),
          );
        }
      }
    });
  } catch (error) {
    if (error instanceof TaskProjectAccessInvariantError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    throw error;
  }

  const [latestTask] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  const finalTask = latestTask ?? responseTask ?? updated;

  enqueueGoogleCalendarPatchSync(priorTask, finalTask, user);
  if (recurringCreatedTaskId) {
    enqueueAutoSyncGoogleCalendarForTask(recurringCreatedTaskId, user);
  }

  // GETと同じ形式でレスポンスを返す
  const result = serializeAutoCloseOnDue(
    finalTask,
    taskToSnake(finalTask as unknown as Record<string, unknown>),
  );

  const tagRows = await db
    .select({
      id: tags.id,
      spaceId: tags.spaceId,
      name: tags.name,
      color: tags.color,
      createdBy: tags.createdBy,
      createdAt: tags.createdAt,
    })
    .from(taskTags)
    .innerJoin(tags, eq(taskTags.tagId, tags.id))
    .where(eq(taskTags.taskId, id));
  result.tags = tagRows.map((t) => ({
    id: t.id,
    space_id: t.spaceId,
    name: t.name,
    color: t.color,
    created_by: t.createdBy,
    created_at: t.createdAt,
  }));

  const assigneeRows = await db
    .select({
      id: taskAssignees.id,
      taskId: taskAssignees.taskId,
      userId: taskAssignees.userId,
      isPrimary: taskAssignees.isPrimary,
      assignedAt: taskAssignees.assignedAt,
      displayName: users.displayName,
      username: users.username,
    })
    .from(taskAssignees)
    .leftJoin(users, eq(taskAssignees.userId, users.id))
    .where(eq(taskAssignees.taskId, id));
  result.assignees = assigneeRows.map((a) => ({
    id: a.id,
    task_id: a.taskId,
    user_id: a.userId,
    is_primary: a.isPrimary,
    assigned_at: a.assignedAt,
    display_name: a.displayName,
    username: a.username,
  }));

  const [activeEntry] = await db
    .select()
    .from(timeEntries)
    .where(
      and(
        eq(timeEntries.taskId, id),
        eq(timeEntries.userId, user.id),
        isNull(timeEntries.endedAt),
      ),
    )
    .orderBy(desc(timeEntries.startedAt));
  result.active_time_entry =
    activeEntry && !activeEntry.endedAt ? mapTimeEntry(activeEntry) : null;

  const [recRow] = await db
    .select({ taskId: taskRecurrenceRules.taskId })
    .from(taskRecurrenceRules)
    .where(eq(taskRecurrenceRules.taskId, id))
    .limit(1);
  result.has_recurrence = !!recRow;

  return NextResponse.json(result);
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

  const [targetTask] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!targetTask) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canWriteProjectId(user, targetTask.projectId))) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  // Task mutation authority lives in the FastAPI service.  In particular,
  // DELETE must not fall back to the legacy Drizzle hard-delete helper: doing
  // so would bypass the canonical tombstone/audit lifecycle and could leave
  // the two stores with divergent trees.  A transport failure is surfaced as
  // a bounded 502 rather than silently duplicating the mutation locally.
  let response: Response;
  try {
    response = await fetchPythonApi(`/api/tasks/${encodeURIComponent(id)}`, {
      method: "DELETE",
      user,
    });
  } catch (error) {
    console.error("正規タスク削除APIへの接続に失敗しました:", error);
    return NextResponse.json(
      { detail: "正規タスク削除サービスに接続できません" },
      { status: 502 },
    );
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    return new NextResponse(
      body || JSON.stringify({ detail: "タスクの削除に失敗しました" }),
      {
        status: response.status,
        headers: {
          "content-type":
            response.headers.get("content-type") ?? "application/json",
        },
      },
    );
  }

  // FastAPI's canonical endpoint is 204. Preserve a non-empty body from a
  // compatible deployment, but keep the historical empty success response for
  // the normal case so taskApi.request<void>() remains valid.
  if (response.status === 204) return new NextResponse(null, { status: 204 });
  const body = await response.text().catch(() => "");
  return new NextResponse(body || null, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/json",
    },
  });
}
