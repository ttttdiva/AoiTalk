import {
  pgTable,
  uuid,
  varchar,
  text,
  boolean,
  timestamp,
  json,
  integer,
  doublePrecision,
  primaryKey,
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
  allowJoinRequests: boolean("allow_join_requests"),
  storageQuotaMb: integer("storage_quota_mb"),
  storageUsedMb: doublePrecision("storage_used_mb"),
  estimatedHours: doublePrecision("estimated_hours"),
  isCompleted: boolean("is_completed").default(false),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
  deletedAt: timestamp("deleted_at"),
  projectMetadata: json("project_metadata"),
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

export const projectInfoCategories = pgTable("project_info_categories", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  key: varchar("category_key", { length: 120 }).notNull(),
  label: varchar("label", { length: 200 }).notNull(),
  description: text("description"),
  status: varchar("status", { length: 32 }).default("active"),
  source: varchar("source", { length: 32 }).default("template"),
  sortOrder: doublePrecision("sort_order").default(0),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const projectDocuments = pgTable("project_documents", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  categoryId: uuid("category_id").references(() => projectInfoCategories.id, {
    onDelete: "set null",
  }),
  title: varchar("title", { length: 255 }).notNull(),
  description: text("description"),
  documentType: varchar("document_type", { length: 64 }).default("document"),
  targetKind: varchar("target_kind", { length: 32 }).default("file"),
  filePath: text("file_path"),
  recordTableId: uuid("record_table_id"),
  externalUrl: text("external_url"),
  role: varchar("role", { length: 64 }).default("reference"),
  isPrimary: boolean("is_primary").default(false),
  aiAccessLevel: varchar("ai_access_level", { length: 32 }).default("metadata"),
  status: varchar("status", { length: 32 }).default("active"),
  notes: text("notes"),
  sourceType: varchar("source_type", { length: 32 }).default("manual"),
  sourceRef: text("source_ref"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
  deletedAt: timestamp("deleted_at"),
});

export const projectFacts = pgTable("project_facts", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  categoryId: uuid("category_id").references(() => projectInfoCategories.id, {
    onDelete: "set null",
  }),
  sourceDocumentId: uuid("source_document_id").references(() => projectDocuments.id, {
    onDelete: "set null",
  }),
  sourceTaskId: uuid("source_task_id"),
  title: varchar("title", { length: 255 }).notNull(),
  content: text("content").notNull(),
  factType: varchar("fact_type", { length: 64 }).default("fact"),
  confidence: doublePrecision("confidence").default(1),
  importance: integer("importance").default(5),
  status: varchar("status", { length: 32 }).default("active"),
  sourceType: varchar("source_type", { length: 32 }).default("manual"),
  sourceRef: text("source_ref"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
  deletedAt: timestamp("deleted_at"),
});

export const projectInfoSyncStates = pgTable("project_info_sync_states", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  sourceType: varchar("source_type", { length: 64 }).default("tasks"),
  lastSyncedAt: timestamp("last_synced_at"),
  lastSeenUpdatedAt: timestamp("last_seen_updated_at"),
  cursor: json("cursor"),
  syncMetadata: json("sync_metadata"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
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
  title: varchar("title").notNull(),
  description: text("description"),
  status: varchar("status").default("todo"),
  priority: varchar("priority").default("medium"),
  startAt: timestamp("start_at", { mode: "string" }),
  endAt: timestamp("end_at", { mode: "string" }),
  allDay: boolean("all_day").default(false),
  reminderOffsets: json("reminder_offsets"),
  notificationsEnabled: boolean("notifications_enabled").default(true),
  source: varchar("source").default("local"),
  createdBy: uuid("created_by"),
  completedAt: timestamp("completed_at", { mode: "string" }),
  archivedAt: timestamp("archived_at", { mode: "string" }),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
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
  isPrimary: boolean("is_primary").default(false),
  assignedAt: timestamp("assigned_at").defaultNow(),
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
  createdAt: timestamp("created_at").defaultNow(),
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
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
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
  sizeBytes: integer("size_bytes").default(0),
  kind: varchar("kind").default("file"),
  createdBy: uuid("created_by").references(() => users.id),
  createdAt: timestamp("created_at").defaultNow(),
  attachmentMetadata: json("metadata").default({}),
});

export const taskRecurrenceRules = pgTable("task_recurrence_rules", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .unique()
    .notNull(),
  rrule: text("rrule").notNull(),
  timezone: varchar("timezone").default(DEFAULT_TASK_TIMEZONE),
  horizonDays: integer("horizon_days").default(90),
  triggerStatus: varchar("trigger_status").default("closed"),
  createNew: boolean("create_new").default(false),
  recurForever: boolean("recur_forever").default(true),
  resetStatusTo: varchar("reset_status_to").default("open"),
  endCount: integer("end_count"),
  endDate: timestamp("end_date", { mode: "string" }),
  skipWeekend: boolean("skip_weekend").default(false).notNull(),
  skipHoliday: boolean("skip_holiday").default(false).notNull(),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
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
  status: varchar("status").default("todo"),
  allDay: boolean("all_day").default(false),
  reminderOffsets: json("reminder_offsets"),
  sourceKind: varchar("source_kind").default("task_schedule"),
  isGenerated: boolean("is_generated").default(false),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
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
  source: varchar("source").default("manual"),
  note: text("note"),
  createdAt: timestamp("created_at", { mode: "string" }).defaultNow(),
  updatedAt: timestamp("updated_at", { mode: "string" }).defaultNow(),
  entryMetadata: json("entry_metadata"),
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
  createdAt: timestamp("created_at").defaultNow(),
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
  createdAt: timestamp("created_at").defaultNow(),
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
  feedbackMetadata: json("feedback_metadata"),
});

// ─── Knowledge Workspace ───

export const knowledgeSources = pgTable("knowledge_sources", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name", { length: 200 }).notNull(),
  description: text("description"),
  rootPath: text("root_path").notNull(),
  sourceType: varchar("source_type", { length: 40 }).default("local_dir"),
  ownerUserId: uuid("owner_user_id").references(() => users.id),
  accessPolicy: json("access_policy"),
  includePatterns: json("include_patterns"),
  excludePatterns: json("exclude_patterns"),
  syncMode: varchar("sync_mode", { length: 20 }).default("manual"),
  writePolicy: varchar("write_policy", { length: 40 }).default("propose_patch"),
  status: varchar("status", { length: 20 }),
  documentCount: integer("document_count"),
  chunkCount: integer("chunk_count"),
  lastSyncedAt: timestamp("last_synced_at"),
  errorMessage: text("error_message"),
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
  permission: varchar("permission", { length: 20 }).default("read"),
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
  linkType: varchar("link_type", { length: 20 }),
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
  source: varchar("source", { length: 20 }),
  status: varchar("status", { length: 20 }),
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
  status: varchar("status", { length: 20 }),
  preHash: varchar("pre_hash", { length: 64 }),
  postHash: varchar("post_hash", { length: 64 }),
  createdAt: timestamp("created_at").defaultNow(),
  appliedAt: timestamp("applied_at"),
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
  status: varchar("status").default("pending"),
  payload: json("payload"),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});
