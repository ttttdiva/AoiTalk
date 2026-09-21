import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { timeEntries, tasks, users, projects } from "@/db/schema";
import { eq, and, gte, lte, desc, inArray, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  resolveReadScope,
  TaskBrowseScopeError,
} from "@/lib/server/task-route-utils";
import {
  calculateTimerDurationSeconds,
  timerDateKey,
  toDbTimerTimestamp,
  type DbTimestampValue,
} from "@/lib/server/db-time";

function calcDurationSeconds(
  startedAt: DbTimestampValue,
  endedAt: DbTimestampValue,
  now: Date,
): number {
  return calculateTimerDurationSeconds(startedAt, endedAt, now);
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const dateFrom = searchParams.get("date_from");
  const dateTo = searchParams.get("date_to");

  const emptyResponse = {
    summary: { total_seconds: 0, entry_count: 0, active_entries: 0 },
    by_task: [],
    by_day: [],
    by_user: [],
    by_project: [],
  };
  let readableProjectIds: string[];
  try {
    readableProjectIds = (await resolveReadScope(user, searchParams)).projectIds;
  } catch (error) {
    if (error instanceof TaskBrowseScopeError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    throw error;
  }
  if (readableProjectIds.length === 0) {
    return NextResponse.json(emptyResponse);
  }

  const conditions = [
    inArray(tasks.projectId, readableProjectIds),
    isNull(tasks.deletedAt),
    isNull(projects.deletedAt),
    isNull(timeEntries.deletedAt),
  ];

  if (dateFrom) {
    // time_entries uses the deployment-zone naive wall clock, unlike generic
    // task/date fields handled by toDbLocalTimestamp.
    conditions.push(gte(timeEntries.startedAt, toDbTimerTimestamp(dateFrom)));
  }
  if (dateTo) {
    conditions.push(lte(timeEntries.startedAt, toDbTimerTimestamp(dateTo)));
  }

  const rows = await db
    .select({
      id: timeEntries.id,
      taskId: timeEntries.taskId,
      userId: timeEntries.userId,
      startedAt: timeEntries.startedAt,
      endedAt: timeEntries.endedAt,
      source: timeEntries.source,
      createdAt: timeEntries.createdAt,
      taskTitle: tasks.title,
      projectId: tasks.projectId,
      projectName: projects.name,
    })
    .from(timeEntries)
    .innerJoin(tasks, eq(timeEntries.taskId, tasks.id))
    .innerJoin(projects, eq(tasks.projectId, projects.id))
    .where(and(...conditions))
    .orderBy(desc(timeEntries.startedAt));

  const now = new Date();
  let totalSeconds = 0;
  let activeEntries = 0;
  for (const r of rows) {
    const dur = calcDurationSeconds(r.startedAt, r.endedAt, now);
    if (dur) totalSeconds += dur;
    if (!r.endedAt) activeEntries++;
  }

  // by_task
  const taskMap = new Map<
    string,
    {
      label: string;
      seconds: number;
      entries: number;
      project_id: string | null;
      project_name: string | null;
    }
  >();
  for (const r of rows) {
    const key = r.taskId;
    const existing = taskMap.get(key) || {
      label: r.taskTitle || "不明",
      seconds: 0,
      entries: 0,
      project_id: r.projectId,
      project_name: r.projectName,
    };
    existing.seconds += calcDurationSeconds(r.startedAt, r.endedAt, now);
    existing.entries += 1;
    taskMap.set(key, existing);
  }

  const byTask = Array.from(taskMap.entries()).map(([key, v]) => ({
    key,
    label: v.label,
    seconds: v.seconds,
    entries: v.entries,
    project_id: v.project_id,
    project_name: v.project_name,
  }));

  // by_project
  const projectMap = new Map<
    string,
    { label: string; seconds: number; entries: number }
  >();
  for (const r of rows) {
    const key = r.projectId;
    const existing = projectMap.get(key) || {
      label: r.projectName || "不明",
      seconds: 0,
      entries: 0,
    };
    existing.seconds += calcDurationSeconds(r.startedAt, r.endedAt, now);
    existing.entries += 1;
    projectMap.set(key, existing);
  }

  const byProject = Array.from(projectMap.entries()).map(([key, v]) => ({
    key,
    label: v.label,
    seconds: v.seconds,
    entries: v.entries,
    project_id: key,
    project_name: v.label,
  }));

  // by_day
  const dayMap = new Map<string, { seconds: number; entries: number }>();
  for (const r of rows) {
    if (!r.startedAt) continue;
    const day = timerDateKey(r.startedAt);
    if (!day) continue;
    const existing = dayMap.get(day) || { seconds: 0, entries: 0 };
    existing.seconds += calcDurationSeconds(r.startedAt, r.endedAt, now);
    existing.entries += 1;
    dayMap.set(day, existing);
  }

  const byDay = Array.from(dayMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, v]) => ({
      key,
      label: key,
      seconds: v.seconds,
      entries: v.entries,
    }));

  // by_user
  const userIds = [...new Set(rows.map((r) => r.userId).filter(Boolean))];
  const userMap = new Map<
    string,
    { label: string; seconds: number; entries: number }
  >();

  const userNames = new Map<string, string>();
  if (userIds.length > 0) {
    const userRows = await db
      .select({
        id: users.id,
        displayName: users.displayName,
        username: users.username,
      })
      .from(users);
    for (const u of userRows) {
      userNames.set(u.id, u.displayName || u.username || u.id);
    }
  }

  for (const r of rows) {
    const key = r.userId || "unknown";
    const existing = userMap.get(key) || {
      label: userNames.get(key) || key,
      seconds: 0,
      entries: 0,
    };
    existing.seconds += calcDurationSeconds(r.startedAt, r.endedAt, now);
    existing.entries += 1;
    userMap.set(key, existing);
  }

  const byUser = Array.from(userMap.entries()).map(([key, v]) => ({
    key,
    label: v.label,
    seconds: v.seconds,
    entries: v.entries,
  }));

  return NextResponse.json({
    summary: {
      total_seconds: totalSeconds,
      entry_count: rows.length,
      active_entries: activeEntries,
    },
    by_task: byTask,
    by_day: byDay,
    by_user: byUser,
    by_project: byProject,
  });
}
