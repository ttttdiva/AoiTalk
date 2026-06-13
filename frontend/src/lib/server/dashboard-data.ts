import { db } from "@/db";
import {
  projects,
  tags,
  taskTags,
  tasks,
  timeEntries,
  users,
} from "@/db/schema";
import { and, desc, eq, inArray, isNotNull, isNull, sql } from "drizzle-orm";
import { serializeDbTimestamp } from "@/lib/server/db-time";

type DashboardScope =
  | { type: "project"; id: string }
  | { type: "space"; id: string; projectIds?: string[] };

type MemberTimeStat = {
  user_id: string;
  username: string;
  display_name: string | null;
  total_seconds: number;
};

function scopeCondition(scope: DashboardScope, projectIds: string[]) {
  if (scope.type === "project") return eq(tasks.projectId, scope.id);
  if (projectIds.length === 0) return sql`false`;
  return inArray(tasks.projectId, projectIds);
}

export async function getDashboardData(scope: DashboardScope) {
  const projectRows =
    scope.type === "project"
      ? await db
          .select({
            id: projects.id,
            estimatedHours: projects.estimatedHours,
          })
          .from(projects)
          .where(eq(projects.id, scope.id))
          .limit(1)
      : scope.projectIds
        ? scope.projectIds.length === 0
          ? []
          : await db
              .select({
                id: projects.id,
                estimatedHours: projects.estimatedHours,
              })
              .from(projects)
              .where(inArray(projects.id, scope.projectIds))
        : await db
            .select({
              id: projects.id,
              estimatedHours: projects.estimatedHours,
            })
            .from(projects)
            .where(eq(projects.spaceId, scope.id));

  const projectIds = projectRows.map((project) => project.id);
  const condition = scopeCondition(scope, projectIds);

  const statusCounts = await db
    .select({
      status: tasks.status,
      count: sql<number>`count(*)::int`,
    })
    .from(tasks)
    .where(and(condition, isNull(tasks.parentTaskId)))
    .groupBy(tasks.status);

  const priorityCounts = await db
    .select({
      priority: tasks.priority,
      count: sql<number>`count(*)::int`,
    })
    .from(tasks)
    .where(and(condition, isNull(tasks.parentTaskId)))
    .groupBy(tasks.priority);

  const tagTimeRows = await db
    .select({
      tagId: tags.id,
      totalSeconds: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
    })
    .from(taskTags)
    .innerJoin(tasks, eq(tasks.id, taskTags.taskId))
    .innerJoin(tags, eq(tags.id, taskTags.tagId))
    .leftJoin(timeEntries, eq(timeEntries.taskId, tasks.id))
    .where(and(condition, isNotNull(timeEntries.endedAt)))
    .groupBy(tags.id);

  const tagTaskCounts = await db
    .select({
      tagId: taskTags.tagId,
      tagName: tags.name,
      tagColor: tags.color,
      count: sql<number>`count(distinct ${taskTags.taskId})::int`,
    })
    .from(taskTags)
    .innerJoin(tasks, eq(tasks.id, taskTags.taskId))
    .innerJoin(tags, eq(tags.id, taskTags.tagId))
    .where(condition)
    .groupBy(taskTags.tagId, tags.name, tags.color);

  const tagTimeMap = new Map(
    tagTimeRows.map((row) => [row.tagId, row.totalSeconds]),
  );
  const tagStats = tagTaskCounts.map((tag) => ({
    id: tag.tagId,
    name: tag.tagName,
    color: tag.tagColor,
    total_seconds: tagTimeMap.get(tag.tagId) || 0,
    task_count: tag.count,
  }));

  const recentCompleted = await db
    .select({
      id: tasks.id,
      title: tasks.title,
      completedAt: tasks.completedAt,
      priority: tasks.priority,
    })
    .from(tasks)
    .where(
      and(condition, isNotNull(tasks.completedAt), isNull(tasks.parentTaskId)),
    )
    .orderBy(desc(tasks.completedAt))
    .limit(5);

  const projectTaskIds = await db
    .select({ id: tasks.id })
    .from(tasks)
    .where(condition);

  let activeTimerCount = 0;
  let totalTimeSeconds = 0;
  let memberTimeStats: MemberTimeStat[] = [];

  if (projectTaskIds.length > 0) {
    const ids = projectTaskIds.map((task) => task.id);

    const [activeResult] = await db
      .select({ count: sql<number>`count(*)::int` })
      .from(timeEntries)
      .where(
        and(inArray(timeEntries.taskId, ids), isNull(timeEntries.endedAt)),
      );
    activeTimerCount = activeResult?.count || 0;

    const [timeResult] = await db
      .select({
        total: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
      })
      .from(timeEntries)
      .where(
        and(inArray(timeEntries.taskId, ids), isNotNull(timeEntries.endedAt)),
      );
    totalTimeSeconds = timeResult?.total || 0;

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

    memberTimeStats = memberRows.map((row) => ({
      user_id: row.userId,
      username: row.username,
      display_name: row.displayName,
      total_seconds: row.totalSeconds,
    }));
  }

  const projectEstimatedHours =
    scope.type === "project"
      ? projectRows[0]?.estimatedHours || null
      : projectRows.reduce(
          (total, project) => total + Number(project.estimatedHours || 0),
          0,
        ) || null;

  const [taskEstRow] = await db
    .select({
      total: sql<number>`coalesce(sum(${tasks.estimatedHours}), 0)`,
      count: sql<number>`count(${tasks.estimatedHours})::int`,
    })
    .from(tasks)
    .where(
      and(
        condition,
        isNull(tasks.parentTaskId),
        isNotNull(tasks.estimatedHours),
      ),
    );

  return {
    status_counts: statusCounts.map((row) => ({
      status: row.status,
      count: row.count,
    })),
    priority_counts: priorityCounts.map((row) => ({
      priority: row.priority,
      count: row.count,
    })),
    tag_stats: tagStats,
    recent_completed: recentCompleted.map((row) => ({
      id: row.id,
      title: row.title,
      completed_at: serializeDbTimestamp(row.completedAt),
      priority: row.priority,
    })),
    active_timer_count: activeTimerCount,
    total_time_seconds: totalTimeSeconds,
    effort_tracking: {
      project_estimated_hours: projectEstimatedHours,
      task_estimated_hours_total: taskEstRow?.total || 0,
      task_estimated_count: taskEstRow?.count || 0,
      actual_hours: Math.round((totalTimeSeconds / 3600) * 100) / 100,
      member_stats: memberTimeStats,
    },
  };
}
