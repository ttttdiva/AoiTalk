import {
  pgTable,
  uuid,
  varchar,
  text,
  boolean,
  timestamp,
  date,
  json,
  jsonb,
  integer,
  doublePrecision,
  primaryKey,
  unique,
  index,
  type AnyPgColumn,
} from "drizzle-orm/pg-core";
import { DEFAULT_TASK_TIMEZONE } from "../lib/task-time";

// ─── ユーザー・認証 ───

export const users = pgTable("users", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  username: varchar("username").notNull(),
  email: varchar("email"),
  passwordHash: varchar("password_hash").notNull(),
  displayName: varchar("display_name"),
  preferredCharacter: varchar("preferred_character"),
  role: varchar("role"),
  isActive: boolean("is_active"),
  isPasswordResetRequired: boolean("is_password_reset_required"),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
  lastLogin: timestamp("last_login"),
  userSettings: json("user_settings"),
});

// ─── スペース（プロジェクトを束ねる上位概念）───

export const spaces = pgTable("spaces", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name").notNull(),
  slug: varchar("slug").notNull(),
  description: text("description"),
  color: varchar("color"),
  ownerId: uuid("owner_id")
    .references(() => users.id)
    .notNull(),
  sortOrder: doublePrecision("sort_order").default(0),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

// ─── プロジェクト管理 ───

export const projects = pgTable("projects", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name").notNull(),
  description: text("description"),
  slug: varchar("slug").notNull(),
  ownerId: uuid("owner_id")
    .references(() => users.id)
    .notNull(),
  spaceId: uuid("space_id").references(() => spaces.id),
  knowledgeNodeId: uuid("knowledge_node_id").references(
    (): AnyPgColumn => knowledgeNodes.id,
    { onDelete: "restrict" },
  ),
  allowJoinRequests: boolean("allow_join_requests"),
  storageQuotaMb: integer("storage_quota_mb"),
  storageUsedMb: doublePrecision("storage_used_mb"),
  estimatedHours: doublePrecision("estimated_hours"),
  isCompleted: boolean("is_completed").default(false).notNull(),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
  deletedAt: timestamp("deleted_at"),
  projectMetadata: json("project_metadata"),
  aliases: json("aliases").default([]),
});

export const projectMembers = pgTable("project_members", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id)
    .notNull(),
  userId: uuid("user_id")
    .references(() => users.id)
    .notNull(),
  role: varchar("role"),
  permissions: json("permissions"),
  joinedAt: timestamp("joined_at"),
  invitedBy: uuid("invited_by"),
});

export const projectQaEntries = pgTable("project_qa_entries", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  knowledgeNodeId: uuid("knowledge_node_id").references(
    (): AnyPgColumn => knowledgeNodes.id,
    { onDelete: "set null" },
  ),
  question: text("question").notNull(),
  answer: text("answer"),
  normalizedQuestionHash: varchar("normalized_question_hash", { length: 128 }),
  status: varchar("status", { length: 32 }).default("unanswered").notNull(),
  reviewState: varchar("review_state", { length: 32 }).default("candidate").notNull(),
  confidence: doublePrecision("confidence").default(1).notNull(),
  askedCount: integer("asked_count").default(1).notNull(),
  sourceSessionId: uuid("source_session_id").references(
    (): AnyPgColumn => conversationSessions.id,
    { onDelete: "set null" },
  ),
  sourceMessageIds: json("source_message_ids").default([]).notNull(),
  sourceAgentRunIds: json("source_agent_run_ids").default([]).notNull(),
  sourceToolCallIds: json("source_tool_call_ids").default([]).notNull(),
  answerSourceRefs: json("answer_source_refs").default([]).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  updatedBy: uuid("updated_by").references(() => users.id),
  createdByAgent: boolean("created_by_agent").default(false).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  lastAskedAt: timestamp("last_asked_at").defaultNow().notNull(),
  deletedAt: timestamp("deleted_at"),
});

export const recordTables = pgTable("record_tables", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  name: varchar("name", { length: 200 }).notNull(),
  description: text("description"),
  icon: varchar("icon", { length: 64 }),
  sortOrder: doublePrecision("sort_order").default(0),
  schemaVersion: integer("schema_version").default(1),
  memoryPolicy: varchar("memory_policy", { length: 32 }).default("manual"),
  defaultSensitivity: varchar("default_sensitivity", { length: 32 }).default("normal"),
  tableMetadata: json("table_metadata"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
  deletedAt: timestamp("deleted_at"),
});

export const recordFields = pgTable("record_fields", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  tableId: uuid("table_id")
    .references(() => recordTables.id, { onDelete: "cascade" })
    .notNull(),
  key: varchar("field_key", { length: 120 }).notNull(),
  label: varchar("label", { length: 200 }).notNull(),
  fieldType: varchar("field_type", { length: 32 }).notNull(),
  options: json("options"),
  required: boolean("required").default(false),
  uniqueValue: boolean("unique_value").default(false),
  sortOrder: doublePrecision("sort_order").default(0),
  isTitle: boolean("is_title").default(false),
  isDue: boolean("is_due").default(false),
  sensitivity: varchar("sensitivity", { length: 32 }).default("normal"),
  fieldMetadata: json("field_metadata"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
  deletedAt: timestamp("deleted_at"),
});

export const recordRows = pgTable("record_rows", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  tableId: uuid("table_id")
    .references(() => recordTables.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  values: json("values").default({}),
  title: text("title"),
  status: varchar("status", { length: 64 }),
  dueAt: timestamp("due_at"),
  searchText: text("search_text"),
  sensitivity: varchar("sensitivity", { length: 32 }).default("normal"),
  rowMetadata: json("row_metadata"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
  deletedAt: timestamp("deleted_at"),
});

export const recordViews = pgTable("record_views", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  tableId: uuid("table_id")
    .references(() => recordTables.id, { onDelete: "cascade" })
    .notNull(),
  name: varchar("name", { length: 200 }).notNull(),
  viewType: varchar("view_type", { length: 32 }).default("grid"),
  config: json("config").default({}),
  sortOrder: doublePrecision("sort_order").default(0),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const recordAttachments = pgTable("record_attachments", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  rowId: uuid("row_id")
    .references(() => recordRows.id, { onDelete: "cascade" })
    .notNull(),
  filePath: text("file_path").notNull(),
  fileName: varchar("file_name", { length: 255 }),
  mimeType: varchar("mime_type", { length: 120 }),
  sizeBytes: integer("size_bytes"),
  sourceHash: varchar("source_hash", { length: 128 }),
  attachmentMetadata: json("attachment_metadata"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
});

export const recordEvents = pgTable("record_events", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  tableId: uuid("table_id").references(() => recordTables.id, { onDelete: "cascade" }),
  rowId: uuid("row_id").references(() => recordRows.id, { onDelete: "cascade" }),
  actorId: uuid("actor_id").references(() => users.id),
  eventType: varchar("event_type", { length: 64 }).notNull(),
  payload: json("payload").default({}),
  createdAt: timestamp("created_at").defaultNow(),
});

// ─── タスク管理 ───

export const tasks = pgTable("tasks", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id)
    .notNull(),
  legacyLocalTaskId: uuid("legacy_local_task_id"),
  knowledgeNodeId: uuid("knowledge_node_id").references(
    (): AnyPgColumn => knowledgeNodes.id,
    { onDelete: "set null" },
  ).unique(),
  title: varchar("title").notNull(),
  description: text("description"),
  status: varchar("status").default("todo").notNull(),
  priority: varchar("priority").default("medium"),
  startAt: timestamp("start_at", { mode: "string" }),
  endAt: timestamp("end_at", { mode: "string" }),
  allDay: boolean("all_day").default(false).notNull(),
  reminderOffsets: json("reminder_offsets"),
  notificationsEnabled: boolean("notifications_enabled").default(true).notNull(),
  source: varchar("source").default("local").notNull(),
  createdBy: uuid("created_by"),
  completedAt: timestamp("completed_at", { mode: "string" }),
  archivedAt: timestamp("archived_at", { mode: "string" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  deletedAt: timestamp("deleted_at", { mode: "string" }),
  taskMetadata: json("task_metadata"),
  estimatedHours: doublePrecision("estimated_hours"),
  sortOrder: doublePrecision("sort_order").default(0).notNull(),
  parentTaskId: uuid("parent_task_id").references((): AnyPgColumn => tasks.id, {
    onDelete: "cascade",
  }),
});

export const taskAssignees = pgTable("task_assignees", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  userId: uuid("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  isPrimary: boolean("is_primary").default(false).notNull(),
  assignedAt: timestamp("assigned_at").defaultNow().notNull(),
  assignedBy: uuid("assigned_by"),
});

export const tags = pgTable("tags", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  spaceId: uuid("space_id")
    .references(() => spaces.id, { onDelete: "cascade" })
    .notNull(),
  name: varchar("name").notNull(),
  color: varchar("color"),
  createdBy: uuid("created_by"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const taskTags = pgTable(
  "task_tags",
  {
    taskId: uuid("task_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    tagId: uuid("tag_id")
      .references(() => tags.id, { onDelete: "cascade" })
      .notNull(),
  },
  (table) => [primaryKey({ columns: [table.taskId, table.tagId] })],
);

export const taskComments = pgTable("task_comments", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  userId: uuid("user_id")
    .references(() => users.id)
    .notNull(),
  content: text("content").notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const taskAttachments = pgTable("task_attachments", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  filePath: text("file_path").notNull(),
  displayName: varchar("display_name").notNull(),
  mimeType: varchar("mime_type"),
  sizeBytes: integer("size_bytes").default(0).notNull(),
  kind: varchar("kind").default("file").notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  attachmentMetadata: jsonb("metadata").default({}),
});

/** ファイル以外も含むタスク参照。task_attachmentsとは責務を分ける。 */
export const taskReferences = pgTable(
  "task_references",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    taskId: uuid("task_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id")
      .references(() => projects.id, { onDelete: "cascade" })
      .notNull(),
    referenceType: varchar("reference_type", { length: 80 }).notNull(),
    relationType: varchar("relation_type", { length: 32 })
      .default("related")
      .notNull(),
    targetId: text("target_id"),
    targetPath: text("target_path"),
    targetUrl: text("target_url"),
    displayName: varchar("display_name", { length: 500 }).notNull(),
    dedupeKey: varchar("dedupe_key", { length: 1200 }).notNull(),
    referenceMetadata: jsonb("metadata").default({}),
    createdBy: uuid("created_by").references(() => users.id),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    unique("uq_task_references_target").on(
      table.taskId,
      table.referenceType,
      table.relationType,
      table.dedupeKey,
    ),
    index("ix_task_references_task_id").on(table.taskId),
    index("ix_task_references_project_id").on(table.projectId),
    index("ix_task_references_created_by").on(table.createdBy),
  ],
);

export const taskRecurrenceRules = pgTable("task_recurrence_rules", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .unique()
    .notNull(),
  rrule: text("rrule").notNull(),
  timezone: varchar("timezone").default(DEFAULT_TASK_TIMEZONE).notNull(),
  horizonDays: integer("horizon_days").default(90).notNull(),
  triggerStatus: varchar("trigger_status").default("closed"),
  createNew: boolean("create_new").default(false),
  recurForever: boolean("recur_forever").default(true),
  resetStatusTo: varchar("reset_status_to").default("open"),
  endCount: integer("end_count"),
  endDate: timestamp("end_date", { mode: "string" }),
  skipWeekend: boolean("skip_weekend").default(false).notNull(),
  skipHoliday: boolean("skip_holiday").default(false).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const taskOccurrences = pgTable("task_occurrences", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  startAt: timestamp("start_at", { mode: "string" }).notNull(),
  endAt: timestamp("end_at", { mode: "string" }).notNull(),
  status: varchar("status").default("todo").notNull(),
  allDay: boolean("all_day").default(false).notNull(),
  reminderOffsets: json("reminder_offsets"),
  sourceKind: varchar("source_kind").default("task_schedule").notNull(),
  isGenerated: boolean("is_generated").default(false).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  deletedAt: timestamp("deleted_at", { mode: "string" }),
});

export const timeEntries = pgTable("time_entries", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  occurrenceId: uuid("occurrence_id").references(() => taskOccurrences.id),
  userId: uuid("user_id")
    .references(() => users.id)
    .notNull(),
  startedAt: timestamp("started_at", { mode: "string" }).notNull(),
  endedAt: timestamp("ended_at", { mode: "string" }),
  source: varchar("source").default("manual").notNull(),
  note: text("note"),
  createdAt: timestamp("created_at", { mode: "string" }).defaultNow().notNull(),
  updatedAt: timestamp("updated_at", { mode: "string" }).defaultNow().notNull(),
  entryMetadata: json("entry_metadata"),
  deletedAt: timestamp("deleted_at", { mode: "string" }),
});

// ─── 会話管理 ───

export const conversationSessions = pgTable("conversation_sessions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: varchar("user_id").notNull(),
  characterName: varchar("character_name").notNull(),
  sessionStart: timestamp("session_start"),
  lastActivity: timestamp("last_activity"),
  messageCount: integer("message_count"),
  context: json("context"),
  currentSummary: text("current_summary"),
  isActive: boolean("is_active"),
  title: varchar("title").default(""),
  deletedAt: timestamp("deleted_at"),
  projectId: uuid("project_id").references(() => projects.id),
  isGroupChat: boolean("is_group_chat").default(false),
  groupCharacterNames: json("group_character_names"),
  rpSettings: json("rp_settings").default({}),
});

export const conversationParticipants = pgTable("conversation_participants", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sessionId: uuid("session_id")
    .references(() => conversationSessions.id, { onDelete: "cascade" })
    .notNull(),
  participantType: varchar("participant_type", { length: 32 }).notNull(),
  participantId: varchar("participant_id", { length: 200 }).notNull(),
  displayName: varchar("display_name", { length: 200 }),
  role: varchar("role", { length: 32 }),
  status: varchar("status", { length: 32 }),
  autoRespond: boolean("auto_respond"),
  participantMetadata: json("participant_metadata"),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
});

export const conversationMessages = pgTable("conversation_messages", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sessionId: uuid("session_id")
    .references(() => conversationSessions.id, { onDelete: "cascade" })
    .notNull(),
  role: varchar("role").notNull(),
  content: text("content").notNull(),
  messageMetadata: json("message_metadata"),
  senderType: varchar("sender_type", { length: 32 }),
  senderId: varchar("sender_id", { length: 200 }),
  senderDisplayName: varchar("sender_display_name", { length: 200 }),
  createdAt: timestamp("created_at"),
  tokenCount: integer("token_count"),
  parentMessageId: uuid("parent_message_id"),
  branchIndex: integer("branch_index").default(0),
  isActiveBranch: boolean("is_active_branch").default(true),
  updatedAt: timestamp("updated_at"),
  deletedAt: timestamp("deleted_at"),
});

export const conversationArchives = pgTable("conversation_archives", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: varchar("user_id").notNull(),
  characterName: varchar("character_name").notNull(),
  originalSessionId: varchar("original_session_id"),
  summary: text("summary").notNull(),
  messageCount: integer("message_count"),
  startTime: timestamp("start_time"),
  endTime: timestamp("end_time"),
  messageMetadata: json("message_metadata"),
  archivedAt: timestamp("archived_at"),
});

export const conversationHistory = pgTable("conversation_history", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: varchar("user_id").notNull(),
  sessionId: uuid("session_id"),
  characterName: varchar("character_name").notNull(),
  role: varchar("role").notNull(),
  content: text("content").notNull(),
  messageMetadata: json("message_metadata"),
  createdAt: timestamp("created_at"),
  tokenCount: integer("token_count"),
  functionCallData: json("function_call_data"),
});

// ─── タスク活動ログ ───

export const taskActivities = pgTable("task_activities", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id)
    .notNull(),
  userId: uuid("user_id").references(() => users.id),
  activityType: varchar("activity_type", { length: 64 }).notNull(),
  payload: json("payload"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

// ─── タスク依存関係 ───

export const taskDependencies = pgTable("task_dependencies", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id)
    .notNull(),
  dependsOnTaskId: uuid("depends_on_task_id")
    .references(() => tasks.id)
    .notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

// ─── フィードバック ───

export const feedback = pgTable("feedback", {
  id: varchar("id", { length: 50 }).primaryKey(),
  sessionId: varchar("session_id", { length: 50 }),
  message: text("message").notNull(),
  character: varchar("character", { length: 100 }),
  userInput: text("user_input"),
  category: varchar("category", { length: 50 }).notNull(),
  comment: text("comment"),
  resolved: boolean("resolved").default(false),
  resolvedAt: timestamp("resolved_at"),
  resolvedBy: varchar("resolved_by", { length: 100 }),
  createdAt: timestamp("created_at").defaultNow(),
  feedbackMetadata: jsonb("feedback_metadata"),
});

// ─── Knowledge Workspace ───

export const knowledgeSources = pgTable("knowledge_sources", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name", { length: 200 }).notNull(),
  description: text("description"),
  rootPath: text("root_path").notNull(),
  sourceType: varchar("source_type", { length: 40 }).default("local_dir").notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id),
  accessPolicy: json("access_policy"),
  includePatterns: json("include_patterns"),
  excludePatterns: json("exclude_patterns"),
  syncMode: varchar("sync_mode", { length: 20 }).default("manual").notNull(),
  writePolicy: varchar("write_policy", { length: 40 }).default("propose_patch").notNull(),
  status: varchar("status", { length: 20 }),
  documentCount: integer("document_count"),
  chunkCount: integer("chunk_count"),
  lastSyncedAt: timestamp("last_synced_at"),
  errorMessage: text("error_message"),
  growiApiToken: text("growi_api_token"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const knowledgeSourcePermissions = pgTable("knowledge_source_permissions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sourceId: uuid("source_id")
    .references(() => knowledgeSources.id, { onDelete: "cascade" })
    .notNull(),
  userId: uuid("user_id").references(() => users.id, { onDelete: "cascade" }),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  permission: varchar("permission", { length: 20 }).default("read").notNull(),
  createdAt: timestamp("created_at").defaultNow(),
  createdBy: uuid("created_by").references(() => users.id),
});

export const knowledgeDocuments = pgTable("knowledge_documents", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sourceId: uuid("source_id")
    .references(() => knowledgeSources.id, { onDelete: "cascade" })
    .notNull(),
  path: text("path").notNull(),
  resolvedAbsolutePath: text("resolved_absolute_path"),
  title: varchar("title", { length: 500 }),
  extension: varchar("extension", { length: 32 }),
  mimeType: varchar("mime_type", { length: 120 }),
  contentHash: varchar("content_hash", { length: 64 }),
  modifiedAt: timestamp("modified_at"),
  sizeBytes: integer("size_bytes"),
  frontmatterJson: json("frontmatter_json"),
  tags: json("tags"),
  projectRefs: json("project_refs"),
  taskRefs: json("task_refs"),
  status: varchar("status", { length: 20 }),
  lastIndexedAt: timestamp("last_indexed_at"),
  errorMessage: text("error_message"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const knowledgeChunks = pgTable("knowledge_chunks", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  documentId: uuid("document_id")
    .references(() => knowledgeDocuments.id, { onDelete: "cascade" })
    .notNull(),
  headingPath: json("heading_path"),
  chunkIndex: integer("chunk_index").notNull(),
  text: text("text").notNull(),
  tokenCount: integer("token_count"),
  contentHash: varchar("content_hash", { length: 64 }),
  vectorId: varchar("vector_id", { length: 100 }),
  metadataJson: json("metadata_json"),
  createdAt: timestamp("created_at").defaultNow(),
});

export const knowledgeLinks = pgTable("knowledge_links", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sourceDocumentId: uuid("source_document_id")
    .references(() => knowledgeDocuments.id, { onDelete: "cascade" })
    .notNull(),
  targetPathOrUrl: text("target_path_or_url").notNull(),
  linkType: varchar("link_type", { length: 20 }).notNull(),
  resolvedDocumentId: uuid("resolved_document_id").references(
    (): AnyPgColumn => knowledgeDocuments.id,
  ),
  createdAt: timestamp("created_at").defaultNow(),
});

export const knowledgeAnnotations = pgTable("knowledge_annotations", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  documentId: uuid("document_id")
    .references(() => knowledgeDocuments.id, { onDelete: "cascade" })
    .notNull(),
  annotationType: varchar("annotation_type", { length: 40 }).notNull(),
  contentJson: json("content_json"),
  confidence: doublePrecision("confidence"),
  source: varchar("source", { length: 20 }).notNull(),
  status: varchar("status", { length: 20 }).notNull(),
  actorUserId: uuid("actor_user_id").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const knowledgeEditEvents = pgTable("knowledge_edit_events", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  documentId: uuid("document_id")
    .references(() => knowledgeDocuments.id, { onDelete: "cascade" })
    .notNull(),
  actorUserId: uuid("actor_user_id").references(() => users.id),
  operation: varchar("operation", { length: 40 }).notNull(),
  diff: text("diff").notNull(),
  reason: text("reason"),
  status: varchar("status", { length: 20 }).notNull(),
  preHash: varchar("pre_hash", { length: 64 }),
  postHash: varchar("post_hash", { length: 64 }),
  createdAt: timestamp("created_at").defaultNow(),
  appliedAt: timestamp("applied_at"),
});

// ─── DB正本 Docs ───

export const knowledgeWorkspaces = pgTable("knowledge_workspaces", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name", { length: 200 }).notNull(),
  description: text("description"),
  ownerUserId: uuid("owner_user_id").references(() => users.id, {
    onDelete: "set null",
  }),
  settingsJson: json("settings_json").default({}).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_knowledge_workspaces_owner_user").on(table.ownerUserId),
]);

export const knowledgeNodes = pgTable("knowledge_nodes", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  parentId: uuid("parent_id").references((): AnyPgColumn => knowledgeNodes.id, {
    onDelete: "cascade",
  }),
  rootPageId: uuid("root_page_id").references(
    (): AnyPgColumn => knowledgeNodes.id,
    { onDelete: "set null" },
  ),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "set null",
  }),
  systemKey: text("system_key"),
  title: text("title").notNull(),
  aliases: json("aliases").$type<string[]>().default([]),
  description: text("description").default("").notNull(),
  bodyJson: json("body_json").default({}).notNull(),
  bodyText: text("body_text").default("").notNull(),
  nodeType: varchar("node_type", { length: 40 }).default("node").notNull(),
  displayProps: json("display_props").default({}).notNull(),
  queryJson: json("query_json"),
  viewJson: json("view_json").default({}).notNull(),
  dayDate: date("day_date"),
  sortOrder: doublePrecision("sort_order").default(0).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  updatedBy: uuid("updated_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  archivedAt: timestamp("archived_at"),
}, (table) => [
  unique("uq_knowledge_nodes_workspace_system_key").on(table.workspaceId, table.systemKey),
  index("ix_knowledge_nodes_workspace").on(table.workspaceId),
  index("ix_knowledge_nodes_workspace_parent_sort").on(table.workspaceId, table.parentId, table.sortOrder),
  index("ix_knowledge_nodes_workspace_project").on(table.workspaceId, table.projectId),
  index("ix_knowledge_nodes_root_page").on(table.rootPageId),
  index("ix_knowledge_nodes_archived_at").on(table.archivedAt),
]);

export const knowledgeSupertags = pgTable("knowledge_supertags", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  parentSupertagId: uuid("parent_supertag_id").references(
    (): AnyPgColumn => knowledgeSupertags.id,
    { onDelete: "set null" },
  ),
  systemKey: text("system_key"),
  name: varchar("name", { length: 120 }).notNull(),
  baseType: varchar("base_type", { length: 40 }).default("note").notNull(),
  description: text("description"),
  icon: varchar("icon", { length: 64 }),
  color: varchar("color", { length: 32 }),
  templateJson: json("template_json").default({}).notNull(),
  pinnedFieldIds: json("pinned_field_ids").default([]).notNull(),
  configJson: json("config_json").default({}).notNull(),
  titleTemplate: text("title_template"),
  aiInstructions: text("ai_instructions"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_knowledge_supertags_workspace_system_key").on(table.workspaceId, table.systemKey),
]);

export const knowledgeNodeSupertags = pgTable(
  "knowledge_node_supertags",
  {
    nodeId: uuid("node_id")
      .references(() => knowledgeNodes.id, { onDelete: "cascade" })
      .notNull(),
    supertagId: uuid("supertag_id")
      .references(() => knowledgeSupertags.id, { onDelete: "cascade" })
      .notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
    createdBy: uuid("created_by").references(() => users.id),
  },
  (table) => [primaryKey({ columns: [table.nodeId, table.supertagId] })],
);

export const knowledgeFields = pgTable("knowledge_fields", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  supertagId: uuid("supertag_id")
    .references(() => knowledgeSupertags.id, { onDelete: "cascade" })
    .notNull(),
  systemKey: text("system_key"),
  name: varchar("name", { length: 120 }).notNull(),
  fieldType: varchar("field_type", { length: 40 }).default("text").notNull(),
  required: boolean("required").default(false).notNull(),
  optionsJson: json("options_json").default({}).notNull(),
  defaultValueJson: json("default_value_json"),
  sortOrder: doublePrecision("sort_order").default(0).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const knowledgeFieldValues = pgTable(
  "knowledge_field_values",
  {
    nodeId: uuid("node_id")
      .references(() => knowledgeNodes.id, { onDelete: "cascade" })
      .notNull(),
    fieldId: uuid("field_id")
      .references(() => knowledgeFields.id, { onDelete: "cascade" })
      .notNull(),
    valueJson: json("value_json"),
    valueText: text("value_text"),
    valueNumber: doublePrecision("value_number"),
    valueDatetime: timestamp("value_datetime"),
    targetNodeId: uuid("target_node_id").references(
      (): AnyPgColumn => knowledgeNodes.id,
      { onDelete: "set null" },
    ),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
    updatedBy: uuid("updated_by").references(() => users.id),
  },
  (table) => [primaryKey({ columns: [table.nodeId, table.fieldId] })],
);

export const knowledgeEdges = pgTable("knowledge_edges", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  sourceNodeId: uuid("source_node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  targetNodeId: uuid("target_node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  relationType: varchar("relation_type", { length: 80 }).default("related_to").notNull(),
  confidence: doublePrecision("confidence").default(1).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const knowledgeSearchIndex = pgTable("knowledge_search_index", {
  nodeId: uuid("node_id")
    .primaryKey()
    .references(() => knowledgeNodes.id, { onDelete: "cascade" }),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "set null",
  }),
  titleText: text("title_text").default("").notNull(),
  bodyTextPlain: text("body_text_plain").default("").notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const knowledgeSupertagFields = pgTable(
  "knowledge_supertag_fields",
  {
    supertagId: uuid("supertag_id")
      .references(() => knowledgeSupertags.id, { onDelete: "cascade" })
      .notNull(),
    fieldId: uuid("field_id")
      .references(() => knowledgeFields.id, { onDelete: "cascade" })
      .notNull(),
    sortOrder: doublePrecision("sort_order").default(0).notNull(),
    required: boolean("required").default(false).notNull(),
    showInTemplate: boolean("show_in_template").default(true).notNull(),
    optional: boolean("optional").default(false).notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [primaryKey({ columns: [table.supertagId, table.fieldId] })],
);

export const knowledgeNodePlacements = pgTable(
  "knowledge_node_placements",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    nodeId: uuid("node_id")
      .references(() => knowledgeNodes.id, { onDelete: "cascade" })
      .notNull(),
    parentNodeId: uuid("parent_node_id")
      .references((): AnyPgColumn => knowledgeNodes.id, { onDelete: "cascade" })
      .notNull(),
    sortOrder: doublePrecision("sort_order").default(0).notNull(),
    collapsed: boolean("collapsed").default(false).notNull(),
    createdBy: uuid("created_by").references(() => users.id),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    unique("uq_knowledge_node_placement_parent").on(table.nodeId, table.parentNodeId),
    index("ix_knowledge_node_placements_parent").on(table.parentNodeId, table.sortOrder),
  ],
);

export const knowledgeSavedViews = pgTable("knowledge_saved_views", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  supertagId: uuid("supertag_id").references(() => knowledgeSupertags.id, {
    onDelete: "set null",
  }),
  name: varchar("name", { length: 200 }).notNull(),
  layout: varchar("layout", { length: 40 }).default("table").notNull(),
  configJson: json("config_json").default({}).notNull(),
  sortOrder: doublePrecision("sort_order").default(0).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const knowledgeRevisions = pgTable("knowledge_revisions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  nodeId: uuid("node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  title: text("title").notNull(),
  bodyJson: json("body_json").default({}).notNull(),
  bodyText: text("body_text").default("").notNull(),
  changeSummary: text("change_summary"),
  sourceRefsJson: json("source_refs_json").default([]).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const knowledgeAiSuggestions = pgTable("knowledge_ai_suggestions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  nodeId: uuid("node_id").references(() => knowledgeNodes.id, {
    onDelete: "cascade",
  }),
  suggestionType: varchar("suggestion_type", { length: 80 }).notNull(),
  payloadJson: json("payload_json").default({}).notNull(),
  status: varchar("status", { length: 20 }).default("proposed").notNull(),
  confidence: doublePrecision("confidence"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const knowledgeAttachments = pgTable("knowledge_attachments", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  nodeId: uuid("node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  fileName: varchar("file_name", { length: 255 }).notNull(),
  filePath: text("file_path").notNull(),
  mimeType: varchar("mime_type", { length: 120 }),
  sizeBytes: integer("size_bytes"),
  attachmentMetadata: json("attachment_metadata").default({}).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const knowledgeImportJobs = pgTable("knowledge_import_jobs", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  workspaceId: uuid("workspace_id")
    .references(() => knowledgeWorkspaces.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "set null",
  }),
  sourceType: varchar("source_type", { length: 40 }).notNull(),
  sourceName: text("source_name").notNull(),
  status: varchar("status", { length: 20 }).default("proposed").notNull(),
  optionsJson: json("options_json").default({}).notNull(),
  summaryJson: json("summary_json").default({}).notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const knowledgeImportItems = pgTable("knowledge_import_items", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  jobId: uuid("job_id")
    .references(() => knowledgeImportJobs.id, { onDelete: "cascade" })
    .notNull(),
  nodeId: uuid("node_id").references(() => knowledgeNodes.id, {
    onDelete: "set null",
  }),
  sourceRef: text("source_ref").notNull(),
  title: text("title").notNull(),
  itemType: varchar("item_type", { length: 40 }).default("page").notNull(),
  status: varchar("status", { length: 20 }).default("proposed").notNull(),
  previewJson: json("preview_json").default({}).notNull(),
  errorMessage: text("error_message"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

// ─── ログイン履歴 ───

export const webuiLoginLogs = pgTable("webui_login_logs", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  username: varchar("username").notNull(),
  action: varchar("action").notNull(),
  ipAddress: varchar("ip_address"),
  userAgent: text("user_agent"),
  success: boolean("success"),
  failureReason: varchar("failure_reason"),
  sessionDurationSeconds: integer("session_duration_seconds"),
  createdAt: timestamp("created_at").defaultNow(),
  loginMetadata: json("login_metadata"),
});

// ─── 通知 ───

export const notificationDeliveries = pgTable("notification_deliveries", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  taskId: uuid("task_id"),
  occurrenceId: uuid("occurrence_id"),
  userId: uuid("user_id"),
  channel: varchar("channel").notNull(),
  notificationType: varchar("notification_type").notNull(),
  dedupeKey: varchar("dedupe_key").notNull(),
  title: varchar("title").notNull(),
  message: text("message").notNull(),
  scheduledFor: timestamp("scheduled_for").notNull(),
  deliveredAt: timestamp("delivered_at"),
  readAt: timestamp("read_at"),
  status: varchar("status").default("pending").notNull(),
  payload: json("payload"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});
