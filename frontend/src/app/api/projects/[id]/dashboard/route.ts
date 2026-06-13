import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  tasks,
  tags,
  taskTags,
  timeEntries,
  projects,
  users,
} from "@/db/schema";
import { eq, and, sql, isNotNull, desc, isNull, inArray } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { normalizeTaskStatus } from "@/lib/task-status";
import { getAccessibleProject } from "@/lib/server/project-access";
import { serializeDbTimestamp } from "@/lib/server/db-time";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const { id: projectId } = await params;
  const access = await getAccessibleProject(projectId, user.id);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const [projSpaceRow] = await db
    .select({ spaceId: projects.spaceId })
    .from(projects)
    .where(eq(projects.id, projectId))
    .limit(1);
  const projSpaceId = projSpaceRow?.spaceId ?? null;

  // ステータス別タスク数
  const statusCounts = await db
    .select({
      status: tasks.status,
      count: sql<number>`count(*)::int`,
    })
    .from(tasks)
    .where(and(eq(tasks.projectId, projectId), isNull(tasks.parentTaskId)))
    .groupBy(tasks.status);
  const mergedStatusCounts = new Map<string, number>();
  for (const row of statusCounts) {
    const normalizedStatus = normalizeTaskStatus(row.status);
    mergedStatusCounts.set(
      normalizedStatus,
      (mergedStatusCounts.get(normalizedStatus) ?? 0) + row.count,
    );
  }

  // 優先度別タスク数
  const priorityCounts = await db
    .select({
      priority: tasks.priority,
      count: sql<number>`count(*)::int`,
    })
    .from(tasks)
    .where(and(eq(tasks.projectId, projectId), isNull(tasks.parentTaskId)))
    .groupBy(tasks.priority);

  // タグ別作業時間集計
  const tagTimeRows = await db
    .select({
      tagId: tags.id,
      tagName: tags.name,
      tagColor: tags.color,
      totalSeconds: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
    })
    .from(tags)
    .leftJoin(taskTags, eq(taskTags.tagId, tags.id))
    .leftJoin(timeEntries, eq(timeEntries.taskId, taskTags.taskId))
    .where(
      and(
        projSpaceId ? eq(tags.spaceId, projSpaceId) : sql`false`,
        isNotNull(timeEntries.endedAt),
      ),
    )
    .groupBy(tags.id, tags.name, tags.color);

  // タグの全一覧も取得(時間がないタグも表示するため)
  const allTags = projSpaceId
    ? await db
        .select({ id: tags.id, name: tags.name, color: tags.color })
        .from(tags)
        .where(eq(tags.spaceId, projSpaceId))
    : [];

  // タグ別タスク数
  const tagTaskCounts = await db
    .select({
      tagId: taskTags.tagId,
      count: sql<number>`count(distinct ${taskTags.taskId})::int`,
    })
    .from(taskTags)
    .innerJoin(tasks, eq(tasks.id, taskTags.taskId))
    .where(eq(tasks.projectId, projectId))
    .groupBy(taskTags.tagId);

  const tagTaskCountMap = new Map(tagTaskCounts.map((r) => [r.tagId, r.count]));
  const tagTimeMap = new Map(tagTimeRows.map((r) => [r.tagId, r.totalSeconds]));

  const tagStats = allTags.map((t) => ({
    id: t.id,
    name: t.name,
    color: t.color,
    total_seconds: tagTimeMap.get(t.id) || 0,
    task_count: tagTaskCountMap.get(t.id) || 0,
  }));

  // 最近完了したタスク (直近5件)
  const recentCompleted = await db
    .select({
      id: tasks.id,
      title: tasks.title,
      completedAt: tasks.completedAt,
      priority: tasks.priority,
    })
    .from(tasks)
    .where(
      and(
        eq(tasks.projectId, projectId),
        isNotNull(tasks.completedAt),
        isNull(tasks.parentTaskId),
      ),
    )
    .orderBy(desc(tasks.completedAt))
    .limit(5);

  // アクティブタイマー数
  const projectTaskIds = await db
    .select({ id: tasks.id })
    .from(tasks)
    .where(eq(tasks.projectId, projectId));

  let activeTimerCount = 0;
  if (projectTaskIds.length > 0) {
    const ids = projectTaskIds.map((t) => t.id);
    const [result] = await db
      .select({ count: sql<number>`count(*)::int` })
      .from(timeEntries)
      .where(
        and(inArray(timeEntries.taskId, ids), isNull(timeEntries.endedAt)),
      );
    activeTimerCount = result?.count || 0;
  }

  // 合計作業時間
  let totalTimeSeconds = 0;
  if (projectTaskIds.length > 0) {
    const ids = projectTaskIds.map((t) => t.id);
    const [result] = await db
      .select({
        total: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
      })
      .from(timeEntries)
      .where(
        and(inArray(timeEntries.taskId, ids), isNotNull(timeEntries.endedAt)),
      );
    totalTimeSeconds = result?.total || 0;
  }

  // ── 予実管理データ ──

  // プロジェクトの見積工数
  const [projectRow] = await db
    .select({ estimatedHours: projects.estimatedHours })
    .from(projects)
    .where(eq(projects.id, projectId))
    .limit(1);
  const projectEstimatedHours = projectRow?.estimatedHours || null;

  // タスク別の見積工数合計
  const [taskEstRow] = await db
    .select({
      total: sql<number>`coalesce(sum(${tasks.estimatedHours}), 0)`,
      count: sql<number>`count(${tasks.estimatedHours})::int`,
    })
    .from(tasks)
    .where(
      and(
        eq(tasks.projectId, projectId),
        isNull(tasks.parentTaskId),
        isNotNull(tasks.estimatedHours),
      ),
    );
  const taskEstimatedHoursTotal = taskEstRow?.total || 0;
  const taskEstimatedCount = taskEstRow?.count || 0;

  // メンバー別実績時間
  let memberTimeStats: {
    user_id: string;
    username: string;
    display_name: string | null;
    total_seconds: number;
  }[] = [];
  if (projectTaskIds.length > 0) {
    const ids = projectTaskIds.map((t) => t.id);
    const memberRows = await db
      .select({
        userId: timeEntries.userId,
        username: users.username,
        displayName: users.displayName,
        totalSeconds: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
      })
      .from(timeEntries)
      .innerJoin(users, eq(users.id, timeEntries.userId))
      .where(
        and(inArray(timeEntries.taskId, ids), isNotNull(timeEntries.endedAt)),
      )
      .groupBy(timeEntries.userId, users.username, users.displayName);

    memberTimeStats = memberRows.map((r) => ({
      user_id: r.userId,
      username: r.username,
      display_name: r.displayName,
      total_seconds: r.totalSeconds,
    }));
  }

  return NextResponse.json({
    status_counts: Array.from(mergedStatusCounts.entries()).map(
      ([status, count]) => ({
        status,
        count,
      }),
    ),
    priority_counts: priorityCounts.map((r) => ({
      priority: r.priority,
      count: r.count,
    })),
    tag_stats: tagStats,
    recent_completed: recentCompleted.map((r) => ({
      id: r.id,
      title: r.title,
      completed_at: serializeDbTimestamp(r.completedAt),
      priority: r.priority,
    })),
    active_timer_count: activeTimerCount,
    total_time_seconds: totalTimeSeconds,
    // 予実管理
    effort_tracking: {
      project_estimated_hours: projectEstimatedHours,
      task_estimated_hours_total: taskEstimatedHoursTotal,
      task_estimated_count: taskEstimatedCount,
      actual_hours: Math.round((totalTimeSeconds / 3600) * 100) / 100,
      member_stats: memberTimeStats,
    },
  });
}
