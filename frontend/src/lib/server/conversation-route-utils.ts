import { and, eq, gt, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  appGrants,
  appTargets,
  apps,
  conversationMessages,
  conversationParticipants,
  conversationSessions,
  projectApps,
  projectMembers,
  projects,
  users,
} from "@/db/schema";
import { decryptTextIfNeeded } from "./field-crypto";
import { hasProjectPermission } from "./project-permissions";

export type ConversationSessionRow = typeof conversationSessions.$inferSelect;

export type ConversationScopeUser = {
  id: string;
  role?: string | null;
};

export class ConversationScopeError extends Error {
  constructor(
    readonly status: 400 | 403 | 404,
    message: string,
  ) {
    super(message);
    this.name = "ConversationScopeError";
  }
}

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const APP_PERMISSION_RANK: Record<string, number> = {
  viewer: 10,
  runner: 20,
  developer: 30,
  maintainer: 40,
  admin: 50,
};

function normalizeUuid(value: string, label: string): string {
  const normalized = value.trim();
  if (!UUID_PATTERN.test(normalized)) {
    throw new ConversationScopeError(400, `${label}の形式が不正です`);
  }
  return normalized;
}

export async function canReadProject(
  projectId: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  const normalizedProjectId = normalizeUuid(projectId, "project_id");

  const [project] = await db
    .select({ ownerId: projects.ownerId })
    .from(projects)
    .where(and(eq(projects.id, normalizedProjectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) {
    throw new ConversationScopeError(404, "Projectが見つかりません");
  }
  if (user.role === "admin") return true;
  if (project.ownerId === user.id) return true;

  const [membership] = await db
    .select({ permissions: projectMembers.permissions })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.projectId, normalizedProjectId),
        eq(projectMembers.userId, user.id),
      ),
    )
    .limit(1);
  if (!membership) return false;

  return hasProjectPermission(membership.permissions, "read");
}

export async function canWriteProject(
  projectId: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  let normalizedProjectId: string;
  try {
    normalizedProjectId = normalizeUuid(projectId, "project_id");
  } catch (error) {
    if (error instanceof ConversationScopeError) return false;
    throw error;
  }

  const [project] = await db
    .select({ ownerId: projects.ownerId })
    .from(projects)
    .where(and(eq(projects.id, normalizedProjectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) return false;
  if (user.role === "admin" || project.ownerId === user.id) return true;

  const [membership] = await db
    .select({ permissions: projectMembers.permissions })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.projectId, normalizedProjectId),
        eq(projectMembers.userId, user.id),
      ),
    )
    .limit(1);
  return Boolean(
    membership && hasProjectPermission(membership.permissions, "write"),
  );
}

export async function validateAppConversationScope(options: {
  appId: string | null | undefined;
  appTargetId?: string | null;
  projectId?: string | null;
  user: ConversationScopeUser;
}) {
  const rawAppId = options.appId?.trim() || null;
  const rawTargetId = options.appTargetId?.trim() || null;
  const rawProjectId = options.projectId?.trim() || null;

  if (!rawAppId) {
    if (rawTargetId) {
      throw new ConversationScopeError(
        400,
        "app_target_idにはapp_idが必要です",
      );
    }
    return null;
  }

  const appId = normalizeUuid(rawAppId, "app_id");
  const appTargetId = rawTargetId
    ? normalizeUuid(rawTargetId, "app_target_id")
    : null;
  const projectId = rawProjectId
    ? normalizeUuid(rawProjectId, "project_id")
    : null;

  const [app] = await db
    .select()
    .from(apps)
    .where(eq(apps.id, appId))
    .limit(1);
  if (!app) {
    throw new ConversationScopeError(404, "Appが見つかりません");
  }

  let projectReadable = false;
  if (projectId) {
    projectReadable = await canReadProject(projectId, options.user);
  }

  let projectBinding: { enabled: boolean } | undefined;
  if (projectId) {
    [projectBinding] = await db
      .select({ enabled: projectApps.enabled })
      .from(projectApps)
      .where(
        and(
          eq(projectApps.projectId, projectId),
          eq(projectApps.appId, appId),
        ),
      )
      .limit(1);
  }

  let permission =
    options.user.role === "admin" || app.ownerUserId === options.user.id
      ? "admin"
      : null;
  const grants = await db
    .select({ userId: appGrants.userId, projectId: appGrants.projectId, permission: appGrants.permission })
    .from(appGrants)
    .where(
      and(
        eq(appGrants.appId, appId),
        projectId
          ? or(
              eq(appGrants.userId, options.user.id),
              eq(appGrants.projectId, projectId),
            )
          : eq(appGrants.userId, options.user.id),
      ),
    );
  for (const grant of grants) {
    if (grant.userId !== options.user.id && (!projectId || !projectReadable)) {
      continue;
    }
    if (
      !permission ||
      APP_PERMISSION_RANK[grant.permission] > APP_PERMISSION_RANK[permission]
    ) {
      permission = grant.permission;
    }
  }
  // ProjectへAppが関連付いていて、ユーザーがそのProjectを閲覧できる場合は、
  // Python側のAppServiceと同じく最低限viewerとして扱う。
  if (!permission && projectReadable && projectBinding?.enabled) {
    permission = "viewer";
  }
  if (!permission && app.visibility === "public") permission = "viewer";
  if (!permission) {
    throw new ConversationScopeError(403, "Appを閲覧できません");
  }

  if (projectId) {
    if (!projectReadable || !projectBinding?.enabled) {
      throw new ConversationScopeError(
        403,
        "このProjectではAppを利用できません",
      );
    }
  }

  if (appTargetId) {
    const [target] = await db
      .select({ id: appTargets.id })
      .from(appTargets)
      .where(and(eq(appTargets.id, appTargetId), eq(appTargets.appId, appId)))
      .limit(1);
    if (!target) {
      throw new ConversationScopeError(404, "App Targetが見つかりません");
    }
  }

  return { appId, appTargetId, projectId };
}

export function sessionToSnake(
  row: Record<string, unknown>,
): Record<string, unknown> {
  const map: Record<string, string> = {
    id: "id",
    userId: "user_id",
    characterName: "character_name",
    title: "title",
    sessionStart: "session_start",
    lastActivity: "last_activity",
    messageCount: "message_count",
    context: "context",
    currentSummary: "current_summary",
    isActive: "is_active",
    deletedAt: "deleted_at",
    projectId: "project_id",
    appId: "app_id",
    appTargetId: "app_target_id",
    developmentStatus: "development_status",
    lastReadAt: "last_read_at",
    isGroupChat: "is_group_chat",
    groupCharacterNames: "group_character_names",
  };
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(row)) {
    out[map[key] ?? key] =
      key === "currentSummary" && typeof value === "string"
        ? decryptTextIfNeeded(value, "conversation_sessions.current_summary")
        : value;
  }
  return out;
}

export function messageToSnake(row: typeof conversationMessages.$inferSelect) {
  const content = decryptTextIfNeeded(
    row.content,
    "conversation_messages.content",
  );
  return {
    id: row.id,
    session_id: row.sessionId,
    role: row.role,
    content,
    metadata: row.messageMetadata,
    sender_type: row.senderType,
    sender_id: row.senderId,
    sender_display_name: row.senderDisplayName,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
    token_count: row.tokenCount,
    parent_message_id: row.parentMessageId,
    branch_index: row.branchIndex,
    is_active_branch: row.isActiveBranch,
  };
}

export async function getConversationSessionForUser(
  id: string,
  userId: string,
  includeDeleted = false,
): Promise<ConversationSessionRow | null> {
  const [participant] = await db
    .select({ sessionId: conversationParticipants.sessionId })
    .from(conversationParticipants)
    .where(
      and(
        eq(conversationParticipants.sessionId, id),
        eq(conversationParticipants.participantType, "user"),
        eq(conversationParticipants.participantId, userId),
        eq(conversationParticipants.status, "joined"),
      ),
    )
    .limit(1);

  const sessionConditions = [
    eq(conversationSessions.id, id),
    participant
      ? eq(conversationSessions.id, participant.sessionId)
      : eq(conversationSessions.userId, userId),
  ];
  if (!includeDeleted) {
    sessionConditions.push(isNull(conversationSessions.deletedAt));
  }
  const [session] = await db
    .select()
    .from(conversationSessions)
    .where(and(...sessionConditions))
    .limit(1);

  if (!session) return null;
  if (!session.projectId) return session;

  const [principal] = await db
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, userId))
    .limit(1);
  try {
    if (
      !(await canReadProject(String(session.projectId), {
        id: userId,
        role: principal?.role,
      }))
    ) {
      return null;
    }
  } catch (error) {
    if (error instanceof ConversationScopeError) return null;
    throw error;
  }
  return session;
}

/**
 * Permission check for restoring a deleted conversation.  The normal
 * canManageWritableConversationSession intentionally excludes tombstones, so
 * restore needs the same ACL policy against an include-deleted lookup.
 */
export async function canManageDeletedWritableConversationSession(
  id: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  const session = await getConversationSessionForUser(id, user.id, true);
  if (!session || !session.deletedAt) return false;
  if (
    session.projectId &&
    !(await canWriteProject(String(session.projectId), user))
  ) {
    return false;
  }
  if (session.userId === user.id) return true;
  const participantRole = await getJoinedParticipantRole(id, user.id);
  return participantRole === "owner" || participantRole === "admin";
}

export async function getLiveConversationSession(
  id: string,
  userId: string,
): Promise<ConversationSessionRow | null> {
  return getConversationSessionForUser(id, userId);
}

async function getJoinedParticipantRole(
  id: string,
  userId: string,
): Promise<string | null> {
  const [participant] = await db
    .select({ role: conversationParticipants.role })
    .from(conversationParticipants)
    .where(
      and(
        eq(conversationParticipants.sessionId, id),
        eq(conversationParticipants.participantType, "user"),
        eq(conversationParticipants.participantId, userId),
        eq(conversationParticipants.status, "joined"),
      ),
    )
    .limit(1);
  return participant?.role ?? null;
}

export async function canManageConversationSession(
  id: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  const session = await getLiveConversationSession(id, user.id);
  if (!session) return false;
  if (session.userId === user.id) return true;

  const participantRole = await getJoinedParticipantRole(id, user.id);
  return participantRole === "owner" || participantRole === "admin";
}

export async function canWriteConversationSession(
  id: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  const session = await getLiveConversationSession(id, user.id);
  if (!session) return false;
  if (
    session.projectId &&
    !(await canWriteProject(String(session.projectId), user))
  ) {
    return false;
  }
  if (session.userId === user.id) return true;

  const participantRole = await getJoinedParticipantRole(id, user.id);
  return ["owner", "admin", "member"].includes(participantRole ?? "");
}

export async function canManageWritableConversationSession(
  id: string,
  user: ConversationScopeUser,
): Promise<boolean> {
  const session = await getLiveConversationSession(id, user.id);
  if (!session) return false;
  if (
    session.projectId &&
    !(await canWriteProject(String(session.projectId), user))
  ) {
    return false;
  }
  if (session.userId === user.id) return true;
  const participantRole = await getJoinedParticipantRole(id, user.id);
  return participantRole === "owner" || participantRole === "admin";
}

async function hasMessagesAfterDeletion(
  id: string,
  deletedAt: Date,
): Promise<boolean> {
  const [message] = await db
    .select({ id: conversationMessages.id })
    .from(conversationMessages)
    .where(
      and(
        eq(conversationMessages.sessionId, id),
        gt(conversationMessages.createdAt, deletedAt),
      ),
    )
    .limit(1);

  return Boolean(message);
}

export async function resumeConversationSession(
  id: string,
  user: ConversationScopeUser,
): Promise<ConversationSessionRow | null> {
  const [existing] = await db
    .select()
    .from(conversationSessions)
    .where(and(eq(conversationSessions.id, id)))
    .limit(1);

  if (!existing) return null;
  const accessible = await getConversationSessionForUser(id, user.id, true);
  if (!accessible) return null;

  if (
    existing.projectId &&
    !(await canWriteProject(String(existing.projectId), {
      id: user.id,
      role: user.role,
    }))
  ) {
    return null;
  }
  if (existing.userId !== user.id) {
    const participantRole = await getJoinedParticipantRole(id, user.id);
    if (participantRole !== "owner" && participantRole !== "admin") {
      return null;
    }
  }

  if (existing.deletedAt) {
    const shouldRepair = await hasMessagesAfterDeletion(id, existing.deletedAt);
    if (!shouldRepair) return null;
  }

  const [session] = await db
    .update(conversationSessions)
    .set({
      isActive: true,
      deletedAt: null,
    })
    .where(and(eq(conversationSessions.id, id)))
    .returning();

  return session ?? null;
}
