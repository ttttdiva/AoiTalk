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
  knowledgeNodes,
} from "@/db/schema";
import { eq, desc, inArray, sql, isNull, and, max } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded } from "@/lib/server/field-crypto";
import { computeNextRecurringScheduleAfter } from "@/lib/recurrence-schedule";
import { normalizeTaskStatus } from "@/lib/task-status";
import { parseRrule } from "@/lib/recurrence-rrule";
import {
  deleteAutoGoogleCalendarForTask,
  enqueueAutoSyncGoogleCalendarForTask,
} from "@/lib/server/google-calendar-auto-sync";
import {
  correctLikelyTimerStartedAt,
  dbTimestampToLocalDate,
  serializeDbTimestamp,
  toDbLocalTimestamp,
} from "@/lib/server/db-time";
import {
  canWriteMembership,
  getProjectMembership,
  isDateOnlyTaskInput,
  normalizeOptionalUuid,
  parseTaskWallClockDate,
  normalizeTaskTitle,
  resolveProjectTagIds,
  stripGoogleCalendarMetadata,
  taskToSnake,
  type SessionUser,
} from "@/lib/server/task-route-utils";
import {
  collectTaskTreeIds,
  deleteTaskTreeRows,
} from "@/lib/server/task-delete";
import {
  appendKnowledgeRevision,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";

async function hasWritableProjectAccess(
  user: SessionUser,
  projectId: string,
): Promise<boolean> {
  const membership = await getProjectMembership(user.id, projectId);
  return canWriteMembership(user, membership);
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
  const startedAt =
    !row.endedAt && row.startedAt
      ? correctLikelyTimerStartedAt(row.startedAt, row.createdAt, row.source)
      : row.startedAt;
  return {
    id: row.id,
    task_id: row.taskId,
    user_id: row.userId,
    started_at: serializeDbTimestamp(startedAt),
    ended_at: serializeDbTimestamp(row.endedAt),
    duration_seconds:
      row.startedAt && row.endedAt
        ? Math.max(
            0,
            Math.floor(
              ((dbTimestampToLocalDate(row.endedAt)?.getTime() ?? 0) -
                (dbTimestampToLocalDate(startedAt)?.getTime() ?? 0)) /
                1000,
            ),
          )
        : null,
    source: row.source,
    note: row.note,
    created_at: serializeDbTimestamp(row.createdAt),
  };
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

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
  const membership = await getProjectMembership(user.id, task.projectId);
  if (!membership) {
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

  const result = taskToSnake(task as unknown as Record<string, unknown>);
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
    const sub = taskToSnake(s as unknown as Record<string, unknown>);
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

  const [priorTask] = await db
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
  const sourceMembership = await getProjectMembership(
    user.id,
    priorTask.projectId,
  );
  if (!sourceMembership) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (!canWriteMembership(user, sourceMembership)) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
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
    updateData.knowledgeNodeId = normalizeOptionalUuid(body.knowledge_node_id);
  }
  if (
    body.metadata !== undefined &&
    body.metadata &&
    typeof body.metadata === "object" &&
    !Array.isArray(body.metadata)
  ) {
    updateData.taskMetadata = body.metadata;
  }
  const [updated] = await db
    .update(tasks)
    .set(updateData)
    .where(eq(tasks.id, id))
    .returning();
  let responseTask = updated;

  if (!updated) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }

  if (
    body.title !== undefined &&
    updated.knowledgeNodeId &&
    updated.title !== priorTask.title
  ) {
    const [linkedNode] = await db
      .update(knowledgeNodes)
      .set({
        title: updated.title,
        updatedBy: user.id,
        updatedAt: new Date(),
      })
      .where(
        and(
          eq(knowledgeNodes.id, updated.knowledgeNodeId),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .returning();
    if (linkedNode) {
      await upsertKnowledgeSearchIndex(
        db,
        linkedNode,
        decryptTextIfNeeded(linkedNode.bodyText ?? "", "knowledge_nodes.body_text") ??
          "",
      );
      await appendKnowledgeRevision(
        db,
        linkedNode,
        user.id,
        "タスクタイトルをDocs nodeへ同期",
      );
    }
  }

  if (body.project_id !== undefined) {
    if (suppressProjectChange) {
      await db.insert(taskActivities).values({
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
      await db.insert(taskActivities).values({
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
      const endCountExhausted =
        !rule.recurForever &&
        rule.endCount !== null &&
        rule.endCount !== undefined &&
        rule.endCount <= 1;

      if (!endCountExhausted) {
        const parsed = parseRrule(rule.rrule);
        const currentStartAt = dbTimestampToLocalDate(updated.startAt);
        const nextSchedule = currentStartAt
          ? computeNextRecurringScheduleAfter({
              currentStartAt,
              currentEndAt: dbTimestampToLocalDate(updated.endAt),
              config: {
                freq: parsed.freq,
                interval: parsed.interval,
                byDay: parsed.byDay,
                skipWeekend: rule.skipWeekend,
                skipHoliday: rule.skipHoliday,
                endCount: rule.recurForever ? null : rule.endCount,
                endDate: rule.recurForever
                  ? null
                  : rule.endDate
                    ? serializeDbTimestamp(rule.endDate)
                    : null,
              },
              after: currentStartAt,
            })
          : null;

        if (nextSchedule) {
          const newStart = nextSchedule.startAt;
          const newEnd = nextSchedule.endAt;
          const resetStatus = normalizeTaskStatus(rule.resetStatusTo || "open");
          const newEndCount =
            rule.endCount !== null && rule.endCount !== undefined
              ? Math.max(0, rule.endCount - nextSchedule.advancedBy)
              : null;

          if (rule.createNew) {
            const [newTask] = await db
              .insert(tasks)
              .values({
                projectId: updated.projectId,
                title: updated.title,
                description: updated.description,
                status: resetStatus,
                priority: updated.priority,
                startAt: toDbLocalTimestamp(newStart),
                endAt: newEnd ? toDbLocalTimestamp(newEnd) : null,
                allDay: updated.allDay,
                reminderOffsets: updated.reminderOffsets,
                notificationsEnabled: updated.notificationsEnabled,
                source: updated.source,
                createdBy: updated.createdBy,
                completedAt: null,
                taskMetadata: stripGoogleCalendarMetadata(updated.taskMetadata),
                estimatedHours: updated.estimatedHours,
                sortOrder: updated.sortOrder,
                parentTaskId: updated.parentTaskId,
              })
              .returning();
            recurringCreatedTaskId = newTask.id;

            const origTags = await db
              .select()
              .from(taskTags)
              .where(eq(taskTags.taskId, id));
            const { tagIds: recurringTagIds } = await resolveProjectTagIds(
              String(newTask.projectId),
              origTags.map((tag) => tag.tagId),
            );
            if (recurringTagIds.length > 0) {
              await db.insert(taskTags).values(
                recurringTagIds.map((tagId) => ({
                  taskId: newTask.id,
                  tagId,
                })),
              );
            }

            const origAssignees = await db
              .select()
              .from(taskAssignees)
              .where(eq(taskAssignees.taskId, id));
            if (origAssignees.length > 0) {
              await db.insert(taskAssignees).values(
                origAssignees.map((a) => ({
                  taskId: newTask.id,
                  userId: a.userId,
                  isPrimary: a.isPrimary,
                })),
              );
            }

            // task_recurrence_rules.task_id は UNIQUE のため旧側を削除してから移管
            await db
              .delete(taskRecurrenceRules)
              .where(eq(taskRecurrenceRules.taskId, id));
            await db.insert(taskRecurrenceRules).values({
              taskId: newTask.id,
              rrule: rule.rrule,
              timezone: rule.timezone,
              horizonDays: rule.horizonDays,
              triggerStatus: rule.triggerStatus,
              createNew: rule.createNew,
              recurForever: rule.recurForever,
              resetStatusTo: rule.resetStatusTo,
              endCount: newEndCount,
              endDate: rule.endDate,
              skipWeekend: rule.skipWeekend,
              skipHoliday: rule.skipHoliday,
            });
          } else {
            const [nextTask] = await db
              .update(tasks)
              .set({
                status: resetStatus,
                startAt: toDbLocalTimestamp(newStart),
                endAt: newEnd ? toDbLocalTimestamp(newEnd) : null,
                completedAt: null,
                updatedAt: new Date(),
              })
              .where(eq(tasks.id, id))
              .returning();
            if (nextTask) {
              responseTask = nextTask;
            }

            if (newEndCount !== null) {
              await db
                .update(taskRecurrenceRules)
                .set({ endCount: newEndCount, updatedAt: new Date() })
                .where(eq(taskRecurrenceRules.taskId, id));
            }
          }
        }
      }
    }
  }

  // tag_ids が指定されていたら差し替え
  if (body.tag_ids !== undefined) {
    await db.delete(taskTags).where(eq(taskTags.taskId, id));
    if (tagResolution && tagResolution.tagIds.length > 0) {
      await db.insert(taskTags).values(
        tagResolution.tagIds.map((tagId: string) => ({
          taskId: id,
          tagId,
        })),
      );
    }
  } else if (String(updated.projectId) !== String(priorTask.projectId)) {
    const existingTags = await db
      .select()
      .from(taskTags)
      .where(eq(taskTags.taskId, id));
    const { tagIds: retainedTagIds } = await resolveProjectTagIds(
      String(updated.projectId),
      existingTags.map((tag) => tag.tagId),
    );
    if (retainedTagIds.length !== existingTags.length) {
      await db.delete(taskTags).where(eq(taskTags.taskId, id));
      if (retainedTagIds.length > 0) {
        await db.insert(taskTags).values(
          retainedTagIds.map((tagId) => ({
            taskId: id,
            tagId,
          })),
        );
      }
    }
  }

  if (body.assignee_ids !== undefined) {
    await db.delete(taskAssignees).where(eq(taskAssignees.taskId, id));
    if (body.assignee_ids.length > 0) {
      await db.insert(taskAssignees).values(
        body.assignee_ids.map((userId: string, index: number) => ({
          taskId: id,
          userId,
          isPrimary: index === 0,
        })),
      );
    }
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
  const result = taskToSnake(finalTask as unknown as Record<string, unknown>);

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
  const membership = await getProjectMembership(user.id, targetTask.projectId);
  if (!membership) {
    return NextResponse.json(
      { detail: "タスクが見つかりません" },
      { status: 404 },
    );
  }
  if (!canWriteMembership(user, membership)) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  try {
    const taskIds = await collectTaskTreeIds(id);
    for (const taskId of taskIds) {
      await deleteAutoGoogleCalendarForTask(taskId, user);
    }

    const deletedRows = await deleteTaskTreeRows(taskIds);
    const deleted = deletedRows.some((row) => row.id === id);

    if (!deleted) {
      return NextResponse.json(
        { detail: "タスクが見つかりません" },
        { status: 404 },
      );
    }

    return NextResponse.json({ success: true });
  } catch (err) {
    console.error("タスク削除エラー:", err);
    return NextResponse.json(
      { detail: "タスクの削除に失敗しました" },
      { status: 500 },
    );
  }
}
