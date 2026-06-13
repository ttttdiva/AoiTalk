import { NextRequest, NextResponse } from "next/server";
import { eq, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationSessions,
  knowledgeSourcePermissions,
  knowledgeSources,
  notificationDeliveries,
  projectMembers,
  projects,
  recordAttachments,
  recordEvents,
  recordRows,
  recordTables,
  recordViews,
  spaces,
  tags,
  taskActivities,
  taskAssignees,
  taskComments,
  tasks,
  timeEntries,
  users,
} from "@/db/schema";
import { getSession } from "@/lib/auth";

type RouteContext = {
  params: Promise<{ id: string }>;
};

type UserSettings = Record<string, unknown>;
type BlockingRelation = { label: string; count: number };

function getSettings(value: unknown): UserSettings {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UserSettings)
    : {};
}

function lifecycleState(value: unknown): string {
  const settings = getSettings(value);
  const lifecycle = settings.account_lifecycle;
  if (lifecycle && typeof lifecycle === "object" && !Array.isArray(lifecycle)) {
    const state = (lifecycle as UserSettings).state;
    if (typeof state === "string") return state;
  }
  return "active";
}

async function requireAdmin() {
  const user = await getSession();
  if (!user) {
    return {
      error: NextResponse.json({ detail: "認証が必要です" }, { status: 401 }),
      user: null,
    };
  }
  if (user.role !== "admin") {
    return {
      error: NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 }),
      user: null,
    };
  }
  return { error: null, user };
}

async function countSpaces(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(spaces)
    .where(eq(spaces.ownerId, userId));
  return row?.count ?? 0;
}

async function countProjects(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(projects)
    .where(eq(projects.ownerId, userId));
  return row?.count ?? 0;
}

async function countProjectMembers(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(projectMembers)
    .where(eq(projectMembers.userId, userId));
  return row?.count ?? 0;
}

async function countInvitedProjectMembers(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(projectMembers)
    .where(eq(projectMembers.invitedBy, userId));
  return row?.count ?? 0;
}

async function countRecordTables(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(recordTables)
    .where(eq(recordTables.createdBy, userId));
  return row?.count ?? 0;
}

async function countRecordRows(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(recordRows)
    .where(eq(recordRows.createdBy, userId));
  return row?.count ?? 0;
}

async function countRecordViews(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(recordViews)
    .where(eq(recordViews.createdBy, userId));
  return row?.count ?? 0;
}

async function countRecordAttachments(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(recordAttachments)
    .where(eq(recordAttachments.createdBy, userId));
  return row?.count ?? 0;
}

async function countRecordEvents(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(recordEvents)
    .where(eq(recordEvents.actorId, userId));
  return row?.count ?? 0;
}

async function countTasks(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(tasks)
    .where(eq(tasks.createdBy, userId));
  return row?.count ?? 0;
}

async function countTaskAssignees(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(taskAssignees)
    .where(eq(taskAssignees.userId, userId));
  return row?.count ?? 0;
}

async function countTaskAssigners(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(taskAssignees)
    .where(eq(taskAssignees.assignedBy, userId));
  return row?.count ?? 0;
}

async function countTags(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(tags)
    .where(eq(tags.createdBy, userId));
  return row?.count ?? 0;
}

async function countTaskComments(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(taskComments)
    .where(eq(taskComments.userId, userId));
  return row?.count ?? 0;
}

async function countTimeEntries(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(timeEntries)
    .where(eq(timeEntries.userId, userId));
  return row?.count ?? 0;
}

async function countConversationSessions(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(conversationSessions)
    .where(eq(conversationSessions.userId, userId));
  return row?.count ?? 0;
}

async function countTaskActivities(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(taskActivities)
    .where(eq(taskActivities.userId, userId));
  return row?.count ?? 0;
}

async function countKnowledgeSources(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(knowledgeSources)
    .where(eq(knowledgeSources.ownerUserId, userId));
  return row?.count ?? 0;
}

async function countKnowledgeSourcePermissions(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(knowledgeSourcePermissions)
    .where(eq(knowledgeSourcePermissions.userId, userId));
  return row?.count ?? 0;
}

async function countKnowledgePermissionCreators(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(knowledgeSourcePermissions)
    .where(eq(knowledgeSourcePermissions.createdBy, userId));
  return row?.count ?? 0;
}

async function countNotificationDeliveries(userId: string) {
  const [row] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(notificationDeliveries)
    .where(eq(notificationDeliveries.userId, userId));
  return row?.count ?? 0;
}

async function collectBlockingRelations(userId: string): Promise<BlockingRelation[]> {
  const entries: Array<[string, Promise<number>]> = [
    ["所有スペース", countSpaces(userId)],
    ["所有プロジェクト", countProjects(userId)],
    ["プロジェクトメンバー", countProjectMembers(userId)],
    ["招待したプロジェクトメンバー", countInvitedProjectMembers(userId)],
    ["作成した台帳", countRecordTables(userId)],
    ["作成した台帳行", countRecordRows(userId)],
    ["作成した台帳ビュー", countRecordViews(userId)],
    ["添付ファイル", countRecordAttachments(userId)],
    ["台帳イベント", countRecordEvents(userId)],
    ["作成タスク", countTasks(userId)],
    ["タスク担当", countTaskAssignees(userId)],
    ["割り当て操作", countTaskAssigners(userId)],
    ["作成タグ", countTags(userId)],
    ["タスクコメント", countTaskComments(userId)],
    ["工数", countTimeEntries(userId)],
    ["会話履歴", countConversationSessions(userId)],
    ["タスク活動ログ", countTaskActivities(userId)],
    ["Knowledge Source", countKnowledgeSources(userId)],
    ["Knowledge Source権限", countKnowledgeSourcePermissions(userId)],
    ["Knowledge Source権限付与", countKnowledgePermissionCreators(userId)],
    ["通知", countNotificationDeliveries(userId)],
  ];

  const counts = await Promise.all(entries.map(([, promise]) => promise));
  return entries
    .map(([label], index) => ({ label, count: counts[index] }))
    .filter((entry) => entry.count > 0);
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;

  const { id } = await context.params;
  const [target] = await db.select().from(users).where(eq(users.id, id)).limit(1);
  if (!target) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }
  if (target.id === guard.user?.id) {
    return NextResponse.json(
      { detail: "自分自身は完全削除できません" },
      { status: 400 },
    );
  }
  if (lifecycleState(target.userSettings) !== "deleted") {
    return NextResponse.json(
      { detail: "完全削除できるのは削除済みユーザーだけです" },
      { status: 400 },
    );
  }

  const blockingRelations = await collectBlockingRelations(id);
  if (blockingRelations.length > 0) {
    return NextResponse.json(
      {
        detail: "関連データが残っているため完全削除できません",
        blocking_relations: blockingRelations,
      },
      { status: 409 },
    );
  }

  await db.delete(users).where(eq(users.id, id));

  return NextResponse.json({ success: true, user_id: id });
}
