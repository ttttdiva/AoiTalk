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
  uniqueIndex,
  index,
  check,
  foreignKey,
  type AnyPgColumn,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";
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
  // ユーザー固有ストレージ内のアイコンファイルへの相対参照。
  // 画像本体は DB に保存せず、API が avatar_url に変換して返す。
  avatarPath: varchar("avatar_path", { length: 512 }),
  role: varchar("role").default("user").notNull(),
  isActive: boolean("is_active"),
  isPasswordResetRequired: boolean("is_password_reset_required"),
  sessionVersion: integer("session_version").notNull().default(1),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
  lastLogin: timestamp("last_login"),
  userSettings: json("user_settings"),
}, (table) => [
  check("ck_users_role_admin_user", sql`${table.role} in ('admin', 'user')`),
]);

// ─── ユーザー単位の HF / Hydrus 接続情報 ───
// encryptedPayload は field-crypto の ciphertext のみを保存する。平文の
// token/API key を Drizzle の insert payload に渡さないよう、サービス層で
// 暗号化してから書き込む契約とする。

export const userHfCredentials = pgTable("user_hf_credentials", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: uuid("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  encryptedPayload: text("encrypted_payload"),
  settingsJson: json("settings_json").default({}).notNull(),
  enabled: boolean("enabled").default(true).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_user_hf_credentials_user").on(table.userId),
  index("ix_user_hf_credentials_user").on(table.userId),
]);

export const userHydrusCredentials = pgTable("user_hydrus_credentials", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: uuid("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  encryptedPayload: text("encrypted_payload"),
  settingsJson: json("settings_json").default({}).notNull(),
  enabled: boolean("enabled").default(true).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_user_hydrus_credentials_user").on(table.userId),
  index("ix_user_hydrus_credentials_user").on(table.userId),
]);

export const enterpriseBootstrapState = pgTable("enterprise_bootstrap_state", {
  id: integer("id").primaryKey(),
  bootstrapUserId: uuid("bootstrap_user_id").references(() => users.id, {
    onDelete: "set null",
  }),
  completedAt: timestamp("completed_at"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  check("ck_enterprise_bootstrap_singleton", sql`${table.id} = 1`),
  index("ix_enterprise_bootstrap_user_id").on(table.bootstrapUserId),
]);

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
  storageQuotaMb: integer("storage_quota_mb").notNull().default(1000),
  storageUsedMb: doublePrecision("storage_used_mb").notNull().default(0),
  estimatedHours: doublePrecision("estimated_hours"),
  isCompleted: boolean("is_completed").default(false).notNull(),
  createdAt: timestamp("created_at"),
  updatedAt: timestamp("updated_at"),
  deletedAt: timestamp("deleted_at"),
  projectMetadata: json("project_metadata"),
  aliases: json("aliases").default([]),
});

// ─── 永続 Apps ───
// App 越境参照（別 App の Target / Release を指す行）は DB でも禁止する。
// app_targets / app_releases の (app_id, id) 複合一意キーを参照先にして、
// 参照側は単独 FK ではなく (app_id, 参照 id) の複合 FK で結ぶ。
// 実 DB 側の ON DELETE は PostgreSQL 15 以降の列指定付き SET NULL (列名) で、
// 複合 FK でも app_id は NULL 化しない（drizzle は列指定を表現できないため
// ここでは "set null" と書く）。スキーマの正本は Alembic（実 DB）。

export const apps = pgTable("apps", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  originProjectId: uuid("origin_project_id").references(() => projects.id, {
    onDelete: "set null",
  }),
  name: varchar("name", { length: 255 }).notNull(),
  slug: varchar("slug", { length: 120 }).notNull().unique(),
  description: text("description"),
  visibility: varchar("visibility", { length: 32 }).default("private").notNull(),
  defaultTargetKey: varchar("default_target_key", { length: 80 }),
  readmeNodeId: uuid("readme_node_id").references((): AnyPgColumn => knowledgeNodes.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  archivedAt: timestamp("archived_at"),
}, (table) => [
  check("ck_apps_visibility", sql`${table.visibility} in ('private','shared','public')`),
  index("ix_apps_origin_project").on(table.originProjectId),
]);

export const projectApps = pgTable("project_apps", {
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  bindingMode: varchar("binding_mode", { length: 20 }).default("development").notNull(),
  installedReleaseId: uuid("installed_release_id"),
  enabled: boolean("enabled").default(true).notNull(),
  pinned: boolean("pinned").default(false).notNull(),
  displayAlias: varchar("display_alias", { length: 255 }),
  configJson: json("config_json").default({}).notNull(),
  capabilityGrantsJson: json("capability_grants_json").default({}).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  primaryKey({ columns: [table.projectId, table.appId] }),
  check("ck_project_apps_binding_mode", sql`${table.bindingMode} in ('development','installed')`),
  // 別 App の Release を install 済みにできないよう複合 FK で縛る。
  foreignKey({
    name: "fk_project_apps_installed_release_app",
    columns: [table.appId, table.installedReleaseId],
    foreignColumns: [appReleases.appId, appReleases.id],
  }).onDelete("set null"),
  index("ix_project_apps_project_enabled").on(table.projectId, table.enabled),
]);

export const appGrants = pgTable("app_grants", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  userId: uuid("user_id").references(() => users.id, { onDelete: "cascade" }),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  permission: varchar("permission", { length: 20 }).default("viewer").notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
}, (table) => [
  check("ck_app_grants_exactly_one_subject", sql`((${table.userId} is null) <> (${table.projectId} is null))`),
  check("ck_app_grants_permission", sql`${table.permission} in ('viewer','runner','developer','maintainer','admin')`),
  // Grant は 1 App × 1 主体につき 1 行で、permission は上書き更新する。
  // UNIQUE は NULL を相異なる値として扱うので、主体側が NOT NULL の行だけを対象にする。
  uniqueIndex("uq_app_grants_app_user")
    .on(table.appId, table.userId)
    .where(sql`${table.userId} is not null`),
  uniqueIndex("uq_app_grants_app_project")
    .on(table.appId, table.projectId)
    .where(sql`${table.projectId} is not null`),
]);

export const appTargets = pgTable("app_targets", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  targetKey: varchar("target_key", { length: 80 }).notNull(),
  displayName: varchar("display_name", { length: 255 }).notNull(),
  surface: varchar("surface", { length: 32 }).notNull(),
  runtime: varchar("runtime", { length: 32 }).notNull(),
  executionHost: varchar("execution_host", { length: 32 }).notNull(),
  entrypoint: text("entrypoint").notNull(),
  manifestSnapshot: json("manifest_snapshot").default({}).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_app_targets_app_target_key").on(table.appId, table.targetKey),
  // 参照側から (app_id, target_id) の複合 FK を張るための一意キー。
  unique("uq_app_targets_app_id_id").on(table.appId, table.id),
  check("ck_app_targets_surface", sql`${table.surface} in ('embedded_web','standalone_web','desktop_gui','headless','office')`),
  check("ck_app_targets_runtime", sql`${table.runtime} in ('static_web','node','python','powershell','batch','vba','executable')`),
  check("ck_app_targets_execution_host", sql`${table.executionHost} in ('aoitalk','server','client','browser','office','download_only')`),
  index("ix_app_targets_app_surface").on(table.appId, table.surface),
]);

export const appReleases = pgTable("app_releases", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  version: varchar("version", { length: 80 }).notNull(),
  gitRevision: varchar("git_revision", { length: 80 }).notNull(),
  manifestHash: varchar("manifest_hash", { length: 64 }).notNull(),
  readmeHash: varchar("readme_hash", { length: 64 }).notNull(),
  changelog: text("changelog"),
  status: varchar("status", { length: 20 }).default("published").notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_app_releases_app_version").on(table.appId, table.version),
  // 参照側から (app_id, release_id) の複合 FK を張るための一意キー。
  unique("uq_app_releases_app_id_id").on(table.appId, table.id),
  check("ck_app_releases_status", sql`${table.status} in ('published','deprecated')`),
  index("ix_app_releases_app_status_created").on(table.appId, table.status, table.createdAt),
]);

export const appArtifacts = pgTable("app_artifacts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  // Release と Target が同じ App に属することを DB で保証するための非正規化列。
  // INSERT 時に省略すると BEFORE INSERT トリガー trg_app_artifacts_set_app_id が
  // release_id から補完する。
  appId: uuid("app_id").notNull(),
  releaseId: uuid("release_id").notNull(),
  targetId: uuid("target_id").notNull(),
  artifactType: varchar("artifact_type", { length: 32 }).notNull(),
  filePath: text("file_path").notNull(),
  filename: varchar("filename", { length: 255 }).notNull(),
  sha256: varchar("sha256", { length: 64 }).notNull(),
  sizeBytes: integer("size_bytes").default(0).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_app_artifacts_release_target_file").on(table.releaseId, table.targetId, table.artifactType, table.filename),
  foreignKey({
    name: "fk_app_artifacts_release_app",
    columns: [table.appId, table.releaseId],
    foreignColumns: [appReleases.appId, appReleases.id],
  }).onDelete("cascade"),
  foreignKey({
    name: "fk_app_artifacts_target_app",
    columns: [table.appId, table.targetId],
    foreignColumns: [appTargets.appId, appTargets.id],
  }).onDelete("restrict"),
  index("ix_app_artifacts_release_target").on(table.releaseId, table.targetId),
]);

export const appJobs = pgTable("app_jobs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  targetId: uuid("target_id"),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "set null" }),
  releaseId: uuid("release_id"),
  agentRunId: uuid("agent_run_id").references((): AnyPgColumn => agentRuns.id, { onDelete: "set null" }),
  jobType: varchar("job_type", { length: 20 }).notNull(),
  status: varchar("status", { length: 20 }).default("queued").notNull(),
  inputJson: json("input_json").default({}).notNull(),
  resultJson: json("result_json").default({}).notNull(),
  logPath: text("log_path"),
  exitCode: integer("exit_code"),
  startedBy: uuid("started_by").references(() => users.id, { onDelete: "set null" }),
  startedAt: timestamp("started_at").defaultNow().notNull(),
  endedAt: timestamp("ended_at"),
}, (table) => [
  check("ck_app_jobs_job_type", sql`${table.jobType} in ('build','test','run','package')`),
  check("ck_app_jobs_status", sql`${table.status} in ('queued','running','succeeded','failed','cancelled')`),
  // Target / Release は同じ App のものしか参照できない。
  foreignKey({
    name: "fk_app_jobs_target_app",
    columns: [table.appId, table.targetId],
    foreignColumns: [appTargets.appId, appTargets.id],
  }).onDelete("set null"),
  foreignKey({
    name: "fk_app_jobs_release_app",
    columns: [table.appId, table.releaseId],
    foreignColumns: [appReleases.appId, appReleases.id],
  }).onDelete("set null"),
  index("ix_app_jobs_app_status_started").on(table.appId, table.status, table.startedAt),
]);

export const taskAppLinks = pgTable("task_app_links", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  taskId: uuid("task_id")
    .references(() => tasks.id, { onDelete: "cascade" })
    .notNull(),
  appId: uuid("app_id")
    .references(() => apps.id, { onDelete: "cascade" })
    .notNull(),
  targetId: uuid("target_id"),
  relationType: varchar("relation_type", { length: 20 }).default("related").notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_task_app_links_target_relation").on(table.taskId, table.appId, table.targetId, table.relationType),
  uniqueIndex("uq_task_app_links_no_target")
    .on(table.taskId, table.appId, table.relationType)
    .where(sql`${table.targetId} is null`),
  check("ck_task_app_links_relation_type", sql`${table.relationType} in ('develops','fixes','tests','releases','uses','related')`),
  // 別 App の Target を指す Link を作れないようにする。
  foreignKey({
    name: "fk_task_app_links_target_app",
    columns: [table.appId, table.targetId],
    foreignColumns: [appTargets.appId, appTargets.id],
  }).onDelete("set null"),
  index("ix_task_app_links_task_relation").on(table.taskId, table.relationType),
  index("ix_task_app_links_app_relation").on(table.appId, table.relationType),
]);

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
  autoCloseOnDue: boolean("auto_close_on_due").default(false).notNull(),
  source: varchar("source").default("local").notNull(),
  createdBy: uuid("created_by"),
  completedAt: timestamp("completed_at", { mode: "string" }),
  archivedAt: timestamp("archived_at", { mode: "string" }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  deletedAt: timestamp("deleted_at", { mode: "string" }),
  deletionBatchId: uuid("deletion_batch_id"),
  taskMetadata: json("task_metadata"),
  estimatedHours: doublePrecision("estimated_hours"),
  sortOrder: doublePrecision("sort_order").default(0).notNull(),
  parentTaskId: uuid("parent_task_id").references((): AnyPgColumn => tasks.id, {
    onDelete: "cascade",
  }),
});

// ─── タスク・スケジュール ───
//
// Schedule は tasks.start_at/end_at と責務を共有しない。工程（phase）は
// project 単位の大きな期間 container、placement は task ごとの 2D 座標を
// 保持する。task の project 移動では placement をサービス層で明示的に解除
// するため、ここでは task_id の cascade だけを DB に委ねる。
export const projectSchedulePhases = pgTable(
  "project_schedule_phases",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    projectId: uuid("project_id")
      .references(() => projects.id, { onDelete: "cascade" })
      .notNull(),
    name: varchar("name", { length: 255 }).notNull(),
    startOn: date("start_on", { mode: "string" }).notNull(),
    endOn: date("end_on", { mode: "string" }).notNull(),
    sortOrder: doublePrecision("sort_order").notNull().default(0),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    check(
      "ck_project_schedule_phases_date_range",
      sql`${table.endOn} >= ${table.startOn}`,
    ),
    index("ix_project_schedule_phases_project_id").on(table.projectId),
    index("ix_project_schedule_phases_project_sort").on(
      table.projectId,
      table.sortOrder,
      table.startOn,
    ),
  ],
);

export const taskSchedulePlacements = pgTable(
  "task_schedule_placements",
  {
    taskId: uuid("task_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .primaryKey(),
    phaseId: uuid("phase_id").references(() => projectSchedulePhases.id, {
      onDelete: "set null",
    }),
    xRatio: doublePrecision("x_ratio").notNull().default(0),
    y: doublePrecision("y").notNull().default(0),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    check(
      "ck_task_schedule_placements_x_ratio",
      sql`${table.xRatio} = ${table.xRatio} and ${table.xRatio} >= 0 and ${table.xRatio} <= 1`,
    ),
    check(
      "ck_task_schedule_placements_y",
      sql`${table.y} = ${table.y} and ${table.y} >= -100000 and ${table.y} <= 100000`,
    ),
    index("ix_task_schedule_placements_phase_id").on(table.phaseId),
  ],
);

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

/** タスク同士の対称な関連。UUIDの小さい側をtask_a_idへ保存する。 */
export const taskRelations = pgTable(
  "task_relations",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    taskAId: uuid("task_a_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    taskBId: uuid("task_b_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    relationType: varchar("relation_type", { length: 32 })
      .default("related")
      .notNull(),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    check(
      "ck_task_relations_canonical_order",
      sql`${table.taskAId} < ${table.taskBId}`,
    ),
    unique("uq_task_relations_pair").on(
      table.taskAId,
      table.taskBId,
      table.relationType,
    ),
    index("ix_task_relations_task_a_id").on(table.taskAId),
    index("ix_task_relations_task_b_id").on(table.taskBId),
    index("ix_task_relations_created_by").on(table.createdBy),
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
  // 土日・祝日に当たった回の扱い: shift_forward / omit
  skipMode: varchar("skip_mode").default("shift_forward").notNull(),
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
  deletionBatchId: uuid("deletion_batch_id"),
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
  deletionBatchId: uuid("deletion_batch_id"),
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
  appId: uuid("app_id").references((): AnyPgColumn => apps.id, {
    onDelete: "set null",
  }),
  // Target は (app_id, app_target_id) の複合 FK で App に閉じ込める。
  appTargetId: uuid("app_target_id"),
  developmentStatus: varchar("development_status", { length: 32 }),
  lastReadAt: timestamp("last_read_at"),
  parentSessionId: uuid("parent_session_id").references(
    (): AnyPgColumn => conversationSessions.id,
    { onDelete: "set null" },
  ),
  forkedFromMessageId: uuid("forked_from_message_id").references(
    (): AnyPgColumn => conversationMessages.id,
    { onDelete: "set null" },
  ),
  isGroupChat: boolean("is_group_chat").default(false),
  groupCharacterNames: json("group_character_names"),
  rpSettings: json("rp_settings").default({}),
}, (table) => [
  check(
    "ck_conversation_sessions_development_status",
    sql`${table.developmentStatus} is null or ${table.developmentStatus} in ('working','waiting_for_user','completed')`,
  ),
  // 別 App の Target を指すチャットを作れないようにする。
  foreignKey({
    name: "fk_conversation_sessions_app_target_app",
    columns: [table.appId, table.appTargetId],
    foreignColumns: [appTargets.appId, appTargets.id],
  }).onDelete("set null"),
  // 複合 FK は MATCH SIMPLE のため app_id が NULL だと検査されない。
  check(
    "ck_conversation_sessions_app_target_requires_app",
    sql`${table.appTargetId} is null or ${table.appId} is not null`,
  ),
]);

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

export const agentRuns = pgTable("agent_runs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  rootRunId: uuid("root_run_id").references((): AnyPgColumn => agentRuns.id),
  parentRunId: uuid("parent_run_id").references((): AnyPgColumn => agentRuns.id),
  sessionId: uuid("session_id").references(() => conversationSessions.id, {
    onDelete: "set null",
  }),
  triggerMessageId: uuid("trigger_message_id").references(() => conversationMessages.id, {
    onDelete: "set null",
  }),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "set null" }),
  userId: varchar("user_id", { length: 200 }),
  clientMessageId: varchar("client_message_id", { length: 512 }),
  clientMessageKey: varchar("client_message_key", { length: 64 }),
  requestFingerprint: varchar("request_fingerprint", { length: 64 }),
  runType: varchar("run_type", { length: 64 }).default("chat_turn").notNull(),
  status: varchar("status", { length: 32 }).default("queued").notNull(),
  title: varchar("title", { length: 255 }).default("").notNull(),
  objective: text("objective").default("").notNull(),
  generationProfile: varchar("generation_profile", { length: 64 }),
  provider: varchar("provider", { length: 80 }),
  model: varchar("model", { length: 160 }),
  error: text("error"),
  result: json("result").default({}),
  validation: json("validation").default({}),
  runMetadata: json("run_metadata").default({}),
  appId: uuid("app_id").references((): AnyPgColumn => apps.id, { onDelete: "set null" }),
  // Target は (app_id, app_target_id) の複合 FK で App に閉じ込める。
  appTargetId: uuid("app_target_id"),
  baseRevision: varchar("base_revision", { length: 80 }),
  resultRevision: varchar("result_revision", { length: 80 }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
  startedAt: timestamp("started_at"),
  endedAt: timestamp("ended_at"),
  lastEventAt: timestamp("last_event_at"),
}, (table) => [
  index("ix_agent_runs_app_status_created").on(table.appId, table.status, table.createdAt),
  index("ix_agent_runs_session_status_created").on(table.sessionId, table.status, table.createdAt),
  // 別 App の Target を指す Run を作れないようにする。
  foreignKey({
    name: "fk_agent_runs_app_target_app",
    columns: [table.appId, table.appTargetId],
    foreignColumns: [appTargets.appId, appTargets.id],
  }).onDelete("set null"),
  // 複合 FK は MATCH SIMPLE のため app_id が NULL だと検査されない。
  check(
    "ck_agent_runs_app_target_requires_app",
    sql`${table.appTargetId} is null or ${table.appId} is not null`,
  ),
]);

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

export const taskDependencies = pgTable(
  "task_dependencies",
  {
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
  },
  (table) => [
    unique("unique_task_dependency").on(
      table.taskId,
      table.dependsOnTaskId,
    ),
    index("ix_task_dependencies_task_id").on(table.taskId),
    index("ix_task_dependencies_depends_on_task_id").on(
      table.dependsOnTaskId,
    ),
  ],
);

// ─── コンテンツ削除ライフサイクル監査 ───
//
// This table deliberately has no foreign keys.  The event ledger must survive
// the eventual physical purge of the content row it describes.
export const contentDeletionEvents = pgTable(
  "content_deletion_events",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    batchId: uuid("batch_id").notNull(),
    entityType: varchar("entity_type", { length: 32 }).notNull(),
    entityId: varchar("entity_id", { length: 512 }).notNull(),
    rootEntityId: varchar("root_entity_id", { length: 512 }),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "set null",
    }),
    actorUserId: uuid("actor_user_id").references(() => users.id, {
      onDelete: "set null",
    }),
    action: varchar("action", { length: 32 }).notNull(),
    displayName: varchar("display_name", { length: 255 }),
    source: varchar("source", { length: 64 }),
    eventAt: timestamp("event_at").defaultNow().notNull(),
    metadata: jsonb("metadata").default({}).notNull(),
  },
  (table) => [
    index("ix_content_deletion_events_entity").on(table.entityType, table.entityId),
    index("ix_content_deletion_events_root_event_at").on(
      table.rootEntityId,
      table.eventAt,
    ),
    index("ix_content_deletion_events_batch_id").on(table.batchId),
    index("ix_content_deletion_events_project_id").on(table.projectId),
    index("ix_content_deletion_events_actor_user_id").on(table.actorUserId),
    index("ix_content_deletion_events_event_at").on(table.eventAt),
  ],
);

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

// Docs Library is the canonical storage scope for the Docs domain.  Filer
// tables are deliberately outside this rename.  A small alias helper below
// keeps old server/mobile imports source-compatible without creating a SQL
// compatibility view or a duplicate physical column.
function withLegacyWorkspaceTypeAlias<T extends { libraryType: unknown }>(table: T) {
  type Select = T extends { $inferSelect: infer S } ? S : never;
  type Insert = T extends { $inferInsert: infer I } ? I : never;
  type WithLegacy = T & {
    workspaceType: T["libraryType"];
    $inferSelect: Select & { workspaceType?: Select extends { libraryType: infer V } ? V : never };
    $inferInsert: Insert | (Omit<Insert, "libraryType"> & { workspaceType: Select extends { libraryType: infer V } ? V : never });
  };
  (table as WithLegacy).workspaceType = table.libraryType;
  return table as WithLegacy;
}

function withLegacyWorkspaceIdAlias<T extends { docsLibraryId: unknown }>(table: T) {
  type Select = T extends { $inferSelect: infer S } ? S : never;
  type Insert = T extends { $inferInsert: infer I } ? I : never;
  type WithLegacy = T & {
    workspaceId: T["docsLibraryId"];
    $inferSelect: Select & { workspaceId?: Select extends { docsLibraryId: infer V } ? V : never };
    $inferInsert: Insert | (Omit<Insert, "docsLibraryId"> & { workspaceId: Select extends { docsLibraryId: infer V } ? V : never });
  };
  (table as WithLegacy).workspaceId = table.docsLibraryId;
  return table as WithLegacy;
}

export const docsLibraries = pgTable("docs_libraries", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  name: varchar("name", { length: 200 }).notNull(),
  description: text("description"),
  ownerUserId: uuid("owner_user_id").references(() => users.id, {
    onDelete: "set null",
  }),
  libraryType: varchar("library_type", { length: 32 }).default("personal").notNull(),
  settingsJson: json("settings_json").default({}).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  check(
    "ck_docs_libraries_library_type_not_project",
    sql`${table.libraryType} <> 'project'`,
  ),
  uniqueIndex("uq_docs_libraries_personal_owner")
    .on(table.ownerUserId)
    .where(sql`${table.libraryType} = 'personal' and ${table.ownerUserId} is not null`),
]);

/** Source-level legacy alias; both names query the physical docs_libraries table. */
export const knowledgeWorkspaces = withLegacyWorkspaceTypeAlias(docsLibraries);
export type DocsLibrary = typeof docsLibraries.$inferSelect;
export type NewDocsLibrary = typeof docsLibraries.$inferInsert;

export const knowledgeNodeShares = pgTable("knowledge_node_shares", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  nodeId: uuid("node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  userId: uuid("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  permission: varchar("permission", { length: 16 }).default("read").notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_knowledge_node_shares_node_user").on(table.nodeId, table.userId),
  check(
    "ck_knowledge_node_shares_permission",
    sql`${table.permission} in ('read', 'write')`,
  ),
  index("ix_knowledge_node_shares_node").on(table.nodeId),
  index("ix_knowledge_node_shares_user").on(table.userId),
]);

export const knowledgeNodes = withLegacyWorkspaceIdAlias(pgTable("knowledge_nodes", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
  appId: uuid("app_id").references((): AnyPgColumn => apps.id, {
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
  unique("uq_knowledge_nodes_docs_library_system_key").on(table.docsLibraryId, table.systemKey),
  index("ix_knowledge_nodes_docs_library").on(table.docsLibraryId),
  index("ix_knowledge_nodes_docs_library_parent_sort").on(table.docsLibraryId, table.parentId, table.sortOrder),
  index("ix_knowledge_nodes_docs_library_project").on(table.docsLibraryId, table.projectId),
  index("ix_knowledge_nodes_root_page").on(table.rootPageId),
  index("ix_knowledge_nodes_archived_at").on(table.archivedAt),
]));

export const projectKnowledgeRefs = pgTable("project_knowledge_refs", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  projectId: uuid("project_id")
    .references(() => projects.id, { onDelete: "cascade" })
    .notNull(),
  knowledgeNodeId: uuid("knowledge_node_id")
    .references(() => knowledgeNodes.id, { onDelete: "cascade" })
    .notNull(),
  relationType: varchar("relation_type", { length: 32 })
    .default("related")
    .notNull(),
  priority: integer("priority").default(100).notNull(),
  createdBy: uuid("created_by")
    .references(() => users.id)
    .notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}, (table) => [
  unique("uq_project_knowledge_refs_project_node").on(
    table.projectId,
    table.knowledgeNodeId,
  ),
  index("ix_project_knowledge_refs_project_priority").on(
    table.projectId,
    table.priority,
  ),
  index("ix_project_knowledge_refs_node").on(table.knowledgeNodeId),
]);

export type ProjectKnowledgeRef = typeof projectKnowledgeRefs.$inferSelect;
export type NewProjectKnowledgeRef = typeof projectKnowledgeRefs.$inferInsert;

export const knowledgeSupertags = withLegacyWorkspaceIdAlias(pgTable("knowledge_supertags", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
  unique("uq_knowledge_supertags_docs_library_system_key").on(table.docsLibraryId, table.systemKey),
]));

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

export const knowledgeFields = withLegacyWorkspaceIdAlias(pgTable("knowledge_fields", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
}));

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

export const knowledgeSearchIndex = withLegacyWorkspaceIdAlias(pgTable("knowledge_search_index", {
  nodeId: uuid("node_id")
    .primaryKey()
    .references(() => knowledgeNodes.id, { onDelete: "cascade" }),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "set null",
  }),
  titleText: text("title_text").default("").notNull(),
  bodyTextPlain: text("body_text_plain").default("").notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
}));

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

export const knowledgeSavedViews = withLegacyWorkspaceIdAlias(pgTable("knowledge_saved_views", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
}));

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

export const knowledgeAiSuggestions = withLegacyWorkspaceIdAlias(pgTable("knowledge_ai_suggestions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
}));

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

export const knowledgeImportJobs = withLegacyWorkspaceIdAlias(pgTable("knowledge_import_jobs", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  docsLibraryId: uuid("docs_library_id")
    .references(() => docsLibraries.id, { onDelete: "cascade" })
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
}));

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

export const webPushSubscriptions = pgTable("web_push_subscriptions", {
  id: uuid("id")
    .primaryKey()
    .$defaultFn(() => crypto.randomUUID()),
  userId: uuid("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  endpoint: text("endpoint").notNull().unique(),
  p256dh: text("p256dh").notNull(),
  auth: text("auth").notNull(),
  expirationTime: timestamp("expiration_time"),
  contentEncoding: varchar("content_encoding").default("aes128gcm").notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});
