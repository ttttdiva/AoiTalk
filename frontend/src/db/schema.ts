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
  // AD is the credential authority; only local users carry a bcrypt hash.
  passwordHash: varchar("password_hash"),
  authSource: varchar("auth_source", { length: 16 }).default("local").notNull(),
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
  check("ck_users_auth_source", sql`${table.authSource} in ('local', 'ad')`),
  check(
    "ck_users_auth_source_password_hash",
    sql`(${table.authSource} = 'local' and ${table.passwordHash} is not null) or (${table.authSource} = 'ad' and ${table.passwordHash} is null)`,
  ),
  check(
    "ck_users_ad_password_reset_disabled",
    sql`${table.authSource} = 'local' or ${table.isPasswordResetRequired} is false`,
  ),
]);

/** Immutable AD objectGUID → local user ownership binding. */
export const externalIdentityBindings = pgTable(
  "external_identity_bindings",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    userId: uuid("user_id")
      .references(() => users.id, { onDelete: "restrict" })
      .notNull(),
    source: varchar("source", { length: 16 }).default("ad").notNull(),
    authority: varchar("authority", { length: 255 }).notNull(),
    // AD objectGUID represented as UUID; DNs and mutable usernames are not stored.
    externalId: uuid("external_id").notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    check("ck_external_identity_bindings_source_ad", sql`${table.source} = 'ad'`),
    check(
      "ck_external_identity_bindings_authority_length",
      sql`length(${table.authority}) between 1 and 255`,
    ),
    unique("uq_external_identity_bindings_source_authority_external").on(
      table.source,
      table.authority,
      table.externalId,
    ),
    unique("uq_external_identity_bindings_user_source").on(
      table.userId,
      table.source,
    ),
    index("ix_external_identity_bindings_user_id").on(table.userId),
    index("ix_external_identity_bindings_source_authority_external").on(
      table.source,
      table.authority,
      table.externalId,
    ),
  ],
);

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
  // Provenance is intentionally independent from the legacy
  // ``created_by_agent`` flag.  Accepted/manual rows must remain protected
  // even when an old agent writer set that boolean.
  origin: varchar("origin", { length: 32 }).default("manual").notNull(),
  createdBy: uuid("created_by").references(() => users.id),
  updatedBy: uuid("updated_by").references(() => users.id),
  createdByAgent: boolean("created_by_agent").default(false).notNull(),
  version: integer("version").default(1).notNull(),
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

export const taskOccurrences = pgTable(
  "task_occurrences",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    taskId: uuid("task_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    startAt: timestamp("start_at", { mode: "string" }).notNull(),
    endAt: timestamp("end_at", { mode: "string" }).notNull(),
    // Canonical RRULE boundary; legacy rows may leave this nullable.
    originalStartAt: timestamp("original_start_at", { mode: "string" }),
    status: varchar("status").default("todo").notNull(),
    allDay: boolean("all_day").default(false).notNull(),
    reminderOffsets: json("reminder_offsets"),
    sourceKind: varchar("source_kind").default("task_schedule").notNull(),
    isGenerated: boolean("is_generated").default(false).notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
    deletedAt: timestamp("deleted_at", { mode: "string" }),
    deletionBatchId: uuid("deletion_batch_id"),
  },
  (table) => [
    unique("uq_task_occurrence_canonical_source").on(
      table.taskId,
      table.originalStartAt,
      table.sourceKind,
    ),
  ],
);

/** Effective-from changes to a recurring task's canonical schedule. */
export const taskRecurrenceScheduleSegments = pgTable(
  "task_recurrence_schedule_segments",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    taskId: uuid("task_id")
      .references(() => tasks.id, { onDelete: "cascade" })
      .notNull(),
    effectiveFrom: timestamp("effective_from", { mode: "string" }).notNull(),
    startOffsetSeconds: integer("start_offset_seconds").notNull().default(0),
    endOffsetSeconds: integer("end_offset_seconds").notNull().default(0),
    allDay: boolean("all_day").notNull().default(false),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    unique("uq_task_recurrence_schedule_segments_task_boundary").on(
      table.taskId,
      table.effectiveFrom,
    ),
    index("ix_task_recurrence_schedule_segments_task_effective").on(
      table.taskId,
      table.effectiveFrom,
    ),
  ],
);

export type TaskRecurrenceScheduleSegment =
  typeof taskRecurrenceScheduleSegments.$inferSelect;
export type NewTaskRecurrenceScheduleSegment =
  typeof taskRecurrenceScheduleSegments.$inferInsert;

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
  // Stable client supplied identity used to make retries idempotent. Keep
  // this nullable so legacy writers and assistant rows without a client id
  // remain valid; the partial unique index below scopes uniqueness per
  // conversation without treating NULL as a shared value.
  clientMessageId: varchar("client_message_id", { length: 512 }),
}, (table) => [
  uniqueIndex("uq_conversation_messages_session_client_message_id")
    .on(table.sessionId, table.clientMessageId)
    .where(sql`${table.clientMessageId} is not null`),
]);

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

// ─── Resolution Knowledge Capture ───
// These tables are owned by the FastAPI migrations as well.  The WebUI only
// needs the narrow write/read surface used by transaction-scoped task writers.
export const projectKnowledgeCaptureSettings = pgTable(
  "project_knowledge_capture_settings",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    projectId: uuid("project_id")
      .references(() => projects.id, { onDelete: "cascade" })
      .notNull(),
    mode: varchar("mode", { length: 16 }).default("suggest").notNull(),
    version: integer("version").default(1).notNull(),
    updatedBy: uuid("updated_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    uniqueIndex("uq_project_knowledge_capture_settings_project").on(
      table.projectId,
    ),
  ],
);

export const knowledgeCaptureCandidates = pgTable(
  "knowledge_capture_candidates",
  {
    id: uuid("id")
      .primaryKey()
      .$defaultFn(() => crypto.randomUUID()),
    projectId: uuid("project_id")
      .references(() => projects.id, { onDelete: "cascade" })
      .notNull(),
    seedTaskId: uuid("seed_task_id").references(() => tasks.id, {
      onDelete: "set null",
    }),
    taskActivityId: uuid("task_activity_id").references(() => taskActivities.id, {
      onDelete: "set null",
    }),
    triggerUserId: uuid("trigger_user_id").references(() => users.id, {
      onDelete: "set null",
    }),
    completionFingerprint: varchar("completion_fingerprint", { length: 64 })
      .notNull()
      .unique(),
    terminalStatus: varchar("terminal_status", { length: 32 })
      .default("closed")
      .notNull(),
    status: varchar("status", { length: 32 }).default("queued").notNull(),
    modeSnapshot: varchar("mode_snapshot", { length: 16 })
      .default("suggest")
      .notNull(),
    version: integer("version").default(1).notNull(),
    evidenceRefs: json("evidence_refs").default([]).notNull(),
    attemptCount: integer("attempt_count").default(0).notNull(),
    maxAttempts: integer("max_attempts").default(5).notNull(),
    completedAt: timestamp("completed_at"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
);

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
      metadata: json("metadata").default({}).notNull(),
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
  // A first-class, explicitly-created empty paragraph.  This discriminator
  // is deliberately kept outside body_json because body_json is encrypted
  // at rest and cannot be inspected by visibility SQL predicates.
  isExplicitBlank: boolean("is_explicit_blank").notNull().default(false),
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
  index("ix_knowledge_nodes_is_explicit_blank").on(table.isExplicitBlank),
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

// ─── Trusted Engagement Operations ───
// These tables mirror the Alembic operations-kernel schema.  Credentials,
// source text and artifact storage paths are intentionally server-side fields;
// the API's safe projections never expose them to the browser.

export const externalConnections = pgTable("external_connections", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  providerKey: varchar("provider_key", { length: 120 }).notNull(),
  displayName: varchar("display_name", { length: 255 }).notNull(),
  remoteAccountRef: varchar("remote_account_ref", { length: 255 }),
  credentialRef: text("credential_ref"),
  authStatus: varchar("auth_status", { length: 32 }).default("unknown").notNull(),
  version: integer("version").default(1).notNull(),
  metadataJson: json("metadata").default({}).notNull(),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check("ck_external_connections_version_positive", sql`${table.version} > 0`),
  unique("uq_external_connections_account").on(
    table.ownerUserId,
    table.projectId,
    table.providerKey,
    table.remoteAccountRef,
  ),
  index("ix_external_connections_owner_project").on(table.ownerUserId, table.projectId),
]);

export const artifactVersions = pgTable("artifact_versions", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  filename: varchar("filename", { length: 512 }),
  sha256: varchar("sha256", { length: 64 }).notNull(),
  sizeBytes: integer("size_bytes").notNull(),
  mimeType: varchar("mime_type", { length: 255 }).notNull(),
  storageRef: text("storage_ref"),
  provenanceJson: json("provenance").default({}).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_artifact_versions_size_nonnegative", sql`${table.sizeBytes} >= 0`),
  check("ck_artifact_versions_sha256_length", sql`length(${table.sha256}) = 64`),
  uniqueIndex("uq_artifact_versions_personal_content")
    .on(table.ownerUserId, table.sha256, table.sizeBytes, table.mimeType)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_artifact_versions_project_content")
    .on(table.ownerUserId, table.projectId, table.sha256, table.sizeBytes, table.mimeType)
    .where(sql`${table.projectId} is not null`),
  index("ix_artifact_versions_owner_project").on(table.ownerUserId, table.projectId),
]);

export const opportunities = pgTable("opportunities", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  connectionId: uuid("connection_id").references(() => externalConnections.id, {
    onDelete: "set null",
  }),
  title: varchar("title", { length: 500 }).notNull(),
  sourceUrl: text("source_url"),
  sourceText: text("source_text"),
  sourceSnapshotHash: varchar("source_snapshot_hash", { length: 64 }),
  sourceSnapshotJson: json("source_snapshot").default({}).notNull(),
  status: varchar("status", { length: 32 }).default("open").notNull(),
  metadataJson: json("metadata").default({}).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check(
    "ck_opportunities_snapshot_hash_length",
    sql`length(${table.sourceSnapshotHash}) = 64 or ${table.sourceSnapshotHash} is null`,
  ),
  index("ix_opportunities_owner_project").on(table.ownerUserId, table.projectId),
]);

export const opportunityEvaluations = pgTable("opportunity_evaluations", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  opportunityId: uuid("opportunity_id")
    .references(() => opportunities.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  version: integer("version").notNull(),
  estimatedEffortHours: doublePrecision("estimated_effort_hours"),
  estimatedCost: doublePrecision("estimated_cost"),
  estimatedRevenue: doublePrecision("estimated_revenue"),
  fit: varchar("fit", { length: 32 }),
  risks: json("risks").default([]).notNull(),
  missingRequirements: json("missing_requirements").default([]).notNull(),
  summary: text("summary"),
  evidenceRefs: json("evidence_refs").default([]).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_opportunity_evaluations_version_positive", sql`${table.version} > 0`),
  unique("uq_opportunity_evaluations_version").on(table.opportunityId, table.version),
]);

export const applicationDrafts = pgTable("application_drafts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  opportunityId: uuid("opportunity_id")
    .references(() => opportunities.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  version: integer("version").notNull(),
  message: text("message").notNull(),
  offeredPrice: doublePrecision("offered_price"),
  currency: varchar("currency", { length: 16 }),
  deliveryEstimate: varchar("delivery_estimate", { length: 255 }),
  artifactVersionIds: json("artifact_version_ids").default([]).notNull(),
  draftHash: varchar("draft_hash", { length: 64 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_application_drafts_version_positive", sql`${table.version} > 0`),
  check("ck_application_drafts_hash_length", sql`length(${table.draftHash}) = 64`),
  unique("uq_application_drafts_version").on(table.opportunityId, table.version),
]);

export const externalActions = pgTable("external_actions", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  opportunityId: uuid("opportunity_id")
    .references(() => opportunities.id, { onDelete: "cascade" }),
  // MediaOps actions reuse this ledger without requiring EngagementOps
  // opportunity/draft bindings.  These opaque UUIDs are validated by the
  // service layer so the optional MediaOps tables remain decoupled here.
  contentItemId: uuid("content_item_id"),
  contentVariantId: uuid("content_variant_id"),
  contentVariantRevisionId: uuid("content_variant_revision_id"),
  personaRevisionId: uuid("persona_revision_id"),
  platformAccountId: uuid("platform_account_id"),
  platformAccountRevisionId: uuid("platform_account_revision_id"),
  platform: varchar("platform", { length: 16 }),
  sourceUrl: text("source_url"),
  sourceSnapshotHash: varchar("source_snapshot_hash", { length: 64 }),
  connectionId: uuid("connection_id")
    .references(() => externalConnections.id, { onDelete: "restrict" })
    .notNull(),
  applicationDraftId: uuid("application_draft_id")
    .references(() => applicationDrafts.id, { onDelete: "restrict" }),
  applicationDraftVersion: integer("application_draft_version").default(1).notNull(),
  actionType: varchar("action_type", { length: 64 })
    .default("engagement.submit_application")
    .notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  payloadJson: json("payload").default({}).notNull(),
  payloadHash: varchar("payload_hash", { length: 64 }).notNull(),
  artifactHashes: json("artifact_hashes").default([]).notNull(),
  actionVersion: integer("action_version").default(1).notNull(),
  version: integer("version").default(1).notNull(),
  status: varchar("status", { length: 32 }).default("proposed").notNull(),
  executionMode: varchar("execution_mode", { length: 16 }).default("manual").notNull(),
  capabilitySnapshotId: uuid("capability_snapshot_id"),
  capabilitySnapshotHash: varchar("capability_snapshot_hash", { length: 64 }),
  adapterKey: varchar("adapter_key", { length: 128 }),
  adapterVersion: varchar("adapter_version", { length: 32 }),
  credentialStateHash: varchar("credential_state_hash", { length: 64 }),
  executionKey: varchar("execution_key", { length: 255 }),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check(
    "ck_external_actions_type",
    sql`${table.actionType} in ('engagement.submit_application', 'media.publish_content', 'media.update_content', 'media.delete_content', 'media.release_product')`,
  ),
  check("ck_external_actions_execution_mode", sql`${table.executionMode} in ('manual', 'provider')`),
  check(
    "ck_external_actions_provider_evidence",
    sql`${table.executionMode} <> 'provider' or (${table.capabilitySnapshotId} is not null and length(${table.capabilitySnapshotHash}) = 64 and length(trim(${table.adapterKey})) > 0 and length(trim(${table.adapterVersion})) > 0 and length(${table.credentialStateHash}) = 64 and length(trim(${table.executionKey})) > 0)`,
  ),
  check("ck_external_actions_capability_snapshot_hash", sql`${table.capabilitySnapshotHash} is null or length(${table.capabilitySnapshotHash}) = 64`),
  check("ck_external_actions_credential_state_hash", sql`${table.credentialStateHash} is null or length(${table.credentialStateHash}) = 64`),
  check("ck_external_actions_payload_hash_length", sql`length(${table.payloadHash}) = 64`),
  check(
    "ck_external_actions_source_hash_length",
    sql`length(${table.sourceSnapshotHash}) = 64 or ${table.sourceSnapshotHash} is null`,
  ),
  check(
    "ck_external_actions_versions_positive",
    sql`${table.actionVersion} > 0 and ${table.version} > 0 and ${table.applicationDraftVersion} > 0`,
  ),
  uniqueIndex("uq_external_actions_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_external_actions_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
  index("ix_external_actions_owner_project").on(table.ownerUserId, table.projectId),
]);

export const externalActionApprovals = pgTable("external_action_approvals", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  actionId: uuid("action_id")
    .references(() => externalActions.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  actionVersion: integer("action_version").notNull(),
  payloadHash: varchar("payload_hash", { length: 64 }).notNull(),
  artifactHashes: json("artifact_hashes").default([]).notNull(),
  decision: varchar("decision", { length: 16 }).notNull(),
  reason: text("reason"),
  decidedBy: uuid("decided_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_external_action_approvals_decision",
    sql`${table.decision} in ('approved', 'rejected', 'invalidated')`,
  ),
  check("ck_external_action_approvals_version_positive", sql`${table.actionVersion} > 0`),
  check("ck_external_action_approvals_hash_length", sql`length(${table.payloadHash}) = 64`),
  index("ix_external_action_approvals_action_version").on(table.actionId, table.actionVersion),
]);

export const externalActionAttempts = pgTable("external_action_attempts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  actionId: uuid("action_id")
    .references(() => externalActions.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  actionVersion: integer("action_version").notNull(),
  executorType: varchar("executor_type", { length: 16 }).default("manual").notNull(),
  executionMode: varchar("execution_mode", { length: 16 }).default("manual").notNull(),
  providerKey: varchar("provider_key", { length: 16 }),
  providerAdapterKey: varchar("provider_adapter_key", { length: 128 }),
  providerAdapterVersion: varchar("provider_adapter_version", { length: 32 }),
  capabilitySnapshotId: uuid("capability_snapshot_id"),
  capabilitySnapshotHash: varchar("capability_snapshot_hash", { length: 64 }),
  credentialStateHash: varchar("credential_state_hash", { length: 64 }),
  executionKey: varchar("execution_key", { length: 255 }),
  status: varchar("status", { length: 16 }).default("running").notNull(),
  providerAttemptRef: varchar("provider_attempt_ref", { length: 255 }),
  evidenceArtifactIds: json("evidence_artifact_ids").default([]).notNull(),
  resultSummary: text("result_summary"),
  evidenceNote: text("evidence_note"),
  errorMessage: text("error_message"),
  startedAt: timestamp("started_at").notNull(),
  finishedAt: timestamp("finished_at"),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
}, (table) => [
  check("ck_external_action_attempts_executor_type", sql`${table.executorType} in ('manual', 'provider')`),
  check("ck_external_action_attempts_execution_mode", sql`${table.executionMode} in ('manual', 'provider')`),
  check("ck_external_action_attempts_provider_executor", sql`${table.executionMode} <> 'provider' or ${table.executorType} = 'provider'`),
  check(
    "ck_external_action_attempts_provider_evidence",
    sql`${table.executionMode} <> 'provider' or (length(trim(${table.providerKey})) > 0 and length(trim(${table.providerAdapterKey})) > 0 and length(trim(${table.providerAdapterVersion})) > 0 and ${table.capabilitySnapshotId} is not null and length(${table.capabilitySnapshotHash}) = 64 and length(${table.credentialStateHash}) = 64 and length(trim(${table.executionKey})) > 0)`,
  ),
  check("ck_external_action_attempts_capability_snapshot_hash", sql`${table.capabilitySnapshotHash} is null or length(${table.capabilitySnapshotHash}) = 64`),
  check("ck_external_action_attempts_credential_state_hash", sql`${table.credentialStateHash} is null or length(${table.credentialStateHash}) = 64`),
  check(
    "ck_external_action_attempts_status",
    sql`${table.status} in ('running', 'succeeded', 'failed', 'uncertain')`,
  ),
  check("ck_external_action_attempts_version_positive", sql`${table.actionVersion} > 0`),
]);

export const externalActionReceipts = pgTable("external_action_receipts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  actionId: uuid("action_id")
    .references(() => externalActions.id, { onDelete: "cascade" })
    .notNull(),
  attemptId: uuid("attempt_id")
    .references(() => externalActionAttempts.id, { onDelete: "restrict" })
    .notNull()
    .unique(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  actionVersion: integer("action_version").notNull(),
  providerReceiptRef: varchar("provider_receipt_ref", { length: 255 }),
  remoteResourceId: varchar("remote_resource_id", { length: 255 }),
  remoteUrl: text("remote_url"),
  remoteStatus: varchar("remote_status", { length: 64 }),
  providerObservedAt: timestamp("provider_observed_at"),
  evidenceNote: text("evidence_note"),
  confirmationLevel: varchar("confirmation_level", { length: 32 }).default("human_confirmed").notNull(),
  evidenceArtifactIds: json("evidence_artifact_ids").default([]).notNull(),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_external_action_receipts_confirmation_level",
    sql`${table.confirmationLevel} in ('human_confirmed', 'provider_confirmed', 'reconciled')`,
  ),
  check("ck_external_action_receipts_version_positive", sql`${table.actionVersion} > 0`),
]);

export const operationEvents = pgTable("operation_events", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  entityType: varchar("entity_type", { length: 64 }).notNull(),
  entityId: uuid("entity_id").notNull(),
  eventType: varchar("event_type", { length: 100 }).notNull(),
  actorId: uuid("actor_id").references(() => users.id, { onDelete: "set null" }),
  actorType: varchar("actor_type", { length: 16 }).default("human").notNull(),
  payloadJson: json("payload").default({}).notNull(),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_operation_events_actor_type",
    sql`${table.actorType} in ('human', 'system', 'agent', 'unknown')`,
  ),
  index("ix_operation_events_entity_created").on(table.entityType, table.entityId, table.createdAt),
  index("ix_operation_events_project_created").on(table.projectId, table.createdAt),
]);

// ─── Typed Media Operations: Persona Core ───
// These declarations mirror alembic/versions/20260901_0005_media_operations_persona_core.py.
// Persona content is immutable: edits append a revision rather than updating
// the stable Persona row.  Resource provenance is either a URL or an artifact
// hash; credentials and raw provider payloads are deliberately absent.

export const mediaPersonas = pgTable("media_personas", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  state: varchar("state", { length: 16 }).default("draft").notNull(),
  parentBrandRef: varchar("parent_brand_ref", { length: 164 }),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_personas_create_hash_length", sql`length(${table.createHash}) = 64`),
  check("ck_media_personas_state", sql`${table.state} in ('draft', 'active', 'paused', 'archived')`),
  index("ix_media_personas_state").on(table.state),
  index("ix_media_personas_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_personas_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_personas_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaPersonaRevisions = pgTable("media_persona_revisions", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  personaId: uuid("persona_id")
    .references(() => mediaPersonas.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  version: integer("version").notNull(),
  displayName: varchar("display_name", { length: 120 }).notNull(),
  summary: text("summary"),
  voice: text("voice"),
  audience: text("audience"),
  niche: text("niche"),
  positioning: text("positioning"),
  visualIdentity: json("visual_identity"),
  creativeDirection: text("creative_direction"),
  allowedSubjects: json("allowed_subjects"),
  prohibitedSubjects: json("prohibited_subjects"),
  adultPolicy: varchar("adult_policy", { length: 32 }),
  sensitivePolicy: varchar("sensitive_policy", { length: 32 }),
  ipPolicy: varchar("ip_policy", { length: 32 }),
  disclosurePolicy: varchar("disclosure_policy", { length: 32 }),
  monetizationPolicy: json("monetization_policy"),
  kpiObjectives: json("kpi_objectives"),
  defaultLanguage: varchar("default_language", { length: 16 }),
  locale: varchar("locale", { length: 64 }),
  timezone: varchar("timezone", { length: 64 }),
  researchPolicy: json("research_policy"),
  imageProductionPolicy: json("image_production_policy"),
  videoProductionPolicy: json("video_production_policy"),
  publicAliases: json("public_aliases"),
  platformX: boolean("platform_x").default(false).notNull(),
  platformPixiv: boolean("platform_pixiv").default(false).notNull(),
  platformDlsite: boolean("platform_dlsite").default(false).notNull(),
  platformPatreon: boolean("platform_patreon").default(false).notNull(),
  platformYoutube: boolean("platform_youtube").default(false).notNull(),
  platformInstagram: boolean("platform_instagram").default(false).notNull(),
  contentPillars: json("content_pillars").default([]).notNull(),
  contentHash: varchar("content_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_persona_revisions_version_positive", sql`${table.version} > 0`),
  check("ck_media_persona_revisions_content_hash_length", sql`length(${table.contentHash}) = 64`),
  unique("uq_media_persona_revisions_version").on(table.personaId, table.version),
  unique("uq_media_persona_revisions_idempotency").on(table.personaId, table.idempotencyKey),
  index("ix_media_persona_revisions_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaPersonaResources = pgTable("media_persona_resources", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  personaId: uuid("persona_id")
    .references(() => mediaPersonas.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  resourceKind: varchar("resource_kind", { length: 32 }).notNull(),
  platform: varchar("platform", { length: 16 }),
  label: varchar("label", { length: 255 }),
  provenanceType: varchar("provenance_type", { length: 16 }).notNull(),
  sourceUrl: text("source_url"),
  artifactSha256: varchar("artifact_sha256", { length: 64 }),
  artifactMimeType: varchar("artifact_mime_type", { length: 255 }),
  resourceHash: varchar("resource_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_persona_resources_kind",
    sql`${table.resourceKind} in ('profile', 'reference', 'asset', 'persona_bible', 'character_bible', 'world_bible', 'visual_style_reference', 'reference_image', 'posting_rule', 'platform_rule', 'sensitive_rule', 'forbidden_content_rule', 'ip_rights_rule', 'monetization_rule', 'kpi_definition', 'experiment_policy', 'topic_source', 'idea_bank', 'high_performing_content', 'supporting_document')`,
  ),
  check(
    "ck_media_persona_resources_platform",
    sql`${table.platform} is null or ${table.platform} in ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')`,
  ),
  check(
    "ck_media_persona_resources_provenance_type",
    sql`${table.provenanceType} in ('url', 'artifact')`,
  ),
  check(
    "ck_media_persona_resources_provenance_shape",
    sql`(${table.provenanceType} = 'url' and ${table.sourceUrl} is not null and ${table.artifactSha256} is null and ${table.artifactMimeType} is null) or (${table.provenanceType} = 'artifact' and ${table.sourceUrl} is null and ${table.artifactSha256} is not null and ${table.artifactMimeType} is not null)`,
  ),
  check(
    "ck_media_persona_resources_artifact_hash_length",
    sql`${table.artifactSha256} is null or length(${table.artifactSha256}) = 64`,
  ),
  check("ck_media_persona_resources_resource_hash_length", sql`length(${table.resourceHash}) = 64`),
  unique("uq_media_persona_resources_idempotency").on(table.personaId, table.idempotencyKey),
  index("ix_media_persona_resources_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaPersonaIntakeSlots = pgTable("media_persona_intake_slots", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  slot: integer("slot").notNull(),
  personaId: uuid("persona_id")
    .references(() => mediaPersonas.id, { onDelete: "cascade" })
    .notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_persona_intake_slots_range", sql`${table.slot} >= 1 and ${table.slot} <= 9`),
  unique("uq_media_persona_intake_slots_persona").on(table.personaId),
  index("ix_media_persona_intake_slots_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_persona_intake_slots_personal_slot")
    .on(table.ownerUserId, table.slot)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_persona_intake_slots_project_slot")
    .on(table.projectId, table.slot)
    .where(sql`${table.projectId} is not null`),
]);

export type MediaPersona = typeof mediaPersonas.$inferSelect;
export type NewMediaPersona = typeof mediaPersonas.$inferInsert;
export type MediaPersonaRevision = typeof mediaPersonaRevisions.$inferSelect;
export type NewMediaPersonaRevision = typeof mediaPersonaRevisions.$inferInsert;
export type MediaPersonaResource = typeof mediaPersonaResources.$inferSelect;
export type NewMediaPersonaResource = typeof mediaPersonaResources.$inferInsert;
export type MediaPersonaIntakeSlot = typeof mediaPersonaIntakeSlots.$inferSelect;
export type NewMediaPersonaIntakeSlot = typeof mediaPersonaIntakeSlots.$inferInsert;

// ─── Typed Media Operations: Persona Setup ───
// These declarations mirror alembic/versions/20260901_0006_media_operations_setup.py.
// Bulk drafts retain typed fact state/evidence for human review; PlatformAccount
// stores only stable identity and capability status, never credentials or raw
// provider payloads.

export const mediaPersonaBulkDrafts = pgTable("media_persona_bulk_drafts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  sourceHash: varchar("source_hash", { length: 64 }).notNull(),
  draftHash: varchar("draft_hash", { length: 64 }).notNull(),
  version: integer("version").default(1).notNull(),
  status: varchar("status", { length: 16 }).default("draft").notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  applyIdempotencyKey: varchar("apply_idempotency_key", { length: 255 }),
  applyHash: varchar("apply_hash", { length: 64 }),
  appliedAt: timestamp("applied_at"),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check("ck_media_persona_bulk_drafts_source_hash", sql`length(${table.sourceHash}) = 64`),
  check("ck_media_persona_bulk_drafts_draft_hash", sql`length(${table.draftHash}) = 64`),
  check("ck_media_persona_bulk_drafts_version_positive", sql`${table.version} > 0`),
  check("ck_media_persona_bulk_drafts_status", sql`${table.status} in ('draft', 'applied')`),
  check(
    "ck_media_persona_bulk_drafts_apply_shape",
    sql`(${table.status} = 'draft' and ${table.applyIdempotencyKey} is null and ${table.applyHash} is null and ${table.appliedAt} is null) or (${table.status} = 'applied' and ${table.applyIdempotencyKey} is not null and ${table.applyHash} is not null and ${table.appliedAt} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_drafts_apply_hash",
    sql`${table.applyHash} is null or length(${table.applyHash}) = 64`,
  ),
  index("ix_media_persona_bulk_drafts_owner_user_id").on(table.ownerUserId),
  index("ix_media_persona_bulk_drafts_project_id").on(table.projectId),
  index("ix_media_persona_bulk_drafts_source_hash").on(table.sourceHash),
  index("ix_media_persona_bulk_drafts_draft_hash").on(table.draftHash),
  index("ix_media_persona_bulk_drafts_status").on(table.status),
  index("ix_media_persona_bulk_drafts_created_at").on(table.createdAt),
  index("ix_media_persona_bulk_drafts_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_persona_bulk_drafts_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_persona_bulk_drafts_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaPersonaBulkDraftSlots = pgTable("media_persona_bulk_draft_slots", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  draftId: uuid("draft_id")
    .references(() => mediaPersonaBulkDrafts.id, { onDelete: "cascade" })
    .notNull(),
  slot: integer("slot").notNull(),
  displayNameState: varchar("display_name_state", { length: 16 }).notNull(),
  displayNameValue: varchar("display_name_value", { length: 120 }),
  displayNameEvidence: text("display_name_evidence"),
  summaryState: varchar("summary_state", { length: 16 }).notNull(),
  summaryValue: text("summary_value"),
  summaryEvidence: text("summary_evidence"),
  voiceState: varchar("voice_state", { length: 16 }).notNull(),
  voiceValue: text("voice_value"),
  voiceEvidence: text("voice_evidence"),
  audienceState: varchar("audience_state", { length: 16 }).notNull(),
  audienceValue: text("audience_value"),
  audienceEvidence: text("audience_evidence"),
  platformsState: varchar("platforms_state", { length: 16 }).notNull(),
  platformX: boolean("platform_x"),
  platformPixiv: boolean("platform_pixiv"),
  platformDlsite: boolean("platform_dlsite"),
  platformPatreon: boolean("platform_patreon"),
  platformYoutube: boolean("platform_youtube"),
  platformInstagram: boolean("platform_instagram"),
  platformsEvidence: text("platforms_evidence"),
  contentPillarsState: varchar("content_pillars_state", { length: 16 }).notNull(),
  contentPillars: json("content_pillars").default([]).notNull(),
  contentPillarsEvidence: text("content_pillars_evidence"),
  slotHash: varchar("slot_hash", { length: 64 }).notNull(),
}, (table) => [
  check("ck_media_persona_bulk_draft_slots_range", sql`${table.slot} >= 1 and ${table.slot} <= 9`),
  unique("uq_media_persona_bulk_draft_slots_slot").on(table.draftId, table.slot),
  check("ck_media_persona_bulk_draft_slots_hash", sql`length(${table.slotHash}) = 64`),
  check(
    "ck_media_persona_bulk_draft_display_name_fact",
    sql`(${table.displayNameState} = 'unknown' and ${table.displayNameValue} is null and ${table.displayNameEvidence} is null) or (${table.displayNameState} = 'explicit' and ${table.displayNameValue} is not null) or (${table.displayNameState} = 'inferred' and ${table.displayNameValue} is not null and ${table.displayNameEvidence} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_draft_summary_fact",
    sql`(${table.summaryState} = 'unknown' and ${table.summaryValue} is null and ${table.summaryEvidence} is null) or (${table.summaryState} = 'explicit' and ${table.summaryValue} is not null) or (${table.summaryState} = 'inferred' and ${table.summaryValue} is not null and ${table.summaryEvidence} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_draft_voice_fact",
    sql`(${table.voiceState} = 'unknown' and ${table.voiceValue} is null and ${table.voiceEvidence} is null) or (${table.voiceState} = 'explicit' and ${table.voiceValue} is not null) or (${table.voiceState} = 'inferred' and ${table.voiceValue} is not null and ${table.voiceEvidence} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_draft_audience_fact",
    sql`(${table.audienceState} = 'unknown' and ${table.audienceValue} is null and ${table.audienceEvidence} is null) or (${table.audienceState} = 'explicit' and ${table.audienceValue} is not null) or (${table.audienceState} = 'inferred' and ${table.audienceValue} is not null and ${table.audienceEvidence} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_draft_platforms_fact",
    sql`(${table.platformsState} = 'unknown' and ${table.platformX} is null and ${table.platformPixiv} is null and ${table.platformDlsite} is null and ${table.platformPatreon} is null and ${table.platformYoutube} is null and ${table.platformInstagram} is null and ${table.platformsEvidence} is null) or (${table.platformsState} = 'explicit' and ${table.platformX} is not null and ${table.platformPixiv} is not null and ${table.platformDlsite} is not null and ${table.platformPatreon} is not null and ${table.platformYoutube} is not null and ${table.platformInstagram} is not null) or (${table.platformsState} = 'inferred' and ${table.platformX} is not null and ${table.platformPixiv} is not null and ${table.platformDlsite} is not null and ${table.platformPatreon} is not null and ${table.platformYoutube} is not null and ${table.platformInstagram} is not null and ${table.platformsEvidence} is not null)`,
  ),
  check(
    "ck_media_persona_bulk_draft_pillars_state",
    sql`${table.contentPillarsState} in ('explicit', 'inferred', 'unknown')`,
  ),
  check(
    "ck_media_persona_bulk_draft_pillars_inferred_evidence",
    sql`${table.contentPillarsState} != 'inferred' or ${table.contentPillarsEvidence} is not null`,
  ),
  index("ix_media_persona_bulk_draft_slots_draft_id").on(table.draftId),
  index("ix_media_persona_bulk_draft_slots_slot_hash").on(table.slotHash),
]);

export const mediaPlatformAccounts = pgTable("media_platform_accounts", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  personaId: uuid("persona_id").references(() => mediaPersonas.id, {
    onDelete: "set null",
  }),
  connectionId: uuid("connection_id").references(() => externalConnections.id, {
    onDelete: "set null",
  }),
  accountType: varchar("account_type", { length: 32 }).default("profile").notNull(),
  remoteUrl: text("remote_url"),
  status: varchar("status", { length: 16 }).default("active").notNull(),
  platform: varchar("platform", { length: 16 }).notNull(),
  accountRef: varchar("account_ref", { length: 255 }).notNull(),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_platform_accounts_platform",
    sql`${table.platform} in ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')`,
  ),
  check("ck_media_platform_accounts_create_hash", sql`length(${table.createHash}) = 64`),
  check("ck_media_platform_accounts_status", sql`${table.status} in ('active', 'paused')`),
  index("ix_media_platform_accounts_owner_user_id").on(table.ownerUserId),
  index("ix_media_platform_accounts_project_id").on(table.projectId),
  index("ix_media_platform_accounts_platform").on(table.platform),
  index("ix_media_platform_accounts_persona_id").on(table.personaId),
  index("ix_media_platform_accounts_connection_id").on(table.connectionId),
  uniqueIndex("uq_media_platform_accounts_connection_id")
    .on(table.connectionId)
    .where(sql`${table.connectionId} is not null`),
  index("ix_media_platform_accounts_status").on(table.status),
  index("ix_media_platform_accounts_create_hash").on(table.createHash),
  index("ix_media_platform_accounts_created_at").on(table.createdAt),
  index("ix_media_platform_accounts_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_platform_accounts_personal_identity")
    .on(table.ownerUserId, table.platform, table.accountRef)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_platform_accounts_project_identity")
    .on(table.projectId, table.platform, table.accountRef)
    .where(sql`${table.projectId} is not null`),
  uniqueIndex("uq_media_platform_accounts_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_platform_accounts_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaPlatformAccountRevisions = pgTable("media_platform_account_revisions", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  platformAccountId: uuid("platform_account_id")
    .references(() => mediaPlatformAccounts.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  version: integer("version").notNull(),
  displayName: varchar("display_name", { length: 255 }).notNull(),
  publishCapability: varchar("publish_capability", { length: 16 }).notNull(),
  mediaCapability: varchar("media_capability", { length: 16 }).notNull(),
  analyticsCapability: varchar("analytics_capability", { length: 16 }).notNull(),
  credentialStatus: varchar("credential_status", { length: 24 }).notNull(),
  remoteUrl: text("remote_url"),
  locale: varchar("locale", { length: 64 }),
  timezone: varchar("timezone", { length: 64 }),
  supportedContentModes: json("supported_content_modes").default([]),
  disclosureDefaults: json("disclosure_defaults").default({}),
  ratingDefaults: json("rating_defaults").default({}),
  adapterRef: varchar("adapter_ref", { length: 164 }),
  contentHash: varchar("content_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_platform_account_revisions_version", sql`${table.version} > 0`),
  check(
    "ck_media_platform_account_revisions_publish",
    sql`${table.publishCapability} in ('unknown', 'available', 'unsupported')`,
  ),
  check(
    "ck_media_platform_account_revisions_media",
    sql`${table.mediaCapability} in ('unknown', 'available', 'unsupported')`,
  ),
  check(
    "ck_media_platform_account_revisions_analytics",
    sql`${table.analyticsCapability} in ('unknown', 'available', 'unsupported')`,
  ),
  check(
    "ck_media_platform_account_revisions_credentials",
    sql`${table.credentialStatus} in ('unknown', 'not_configured', 'configured', 'invalid')`,
  ),
  check("ck_media_platform_account_revisions_timezone", sql`${table.timezone} is null or length(${table.timezone}) <= 64`),
  check("ck_media_platform_account_revisions_hash", sql`length(${table.contentHash}) = 64`),
  unique("uq_media_platform_account_revisions_version").on(table.platformAccountId, table.version),
  unique("uq_media_platform_account_revisions_idempotency").on(table.platformAccountId, table.idempotencyKey),
  index("ix_media_platform_account_revisions_platform_account_id").on(table.platformAccountId),
  index("ix_media_platform_account_revisions_owner_user_id").on(table.ownerUserId),
  index("ix_media_platform_account_revisions_project_id").on(table.projectId),
  index("ix_media_platform_account_revisions_content_hash").on(table.contentHash),
  index("ix_media_platform_account_revisions_created_at").on(table.createdAt),
  index("ix_media_platform_account_revisions_owner_project").on(table.ownerUserId, table.projectId),
]);

// ─── Typed Media Operations: encrypted credential vault ───
// Mirrors alembic/versions/20260901_0018_media_credential_vault.py.  The
// browser-side Drizzle schema contains ciphertext metadata only; application
// code never selects or serializes the encrypted payload into a UI DTO.
export const mediaPlatformCredentials = pgTable("media_platform_credentials", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  platformAccountId: uuid("platform_account_id")
    .references(() => mediaPlatformAccounts.id, { onDelete: "cascade" })
    .notNull(),
  connectionId: uuid("connection_id")
    .references(() => externalConnections.id, { onDelete: "restrict" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  connectionType: varchar("connection_type", { length: 24 }).notNull(),
  encryptedPayload: text("encrypted_payload").notNull(),
  encryptionKeyId: varchar("encryption_key_id", { length: 128 }).notNull(),
  payloadDigest: varchar("payload_digest", { length: 64 }).notNull(),
  revision: integer("revision").default(1).notNull(),
  stateHash: varchar("state_hash", { length: 64 }).notNull(),
  status: varchar("status", { length: 32 }).notNull(),
  capabilities: json("capabilities").default({}).notNull(),
  verificationCode: varchar("verification_code", { length: 64 }),
  verificationStartedAt: timestamp("verification_started_at"),
  verificationCompletedAt: timestamp("verification_completed_at"),
  lastVerifiedAt: timestamp("last_verified_at"),
  disabledAt: timestamp("disabled_at"),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check(
    "ck_media_platform_credentials_connection_type",
    sql`${table.connectionType} in ('cookie_export', 'api_token', 'oauth')`,
  ),
  check(
    "ck_media_platform_credentials_status",
    sql`${table.status} in ('verification_pending', 'verified', 'invalid', 'unsupported', 'disabled', 'key_unavailable')`,
  ),
  check("ck_media_platform_credentials_revision", sql`${table.revision} > 0`),
  check("ck_media_platform_credentials_payload_digest", sql`length(${table.payloadDigest}) = 64`),
  check("ck_media_platform_credentials_state_hash", sql`length(${table.stateHash}) = 64`),
  uniqueIndex("uq_media_platform_credentials_platform_account").on(table.platformAccountId),
  uniqueIndex("uq_media_platform_credentials_connection").on(table.connectionId),
  index("ix_media_platform_credentials_platform_account_id").on(table.platformAccountId),
  index("ix_media_platform_credentials_connection_id").on(table.connectionId),
  index("ix_media_platform_credentials_owner_user_id").on(table.ownerUserId),
  index("ix_media_platform_credentials_project_id").on(table.projectId),
  index("ix_media_platform_credentials_status").on(table.status),
  index("ix_media_platform_credentials_created_at").on(table.createdAt),
  index("ix_media_platform_credentials_updated_at").on(table.updatedAt),
  index("ix_media_platform_credentials_owner_project").on(table.ownerUserId, table.projectId),
  index("ix_media_platform_credentials_status_updated").on(table.status, table.updatedAt),
]);

export const mediaPlatformCredentialAuditEvents = pgTable("media_platform_credential_audit_events", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  credentialId: uuid("credential_id").notNull(),
  platformAccountId: uuid("platform_account_id").notNull(),
  ownerUserId: uuid("owner_user_id").notNull(),
  projectId: uuid("project_id"),
  eventType: varchar("event_type", { length: 16 }).notNull(),
  actorId: uuid("actor_id").references(() => users.id, { onDelete: "set null" }),
  actorType: varchar("actor_type", { length: 16 }).default("human").notNull(),
  snapshotJson: json("snapshot_json").default({}).notNull(),
  requestHash: varchar("request_hash", { length: 64 }).notNull(),
  sequence: integer("sequence").default(1).notNull(),
  idempotencyScope: varchar("idempotency_scope", { length: 255 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  prevEventHash: varchar("prev_event_hash", { length: 64 }),
  eventHash: varchar("event_hash", { length: 64 }).notNull(),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_platform_credential_audit_event_type", sql`${table.eventType} in ('add', 'rotate', 'verify', 'disable', 'rekey')`),
  check("ck_media_platform_credential_audit_actor_type", sql`${table.actorType} in ('human', 'admin', 'unknown')`),
  check("ck_media_platform_credential_audit_request_hash", sql`length(${table.requestHash}) = 64`),
  check("ck_media_platform_credential_audit_event_hash", sql`length(${table.eventHash}) = 64`),
  check("ck_media_platform_credential_audit_sequence", sql`${table.sequence} > 0`),
  check("ck_media_platform_credential_audit_prev_hash", sql`${table.prevEventHash} is null or length(${table.prevEventHash}) = 64`),
  unique("uq_media_platform_credential_audit_scope_key").on(table.idempotencyScope, table.idempotencyKey),
  unique("uq_media_platform_credential_audit_sequence").on(table.credentialId, table.sequence),
  index("ix_media_platform_credential_audit_events_credential_id").on(table.credentialId),
  index("ix_media_platform_credential_audit_events_platform_account_id").on(table.platformAccountId),
  index("ix_media_platform_credential_audit_events_owner_user_id").on(table.ownerUserId),
  index("ix_media_platform_credential_audit_events_project_id").on(table.projectId),
  index("ix_media_platform_credential_audit_events_created_at").on(table.createdAt),
  index("ix_media_platform_credential_audit_account_created").on(table.platformAccountId, table.createdAt),
  index("ix_media_platform_credential_audit_owner_project_created").on(table.ownerUserId, table.projectId, table.createdAt),
]);

export type MediaPersonaBulkDraft = typeof mediaPersonaBulkDrafts.$inferSelect;
export type NewMediaPersonaBulkDraft = typeof mediaPersonaBulkDrafts.$inferInsert;
export type MediaPersonaBulkDraftSlot = typeof mediaPersonaBulkDraftSlots.$inferSelect;
export type NewMediaPersonaBulkDraftSlot = typeof mediaPersonaBulkDraftSlots.$inferInsert;
export type MediaPlatformAccount = typeof mediaPlatformAccounts.$inferSelect;
export type NewMediaPlatformAccount = typeof mediaPlatformAccounts.$inferInsert;
export type MediaPlatformAccountRevision = typeof mediaPlatformAccountRevisions.$inferSelect;
export type NewMediaPlatformAccountRevision = typeof mediaPlatformAccountRevisions.$inferInsert;
export type MediaPlatformCredential = typeof mediaPlatformCredentials.$inferSelect;
export type NewMediaPlatformCredential = typeof mediaPlatformCredentials.$inferInsert;
export type MediaPlatformCredentialAuditEvent = typeof mediaPlatformCredentialAuditEvents.$inferSelect;
export type NewMediaPlatformCredentialAuditEvent = typeof mediaPlatformCredentialAuditEvents.$inferInsert;

// ─── Typed Media Operations: provider capability observations ───
// Mirrors alembic/versions/20260902_0023_media_provider_capability_snapshots.py.
// This is an append-only, secret-free observation ledger.  Credential state
// hashes are server-side binding evidence and are intentionally not projected
// to browser DTOs, but remain represented here for schema parity.
export const mediaProviderCapabilitySnapshots = pgTable("media_provider_capability_snapshots", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  platformAccountId: uuid("platform_account_id")
    .references(() => mediaPlatformAccounts.id, { onDelete: "restrict" })
    .notNull(),
  accountRevisionId: uuid("account_revision_id")
    .references(() => mediaPlatformAccountRevisions.id, { onDelete: "restrict" })
    .notNull(),
  credentialId: uuid("credential_id")
    .references(() => mediaPlatformCredentials.id, { onDelete: "restrict" })
    .notNull(),
  credentialStateHash: varchar("credential_state_hash", { length: 64 }).notNull(),
  credentialRevision: integer("credential_revision").notNull(),
  accountRevision: integer("account_revision").notNull(),
  credentialScope: varchar("credential_scope", { length: 255 }).notNull(),
  accountType: varchar("account_type", { length: 32 }).notNull(),
  provider: varchar("provider", { length: 16 }).notNull(),
  operation: varchar("operation", { length: 16 }).notNull(),
  status: varchar("status", { length: 16 }).notNull(),
  registryVersion: varchar("registry_version", { length: 32 }).notNull(),
  grantedScopes: json("granted_scopes").notNull().default([]),
  accountEligibility: varchar("account_eligibility", { length: 16 }).notNull().default("unknown"),
  adapterKey: varchar("adapter_key", { length: 128 }).notNull(),
  adapterVersion: varchar("adapter_version", { length: 32 }).notNull(),
  observedAt: timestamp("observed_at").notNull(),
  snapshotHash: varchar("snapshot_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_provider_capability_snapshots_provider", sql`${table.provider} in ('x', 'pixiv', 'patreon', 'youtube', 'instagram', 'dlsite')`),
  check("ck_media_provider_capability_snapshots_operation", sql`${table.operation} in ('identity', 'oauth', 'token', 'cookie', 'text', 'image', 'video', 'schedule', 'edit', 'delete', 'analytics', 'revenue', 'refresh', 'revoke')`),
  check("ck_media_provider_capability_snapshots_status", sql`${table.status} in ('automatable', 'manual', 'unsupported', 'unverified', 'unavailable')`),
  check("ck_media_provider_capability_snapshots_account_eligibility", sql`${table.accountEligibility} in ('unknown', 'eligible', 'ineligible', 'unverified')`),
  check("ck_media_provider_capability_snapshots_credential_revision", sql`${table.credentialRevision} > 0`),
  check("ck_media_provider_capability_snapshots_account_revision", sql`${table.accountRevision} > 0`),
  check("ck_media_provider_capability_snapshots_credential_state_hash", sql`length(${table.credentialStateHash}) = 64`),
  check("ck_media_provider_capability_snapshots_snapshot_hash", sql`length(${table.snapshotHash}) = 64`),
  check("ck_media_provider_capability_snapshots_credential_scope", sql`length(trim(${table.credentialScope})) > 0`),
  check("ck_media_provider_capability_snapshots_adapter_key", sql`length(trim(${table.adapterKey})) > 0`),
  check("ck_media_provider_capability_snapshots_adapter_version", sql`length(trim(${table.adapterVersion})) > 0`),
  check("ck_media_provider_capability_snapshots_registry_version", sql`length(trim(${table.registryVersion})) > 0`),
  unique("uq_media_provider_capability_snapshots_observation").on(table.platformAccountId, table.operation, table.credentialRevision, table.accountRevision, table.snapshotHash),
  index("ix_media_provider_capability_snapshots_owner_user_id").on(table.ownerUserId),
  index("ix_media_provider_capability_snapshots_project_id").on(table.projectId),
  index("ix_media_provider_capability_snapshots_platform_account_id").on(table.platformAccountId),
  index("ix_media_provider_capability_snapshots_account_revision_id").on(table.accountRevisionId),
  index("ix_media_provider_capability_snapshots_credential_id").on(table.credentialId),
  index("ix_media_provider_capability_snapshots_snapshot_hash").on(table.snapshotHash),
  index("ix_media_provider_capability_snapshots_observed_at").on(table.observedAt),
  index("ix_media_provider_capability_snapshots_owner_project").on(table.ownerUserId, table.projectId),
  index("ix_media_provider_cap_snap_account_op_observed").on(table.platformAccountId, table.operation, table.observedAt),
  uniqueIndex("uq_media_provider_capability_snapshots_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_provider_capability_snapshots_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export type MediaProviderCapabilitySnapshot = typeof mediaProviderCapabilitySnapshots.$inferSelect;
export type NewMediaProviderCapabilitySnapshot = typeof mediaProviderCapabilitySnapshots.$inferInsert;

// ─── Typed Media Operations: Research + Editorial Trace ───
// Mirrors alembic/versions/20260901_0007_media_operations_research_editorial.py.

export const mediaResearchRoutines = pgTable("media_research_routines", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  personaId: uuid("persona_id").references(() => mediaPersonas.id, {
    onDelete: "set null",
  }),
  state: varchar("state", { length: 16 }).default("draft").notNull(),
  enabled: boolean("enabled").default(false).notNull(),
  platformAccountId: uuid("platform_account_id").references(
    () => mediaPlatformAccounts.id,
    { onDelete: "set null" },
  ),
  lastDueAt: timestamp("last_due_at"),
  nextDueAt: timestamp("next_due_at"),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_research_routines_create_hash",
    sql`length(${table.createHash}) = 64`,
  ),
  index("ix_media_research_routines_owner_user_id").on(table.ownerUserId),
  index("ix_media_research_routines_project_id").on(table.projectId),
  index("ix_media_research_routines_create_hash").on(table.createHash),
  index("ix_media_research_routines_created_at").on(table.createdAt),
  index("ix_media_research_routines_persona_id").on(table.personaId),
  index("ix_media_research_routines_state").on(table.state),
  index("ix_media_research_routines_enabled").on(table.enabled),
  index("ix_media_research_routines_platform_account_id").on(table.platformAccountId),
  index("ix_media_research_routines_last_due_at").on(table.lastDueAt),
  index("ix_media_research_routines_next_due_at").on(table.nextDueAt),
  index("ix_media_research_routines_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
  uniqueIndex("uq_media_research_routines_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_research_routines_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaResearchRoutineRevisions = pgTable(
  "media_research_routine_revisions",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    researchRoutineId: uuid("research_routine_id")
      .references(() => mediaResearchRoutines.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    version: integer("version").notNull(),
    name: varchar("name", { length: 255 }).notNull(),
    objective: text("objective").notNull(),
    cadence: varchar("cadence", { length: 32 }).default("manual").notNull(),
    timezone: varchar("timezone", { length: 64 }).default("UTC").notNull(),
    schedule: json("schedule").default({}).notNull(),
    sourceTypes: json("source_types").default([]).notNull(),
    searchQueries: json("search_queries").default([]).notNull(),
    domains: json("domains").default([]).notNull(),
    followAccounts: json("follow_accounts").default([]).notNull(),
    followTags: json("follow_tags").default([]).notNull(),
    exclusions: json("exclusions").default([]).notNull(),
    freshnessHours: integer("freshness_hours").default(168).notNull(),
    maxCandidates: integer("max_candidates").default(20).notNull(),
    reviewPolicy: varchar("review_policy", { length: 64 })
      .default("human_review")
      .notNull(),
    questions: json("questions").default([]).notNull(),
    platformX: boolean("platform_x").default(false).notNull(),
    platformPixiv: boolean("platform_pixiv").default(false).notNull(),
    platformDlsite: boolean("platform_dlsite").default(false).notNull(),
    platformPatreon: boolean("platform_patreon").default(false).notNull(),
    platformYoutube: boolean("platform_youtube").default(false).notNull(),
    platformInstagram: boolean("platform_instagram").default(false).notNull(),
    contentHash: varchar("content_hash", { length: 64 }).notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_research_routine_revisions_version",
      sql`${table.version} > 0`,
    ),
    check(
      "ck_media_research_routine_revisions_hash",
      sql`length(${table.contentHash}) = 64`,
    ),
    unique("uq_media_research_routine_revisions_version").on(
      table.researchRoutineId,
      table.version,
    ),
    unique("uq_media_research_routine_revisions_idempotency").on(
      table.researchRoutineId,
      table.idempotencyKey,
    ),
    index("ix_media_research_routine_revisions_research_routine_id").on(
      table.researchRoutineId,
    ),
    index("ix_media_research_routine_revisions_owner_user_id").on(
      table.ownerUserId,
    ),
    index("ix_media_research_routine_revisions_project_id").on(
      table.projectId,
    ),
    index("ix_media_research_routine_revisions_content_hash").on(
      table.contentHash,
    ),
    index("ix_media_research_routine_revisions_created_at").on(
      table.createdAt,
    ),
    index("ix_media_research_routine_revisions_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
  ],
);

export const mediaResearchRuns = pgTable("media_research_runs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  researchRoutineId: uuid("research_routine_id")
    .references(() => mediaResearchRoutines.id, { onDelete: "cascade" })
    .notNull(),
  researchRoutineRevisionId: uuid("research_routine_revision_id")
    .references(() => mediaResearchRoutineRevisions.id, {
      onDelete: "cascade",
    })
    .notNull(),
  routineContentHash: varchar("routine_content_hash", { length: 64 }).notNull(),
  focusNote: text("focus_note"),
  runHash: varchar("run_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  status: varchar("status", { length: 16 }).default("recorded").notNull(),
  startedAt: timestamp("started_at"),
  finishedAt: timestamp("finished_at"),
  sourceRefs: json("source_refs").default([]).notNull(),
  omissions: json("omissions").default([]).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_research_runs_routine_hash",
    sql`length(${table.routineContentHash}) = 64`,
  ),
  check("ck_media_research_runs_hash", sql`length(${table.runHash}) = 64`),
  unique("uq_media_research_runs_idempotency").on(
    table.researchRoutineId,
    table.idempotencyKey,
  ),
  index("ix_media_research_runs_owner_user_id").on(table.ownerUserId),
  index("ix_media_research_runs_project_id").on(table.projectId),
  index("ix_media_research_runs_research_routine_id").on(
    table.researchRoutineId,
  ),
  index("ix_media_research_runs_research_routine_revision_id").on(
    table.researchRoutineRevisionId,
  ),
  index("ix_media_research_runs_run_hash").on(table.runHash),
  index("ix_media_research_runs_status").on(table.status),
  index("ix_media_research_runs_started_at").on(table.startedAt),
  index("ix_media_research_runs_finished_at").on(table.finishedAt),
  index("ix_media_research_runs_created_at").on(table.createdAt),
  index("ix_media_research_runs_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
]);

export const mediaResearchFindings = pgTable("media_research_findings", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  researchRunId: uuid("research_run_id")
    .references(() => mediaResearchRuns.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  kind: varchar("kind", { length: 16 }).notNull(),
  statement: text("statement").notNull(),
  findingHash: varchar("finding_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_research_findings_kind",
    sql`${table.kind} in ('fact', 'signal', 'hypothesis')`,
  ),
  check(
    "ck_media_research_findings_hash",
    sql`length(${table.findingHash}) = 64`,
  ),
  unique("uq_media_research_findings_idempotency").on(
    table.researchRunId,
    table.idempotencyKey,
  ),
  unique("uq_media_research_findings_hash").on(
    table.researchRunId,
    table.findingHash,
  ),
  index("ix_media_research_findings_research_run_id").on(table.researchRunId),
  index("ix_media_research_findings_owner_user_id").on(table.ownerUserId),
  index("ix_media_research_findings_project_id").on(table.projectId),
  index("ix_media_research_findings_kind").on(table.kind),
  index("ix_media_research_findings_finding_hash").on(table.findingHash),
  index("ix_media_research_findings_created_at").on(table.createdAt),
  index("ix_media_research_findings_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
]);

export const mediaResearchFindingEvidence = pgTable(
  "media_research_finding_evidence",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    findingId: uuid("finding_id")
      .references(() => mediaResearchFindings.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    ordinal: integer("ordinal").notNull(),
    evidenceType: varchar("evidence_type", { length: 16 }).notNull(),
    label: varchar("label", { length: 255 }),
    sourceUrl: text("source_url"),
    artifactSha256: varchar("artifact_sha256", { length: 64 }),
    artifactMimeType: varchar("artifact_mime_type", { length: 255 }),
    note: text("note"),
    evidenceHash: varchar("evidence_hash", { length: 64 }).notNull(),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_research_finding_evidence_ordinal",
      sql`${table.ordinal} >= 1 and ${table.ordinal} <= 20`,
    ),
    check(
      "ck_media_research_finding_evidence_type",
      sql`${table.evidenceType} in ('url', 'artifact')`,
    ),
    check(
      "ck_media_research_finding_evidence_shape",
      sql`(${table.evidenceType} = 'url' and ${table.sourceUrl} is not null and ${table.artifactSha256} is null and ${table.artifactMimeType} is null) or (${table.evidenceType} = 'artifact' and ${table.sourceUrl} is null and ${table.artifactSha256} is not null and ${table.artifactMimeType} is not null)`,
    ),
    check(
      "ck_media_research_finding_evidence_artifact_hash",
      sql`${table.artifactSha256} is null or length(${table.artifactSha256}) = 64`,
    ),
    check(
      "ck_media_research_finding_evidence_hash",
      sql`length(${table.evidenceHash}) = 64`,
    ),
    unique("uq_media_research_finding_evidence_ordinal").on(
      table.findingId,
      table.ordinal,
    ),
    unique("uq_media_research_finding_evidence_hash").on(
      table.findingId,
      table.evidenceHash,
    ),
    index("ix_media_research_finding_evidence_finding_id").on(table.findingId),
    index("ix_media_research_finding_evidence_owner_user_id").on(
      table.ownerUserId,
    ),
    index("ix_media_research_finding_evidence_project_id").on(table.projectId),
    index("ix_media_research_finding_evidence_evidence_hash").on(
      table.evidenceHash,
    ),
    index("ix_media_research_finding_evidence_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
  ],
);

export const mediaEditorialPrograms = pgTable("media_editorial_programs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  personaId: uuid("persona_id")
    .references(() => mediaPersonas.id, { onDelete: "cascade" })
    .notNull(),
  state: varchar("state", { length: 16 }).default("draft").notNull(),
  enabled: boolean("enabled").default(false).notNull(),
  lastDueAt: timestamp("last_due_at"),
  nextDueAt: timestamp("next_due_at"),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_editorial_programs_create_hash",
    sql`length(${table.createHash}) = 64`,
  ),
  index("ix_media_editorial_programs_owner_user_id").on(table.ownerUserId),
  index("ix_media_editorial_programs_project_id").on(table.projectId),
  index("ix_media_editorial_programs_persona_id").on(table.personaId),
  index("ix_media_editorial_programs_create_hash").on(table.createHash),
  index("ix_media_editorial_programs_created_at").on(table.createdAt),
  index("ix_media_editorial_programs_state").on(table.state),
  index("ix_media_editorial_programs_enabled").on(table.enabled),
  index("ix_media_editorial_programs_last_due_at").on(table.lastDueAt),
  index("ix_media_editorial_programs_next_due_at").on(table.nextDueAt),
  index("ix_media_editorial_programs_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
  uniqueIndex("uq_media_editorial_programs_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_editorial_programs_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaEditorialProgramRevisions = pgTable(
  "media_editorial_program_revisions",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    editorialProgramId: uuid("editorial_program_id")
      .references(() => mediaEditorialPrograms.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    version: integer("version").notNull(),
    name: varchar("name", { length: 255 }).notNull(),
    objective: text("objective").notNull(),
    contentType: varchar("content_type", { length: 64 })
      .default("article")
      .notNull(),
    cadence: varchar("cadence", { length: 32 }).default("manual").notNull(),
    targetPlatforms: json("target_platforms").default([]).notNull(),
    targetAccountRefs: json("target_account_refs").default([]).notNull(),
    contentPillar: varchar("content_pillar", { length: 200 }),
    requiredResources: json("required_resources").default([]).notNull(),
    defaultCreativeRecipeRef: varchar("default_creative_recipe_ref", {
      length: 164,
    }),
    defaultQaPolicyRef: varchar("default_qa_policy_ref", { length: 164 }),
    experimentRef: varchar("experiment_ref", { length: 164 }),
    draftGenerationPolicy: varchar("draft_generation_policy", { length: 64 })
      .default("human_review")
      .notNull(),
    contentHash: varchar("content_hash", { length: 64 }).notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_editorial_program_revisions_version",
      sql`${table.version} > 0`,
    ),
    check(
      "ck_media_editorial_program_revisions_hash",
      sql`length(${table.contentHash}) = 64`,
    ),
    unique("uq_media_editorial_program_revisions_version").on(
      table.editorialProgramId,
      table.version,
    ),
    unique("uq_media_editorial_program_revisions_idempotency").on(
      table.editorialProgramId,
      table.idempotencyKey,
    ),
    index("ix_media_editorial_program_revisions_editorial_program_id").on(
      table.editorialProgramId,
    ),
    index("ix_media_editorial_program_revisions_owner_user_id").on(
      table.ownerUserId,
    ),
    index("ix_media_editorial_program_revisions_project_id").on(table.projectId),
    index("ix_media_editorial_program_revisions_content_hash").on(
      table.contentHash,
    ),
    index("ix_media_editorial_program_revisions_created_at").on(table.createdAt),
    index("ix_media_editorial_program_revisions_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
  ],
);

export const mediaContentItems = pgTable("media_content_items", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  editorialProgramId: uuid("editorial_program_id")
    .references(() => mediaEditorialPrograms.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  title: varchar("title", { length: 500 }).notNull(),
  brief: text("brief").notNull(),
  version: integer("version").default(1).notNull(),
  status: varchar("status", { length: 16 }).default("draft").notNull(),
  personaRevisionId: uuid("persona_revision_id").references(
    () => mediaPersonaRevisions.id,
    { onDelete: "set null" },
  ),
  objective: text("objective"),
  contentType: varchar("content_type", { length: 64 })
    .default("article")
    .notNull(),
  contentPillar: varchar("content_pillar", { length: 200 }),
  intendedAudience: text("intended_audience"),
  sourceRefs: json("source_refs").default([]).notNull(),
  candidateRefs: json("candidate_refs").default([]).notNull(),
  desiredAssets: json("desired_assets").default([]).notNull(),
  monetizationRef: varchar("monetization_ref", { length: 164 }),
  experimentRef: varchar("experiment_ref", { length: 164 }),
  scheduledAt: timestamp("scheduled_at"),
  contentHash: varchar("content_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_content_items_hash", sql`length(${table.contentHash}) = 64`),
  unique("uq_media_content_items_idempotency").on(
    table.editorialProgramId,
    table.idempotencyKey,
  ),
  index("ix_media_content_items_editorial_program_id").on(
    table.editorialProgramId,
  ),
  index("ix_media_content_items_owner_user_id").on(table.ownerUserId),
  index("ix_media_content_items_project_id").on(table.projectId),
  index("ix_media_content_items_content_hash").on(table.contentHash),
  index("ix_media_content_items_status").on(table.status),
  index("ix_media_content_items_persona_revision_id").on(table.personaRevisionId),
  index("ix_media_content_items_scheduled_at").on(table.scheduledAt),
  index("ix_media_content_items_created_at").on(table.createdAt),
  index("ix_media_content_items_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
]);

/** Source-backed research candidates; mutable status is only a projection. */
export const mediaResearchCandidates = pgTable("media_research_candidates", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  researchRoutineId: uuid("research_routine_id")
    .references(() => mediaResearchRoutines.id, { onDelete: "cascade" })
    .notNull(),
  researchRunId: uuid("research_run_id")
    .references(() => mediaResearchRuns.id, { onDelete: "cascade" })
    .notNull(),
  routineRevisionId: uuid("routine_revision_id")
    .references(() => mediaResearchRoutineRevisions.id, { onDelete: "cascade" })
    .notNull(),
  candidateKey: varchar("candidate_key", { length: 64 }).notNull(),
  title: varchar("title", { length: 500 }).notNull(),
  summary: text("summary").notNull(),
  sourceUrl: text("source_url"),
  sourcePublishedAt: timestamp("source_published_at"),
  discoveredAt: timestamp("discovered_at").notNull(),
  expiresAt: timestamp("expires_at"),
  relevanceScore: doublePrecision("relevance_score"),
  freshnessScore: doublePrecision("freshness_score"),
  evidence: json("evidence").default([]).notNull(),
  reason: text("reason"),
  status: varchar("status", { length: 16 }).default("discovered").notNull(),
  decisionVersion: integer("decision_version").default(0).notNull(),
  contentItemId: uuid("content_item_id").references(() => mediaContentItems.id, {
    onDelete: "set null",
  }),
  candidateHash: varchar("candidate_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check(
    "ck_media_research_candidates_status",
    sql`${table.status} in ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')`,
  ),
  check("ck_media_research_candidates_key", sql`${table.candidateKey} <> ''`),
  check(
    "ck_media_research_candidates_hash",
    sql`length(${table.candidateHash}) = 64`,
  ),
  check(
    "ck_media_research_candidates_relevance",
    sql`${table.relevanceScore} is null or (${table.relevanceScore} >= 0 and ${table.relevanceScore} <= 1)`,
  ),
  check(
    "ck_media_research_candidates_freshness",
    sql`${table.freshnessScore} is null or (${table.freshnessScore} >= 0 and ${table.freshnessScore} <= 1)`,
  ),
  check(
    "ck_media_research_candidates_decision_version",
    sql`${table.decisionVersion} >= 0`,
  ),
  unique("uq_media_research_candidates_identity").on(
    table.researchRoutineId,
    table.candidateKey,
  ),
  unique("uq_media_research_candidates_idempotency").on(
    table.researchRunId,
    table.idempotencyKey,
  ),
  index("ix_media_research_candidates_owner_user_id").on(table.ownerUserId),
  index("ix_media_research_candidates_project_id").on(table.projectId),
  index("ix_media_research_candidates_research_routine_id").on(
    table.researchRoutineId,
  ),
  index("ix_media_research_candidates_research_run_id").on(table.researchRunId),
  index("ix_media_research_candidates_routine_revision_id").on(
    table.routineRevisionId,
  ),
  index("ix_media_research_candidates_candidate_key").on(table.candidateKey),
  index("ix_media_research_candidates_discovered_at").on(table.discoveredAt),
  index("ix_media_research_candidates_expires_at").on(table.expiresAt),
  index("ix_media_research_candidates_status").on(table.status),
  index("ix_media_research_candidates_content_item_id").on(table.contentItemId),
  index("ix_media_research_candidates_candidate_hash").on(table.candidateHash),
  index("ix_media_research_candidates_created_at").on(table.createdAt),
  index("ix_media_research_candidates_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
]);

/** Immutable review/expiry/promotion history for each candidate. */
export const mediaResearchCandidateDecisions = pgTable(
  "media_research_candidate_decisions",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    candidateId: uuid("candidate_id")
      .references(() => mediaResearchCandidates.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    sequence: integer("sequence").default(1).notNull(),
    eventType: varchar("event_type", { length: 16 }).notNull(),
    fromStatus: varchar("from_status", { length: 16 }),
    toStatus: varchar("to_status", { length: 16 }).notNull(),
    reason: text("reason"),
    candidateSnapshotJson: json("candidate_snapshot").default({}).notNull(),
    candidateHash: varchar("candidate_hash", { length: 64 }).notNull(),
    actorId: uuid("actor_id").references(() => users.id, {
      onDelete: "set null",
    }),
    actorType: varchar("actor_type", { length: 16 }).default("system").notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
    requestHash: varchar("request_hash", { length: 64 }).notNull(),
    decisionHash: varchar("decision_hash", { length: 64 }).notNull(),
    prevEventHash: varchar("prev_event_hash", { length: 64 }),
    eventHash: varchar("event_hash", { length: 64 }).notNull(),
    contentItemId: uuid("content_item_id").references(() => mediaContentItems.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_research_candidate_decisions_event_type",
      sql`${table.eventType} in ('triage', 'accept', 'reject', 'expire', 'promote')`,
    ),
    check(
      "ck_media_research_candidate_decisions_from_status",
      sql`${table.fromStatus} is null or ${table.fromStatus} in ('discovered', 'triaged', 'accepted')`,
    ),
    check(
      "ck_media_research_candidate_decisions_to_status",
      sql`${table.toStatus} in ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')`,
    ),
    check(
      "ck_media_research_candidate_decisions_event_target",
      sql`(${table.eventType} = 'triage' and ${table.toStatus} = 'triaged') or (${table.eventType} = 'accept' and ${table.toStatus} = 'accepted') or (${table.eventType} = 'reject' and ${table.toStatus} = 'rejected') or (${table.eventType} = 'expire' and ${table.toStatus} = 'expired') or (${table.eventType} = 'promote' and ${table.toStatus} = 'promoted')`,
    ),
    check(
      "ck_media_research_candidate_decisions_actor_type",
      sql`${table.actorType} in ('human', 'agent', 'system', 'admin', 'unknown')`,
    ),
    check(
      "ck_media_research_candidate_decisions_reason",
      sql`${table.eventType} not in ('accept', 'reject') or (length(trim(coalesce(${table.reason}, ''))) > 0 and ${table.actorType} in ('human', 'admin'))`,
    ),
    check(
      "ck_media_research_candidate_decisions_promotion_content",
      sql`${table.eventType} <> 'promote' or ${table.contentItemId} is not null`,
    ),
    check(
      "ck_media_research_candidate_decisions_sequence",
      sql`${table.sequence} > 0`,
    ),
    check(
      "ck_media_research_candidate_decisions_candidate_hash",
      sql`length(${table.candidateHash}) = 64`,
    ),
    check(
      "ck_media_research_candidate_decisions_request_hash",
      sql`length(${table.requestHash}) = 64`,
    ),
    check(
      "ck_media_research_candidate_decisions_decision_hash",
      sql`length(${table.decisionHash}) = 64`,
    ),
    check(
      "ck_media_research_candidate_decisions_prev_event_hash",
      sql`${table.prevEventHash} is null or length(${table.prevEventHash}) = 64`,
    ),
    check(
      "ck_media_research_candidate_decisions_event_hash",
      sql`length(${table.eventHash}) = 64`,
    ),
    unique("uq_media_research_candidate_decisions_sequence").on(
      table.candidateId,
      table.sequence,
    ),
    unique("uq_media_research_candidate_decisions_idempotency").on(
      table.candidateId,
      table.idempotencyKey,
    ),
    index("ix_media_research_candidate_decisions_candidate_id").on(
      table.candidateId,
    ),
    index("ix_media_research_candidate_decisions_owner_user_id").on(
      table.ownerUserId,
    ),
    index("ix_media_research_candidate_decisions_project_id").on(table.projectId),
    index("ix_media_research_candidate_decisions_candidate_created").on(
      table.candidateId,
      table.createdAt,
    ),
    index("ix_media_research_candidate_decisions_event_type").on(table.eventType),
    index("ix_media_research_candidate_decisions_candidate_hash").on(
      table.candidateHash,
    ),
    index("ix_media_research_candidate_decisions_decision_hash").on(
      table.decisionHash,
    ),
    index("ix_media_research_candidate_decisions_event_hash").on(table.eventHash),
    index("ix_media_research_candidate_decisions_content_item_id").on(
      table.contentItemId,
    ),
    index("ix_media_research_candidate_decisions_created_at").on(table.createdAt),
    index("ix_media_research_candidate_decisions_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
  ],
);

export const mediaContentItemFindings = pgTable("media_content_item_findings", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  contentItemId: uuid("content_item_id")
    .references(() => mediaContentItems.id, { onDelete: "cascade" })
    .notNull(),
  findingId: uuid("finding_id")
    .references(() => mediaResearchFindings.id, { onDelete: "cascade" })
    .notNull(),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  ordinal: integer("ordinal").notNull(),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_content_item_findings_ordinal",
    sql`${table.ordinal} >= 1 and ${table.ordinal} <= 20`,
  ),
  unique("uq_media_content_item_findings_finding").on(
    table.contentItemId,
    table.findingId,
  ),
  unique("uq_media_content_item_findings_ordinal").on(
    table.contentItemId,
    table.ordinal,
  ),
  index("ix_media_content_item_findings_content_item_id").on(
    table.contentItemId,
  ),
  index("ix_media_content_item_findings_finding_id").on(table.findingId),
  index("ix_media_content_item_findings_owner_user_id").on(table.ownerUserId),
  index("ix_media_content_item_findings_project_id").on(table.projectId),
  index("ix_media_content_item_findings_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
]);

// ─── Typed Media Operations: Content Variants / QA / Rights ───
// Mirrors alembic/versions/20260901_0009_media_operations_content.py.  A
// ContentVariant is a stable identity; editable platform payloads live only
// in append-only revisions.  JSON fields below contain bounded typed semantic
// values (never provider credentials, raw requests, or arbitrary commands).

export const mediaContentVariants = pgTable("media_content_variants", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  projectId: uuid("project_id").references(() => projects.id, {
    onDelete: "cascade",
  }),
  contentItemId: uuid("content_item_id")
    .references(() => mediaContentItems.id, { onDelete: "cascade" })
    .notNull(),
  platform: varchar("platform", { length: 16 }).notNull(),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, {
    onDelete: "set null",
  }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check(
    "ck_media_content_variants_platform",
    sql`${table.platform} in ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')`,
  ),
  check(
    "ck_media_content_variants_create_hash",
    sql`length(${table.createHash}) = 64`,
  ),
  index("ix_media_content_variants_owner_user_id").on(table.ownerUserId),
  index("ix_media_content_variants_project_id").on(table.projectId),
  index("ix_media_content_variants_content_item_id").on(table.contentItemId),
  index("ix_media_content_variants_platform").on(table.platform),
  index("ix_media_content_variants_create_hash").on(table.createHash),
  index("ix_media_content_variants_created_at").on(table.createdAt),
  index("ix_media_content_variants_owner_project").on(
    table.ownerUserId,
    table.projectId,
  ),
  uniqueIndex("uq_media_content_variants_personal_identity")
    .on(table.ownerUserId, table.contentItemId, table.platform)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_content_variants_project_identity")
    .on(table.projectId, table.contentItemId, table.platform)
    .where(sql`${table.projectId} is not null`),
  uniqueIndex("uq_media_content_variants_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_content_variants_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaContentVariantRevisions = pgTable(
  "media_content_variant_revisions",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    contentVariantId: uuid("content_variant_id")
      .references(() => mediaContentVariants.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    version: integer("version").notNull(),
    contentItemId: uuid("content_item_id")
      .references(() => mediaContentItems.id, { onDelete: "cascade" })
      .notNull(),
    contentItemHash: varchar("content_item_hash", { length: 64 }).notNull(),
    personaRevisionId: uuid("persona_revision_id")
      .references(() => mediaPersonaRevisions.id, { onDelete: "cascade" })
      .notNull(),
    personaRevisionHash: varchar("persona_revision_hash", { length: 64 }).notNull(),
    platformAccountId: uuid("platform_account_id").references(
      () => mediaPlatformAccounts.id,
      { onDelete: "cascade" },
    ),
    platformAccountRevisionId: uuid("platform_account_revision_id").references(
      () => mediaPlatformAccountRevisions.id,
      { onDelete: "cascade" },
    ),
    platformAccountRevisionHash: varchar("platform_account_revision_hash", {
      length: 64,
    }),
    platform: varchar("platform", { length: 16 }).notNull(),
    payload: json("payload").notNull(),
    generationOutputRefs: json("generation_output_refs")
      .default([])
      .notNull(),
    sourceEvidence: json("source_evidence").default([]).notNull(),
    contentHash: varchar("content_hash", { length: 64 }).notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_content_variant_revisions_version",
      sql`${table.version} > 0`,
    ),
    check(
      "ck_media_content_variant_revisions_platform",
      sql`${table.platform} in ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')`,
    ),
    check(
      "ck_media_content_variant_revisions_content_item_hash",
      sql`length(${table.contentItemHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_revisions_persona_hash",
      sql`length(${table.personaRevisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_revisions_account_hash",
      sql`${table.platformAccountRevisionHash} is null or length(${table.platformAccountRevisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_revisions_hash",
      sql`length(${table.contentHash}) = 64`,
    ),
    unique("uq_media_content_variant_revisions_version").on(
      table.contentVariantId,
      table.version,
    ),
    unique("uq_media_content_variant_revisions_idempotency").on(
      table.contentVariantId,
      table.idempotencyKey,
    ),
    index("ix_media_content_variant_revisions_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
    index("ix_media_content_variant_revisions_content_variant_id").on(
      table.contentVariantId,
    ),
    index("ix_media_content_variant_revisions_persona_revision_id").on(
      table.personaRevisionId,
    ),
    index("ix_media_content_variant_revisions_platform_account_id").on(
      table.platformAccountId,
    ),
    index("ix_media_content_variant_revisions_content_hash").on(
      table.contentHash,
    ),
    index("ix_media_content_variant_revisions_owner_user_id").on(table.ownerUserId),
    index("ix_media_content_variant_revisions_project_id").on(table.projectId),
    index("ix_media_content_variant_revisions_content_item_id").on(table.contentItemId),
    index("ix_media_content_variant_revisions_content_item_hash").on(table.contentItemHash),
    index("ix_media_content_variant_revisions_persona_revision_hash").on(table.personaRevisionHash),
    index("ix_media_content_variant_revisions_platform_account_revision_id").on(table.platformAccountRevisionId),
    index("ix_media_content_variant_revisions_platform_account_revision_hash").on(table.platformAccountRevisionHash),
    index("ix_media_content_variant_revisions_platform").on(table.platform),
    index("ix_media_content_variant_revisions_created_at").on(table.createdAt),
  ],
);

export const mediaContentVariantQaAssessments = pgTable(
  "media_content_variant_qa_assessments",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    contentVariantId: uuid("content_variant_id")
      .references(() => mediaContentVariants.id, { onDelete: "cascade" })
      .notNull(),
    contentVariantRevisionId: uuid("content_variant_revision_id")
      .references(() => mediaContentVariantRevisions.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    revisionHash: varchar("revision_hash", { length: 64 }).notNull(),
    policyRevisionId: uuid("policy_revision_id"),
    policyRevisionHash: varchar("policy_revision_hash", { length: 64 }).notNull(),
    result: varchar("result", { length: 24 }).notNull(),
    checks: json("checks").default([]).notNull(),
    findings: json("findings").default([]).notNull(),
    evidence: json("evidence").default([]).notNull(),
    assessmentHash: varchar("assessment_hash", { length: 64 }).notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_content_variant_qa_result",
      sql`${table.result} in ('passed', 'failed', 'review_required')`,
    ),
    check(
      "ck_media_content_variant_qa_revision_hash",
      sql`length(${table.revisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_qa_policy_hash",
      sql`length(${table.policyRevisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_qa_hash",
      sql`length(${table.assessmentHash}) = 64`,
    ),
    unique("uq_media_content_variant_qa_assessment_hash").on(
      table.contentVariantRevisionId,
      table.assessmentHash,
    ),
    unique("uq_media_content_variant_qa_idempotency").on(
      table.contentVariantRevisionId,
      table.idempotencyKey,
    ),
    index("ix_media_content_variant_qa_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
    index("ix_media_content_variant_qa_assessments_content_variant_id").on(table.contentVariantId),
    index("ix_media_content_variant_qa_assessments_content_variant_revision_id").on(table.contentVariantRevisionId),
    index("ix_media_content_variant_qa_assessments_owner_user_id").on(table.ownerUserId),
    index("ix_media_content_variant_qa_assessments_project_id").on(table.projectId),
    index("ix_media_content_variant_qa_assessments_revision_hash").on(table.revisionHash),
    index("ix_media_content_variant_qa_assessments_policy_revision_id").on(table.policyRevisionId),
    index("ix_media_content_variant_qa_assessments_policy_revision_hash").on(table.policyRevisionHash),
    index("ix_media_content_variant_qa_assessments_result").on(table.result),
    index("ix_media_content_variant_qa_assessments_assessment_hash").on(table.assessmentHash),
    index("ix_media_content_variant_qa_assessments_idempotency_key").on(table.idempotencyKey),
    index("ix_media_content_variant_qa_assessments_created_at").on(table.createdAt),
  ],
);

export const mediaContentVariantRightsAssessments = pgTable(
  "media_content_variant_rights_assessments",
  {
    id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
    contentVariantId: uuid("content_variant_id")
      .references(() => mediaContentVariants.id, { onDelete: "cascade" })
      .notNull(),
    contentVariantRevisionId: uuid("content_variant_revision_id")
      .references(() => mediaContentVariantRevisions.id, { onDelete: "cascade" })
      .notNull(),
    ownerUserId: uuid("owner_user_id")
      .references(() => users.id, { onDelete: "cascade" })
      .notNull(),
    projectId: uuid("project_id").references(() => projects.id, {
      onDelete: "cascade",
    }),
    revisionHash: varchar("revision_hash", { length: 64 }).notNull(),
    policyRevisionId: uuid("policy_revision_id"),
    policyRevisionHash: varchar("policy_revision_hash", { length: 64 }).notNull(),
    result: varchar("result", { length: 24 }).notNull(),
    checks: json("checks").default([]).notNull(),
    findings: json("findings").default([]).notNull(),
    evidence: json("evidence").default([]).notNull(),
    assessmentHash: varchar("assessment_hash", { length: 64 }).notNull(),
    idempotencyKey: varchar("idempotency_key", { length: 255 }),
    createdBy: uuid("created_by").references(() => users.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").notNull(),
  },
  (table) => [
    check(
      "ck_media_content_variant_rights_result",
      sql`${table.result} in ('cleared', 'blocked', 'review_required')`,
    ),
    check(
      "ck_media_content_variant_rights_revision_hash",
      sql`length(${table.revisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_rights_policy_hash",
      sql`length(${table.policyRevisionHash}) = 64`,
    ),
    check(
      "ck_media_content_variant_rights_hash",
      sql`length(${table.assessmentHash}) = 64`,
    ),
    unique("uq_media_content_variant_rights_assessment_hash").on(
      table.contentVariantRevisionId,
      table.assessmentHash,
    ),
    unique("uq_media_content_variant_rights_idempotency").on(
      table.contentVariantRevisionId,
      table.idempotencyKey,
    ),
    index("ix_media_content_variant_rights_owner_project").on(
      table.ownerUserId,
      table.projectId,
    ),
    index("ix_media_content_variant_rights_assessments_content_variant_id").on(table.contentVariantId),
    index("ix_media_content_variant_rights_assessments_content_variant_revision_id").on(table.contentVariantRevisionId),
    index("ix_media_content_variant_rights_assessments_owner_user_id").on(table.ownerUserId),
    index("ix_media_content_variant_rights_assessments_project_id").on(table.projectId),
    index("ix_media_content_variant_rights_assessments_revision_hash").on(table.revisionHash),
    index("ix_media_content_variant_rights_assessments_policy_revision_id").on(table.policyRevisionId),
    index("ix_media_content_variant_rights_assessments_policy_revision_hash").on(table.policyRevisionHash),
    index("ix_media_content_variant_rights_assessments_result").on(table.result),
    index("ix_media_content_variant_rights_assessments_assessment_hash").on(table.assessmentHash),
    index("ix_media_content_variant_rights_assessments_idempotency_key").on(table.idempotencyKey),
    index("ix_media_content_variant_rights_assessments_created_at").on(table.createdAt),
  ],
);

export type MediaContentVariant = typeof mediaContentVariants.$inferSelect;
export type NewMediaContentVariant = typeof mediaContentVariants.$inferInsert;
export type MediaContentVariantRevision = typeof mediaContentVariantRevisions.$inferSelect;
export type NewMediaContentVariantRevision = typeof mediaContentVariantRevisions.$inferInsert;
export type MediaContentVariantQaAssessment = typeof mediaContentVariantQaAssessments.$inferSelect;
export type NewMediaContentVariantQaAssessment = typeof mediaContentVariantQaAssessments.$inferInsert;
export type MediaContentVariantRightsAssessment = typeof mediaContentVariantRightsAssessments.$inferSelect;
export type NewMediaContentVariantRightsAssessment = typeof mediaContentVariantRightsAssessments.$inferInsert;


// ─── Typed Media Operations: Metrics / Experiments / Learning ───
// Mirrors alembic/versions/20260901_0011_media_operations_metrics_learning.py.
// These tables are an append-only, provider-neutral evidence ledger.  They
// intentionally contain normalized values and opaque references only; raw
// provider responses, credentials and publication side effects do not belong
// in this client-owned mirror.

export const mediaMetricSnapshots = pgTable("media_metric_snapshots", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  personaRef: varchar("persona_ref", { length: 164 }),
  platformAccountRef: varchar("platform_account_ref", { length: 164 }),
  contentVariantRef: varchar("content_variant_ref", { length: 164 }),
  publicationRef: varchar("publication_ref", { length: 164 }),
  periodStart: timestamp("period_start"),
  periodEnd: timestamp("period_end"),
  observedAt: timestamp("observed_at").notNull(),
  source: varchar("source", { length: 16 }).notNull(),
  provider: varchar("provider", { length: 128 }).notNull().default("manual"),
  normalizedMetrics: json("normalized_metrics").notNull().default({}),
  platformMetrics: json("platform_metrics").notNull().default({}),
  provenance: json("provenance").notNull().default([]),
  completeness: varchar("completeness", { length: 16 }).notNull().default("unknown"),
  ingestionStatus: varchar("ingestion_status", { length: 16 }).notNull().default("accepted"),
  correctionOfId: uuid("correction_of_id"),
  snapshotHash: varchar("snapshot_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_metric_snapshots_source", sql`${table.source} in ('manual', 'imported', 'api')`),
  check("ck_media_metric_snapshots_completeness", sql`${table.completeness} in ('complete', 'partial', 'unknown')`),
  check("ck_media_metric_snapshots_ingestion_status", sql`${table.ingestionStatus} in ('accepted', 'rejected', 'superseded')`),
  check("ck_media_metric_snapshots_hash", sql`length(${table.snapshotHash}) = 64`),
  index("ix_media_metric_snapshots_owner_user_id").on(table.ownerUserId),
  index("ix_media_metric_snapshots_project_id").on(table.projectId),
  index("ix_media_metric_snapshots_persona_ref").on(table.personaRef),
  index("ix_media_metric_snapshots_platform_account_ref").on(table.platformAccountRef),
  index("ix_media_metric_snapshots_content_variant_ref").on(table.contentVariantRef),
  index("ix_media_metric_snapshots_publication_ref").on(table.publicationRef),
  index("ix_media_metric_snapshots_period_start").on(table.periodStart),
  index("ix_media_metric_snapshots_period_end").on(table.periodEnd),
  index("ix_media_metric_snapshots_observed_at").on(table.observedAt),
  index("ix_media_metric_snapshots_source").on(table.source),
  index("ix_media_metric_snapshots_correction_of_id").on(table.correctionOfId),
  index("ix_media_metric_snapshots_snapshot_hash").on(table.snapshotHash),
  index("ix_media_metric_snapshots_created_at").on(table.createdAt),
  index("ix_media_metric_snapshots_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_metric_snapshots_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_metric_snapshots_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaMetricIngestionRuns = pgTable("media_metric_ingestion_runs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  provider: varchar("provider", { length: 16 }).notNull(),
  platformAccountId: uuid("platform_account_id").references(() => mediaPlatformAccounts.id, { onDelete: "restrict" }),
  platformAccountRef: varchar("platform_account_ref", { length: 164 }),
  windowStart: timestamp("window_start"),
  windowEnd: timestamp("window_end"),
  cursor: varchar("cursor", { length: 512 }),
  checkpoint: json("checkpoint").notNull().default({}),
  status: varchar("status", { length: 16 }).notNull().default("pending"),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  requestHash: varchar("request_hash", { length: 64 }).notNull(),
  observationHash: varchar("observation_hash", { length: 64 }).notNull(),
  evidence: json("evidence").notNull().default([]),
  platformAccountRevisionId: uuid("platform_account_revision_id").references(() => mediaPlatformAccountRevisions.id, { onDelete: "restrict" }),
  platformAccountRevisionHash: varchar("platform_account_revision_hash", { length: 64 }),
  credentialStateHash: varchar("credential_state_hash", { length: 64 }),
  capabilitySnapshotId: uuid("capability_snapshot_id").references(() => mediaProviderCapabilitySnapshots.id, { onDelete: "restrict" }),
  capabilitySnapshotHash: varchar("capability_snapshot_hash", { length: 64 }),
  externalActionReceiptRef: varchar("external_action_receipt_ref", { length: 164 }),
  remoteRef: varchar("remote_ref", { length: 164 }),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_metric_ingestion_runs_provider", sql`${table.provider} in ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')`),
  check("ck_media_metric_ingestion_runs_status", sql`${table.status} in ('pending', 'running', 'succeeded', 'partial', 'failed', 'uncertain')`),
  check("ck_media_metric_ingestion_runs_request_hash", sql`length(${table.requestHash}) = 64`),
  check("ck_media_metric_ingestion_runs_observation_hash", sql`length(${table.observationHash}) = 64`),
  check("ck_media_metric_ingestion_runs_account_revision_hash", sql`${table.platformAccountRevisionHash} is null or length(${table.platformAccountRevisionHash}) = 64`),
  check("ck_media_metric_ingestion_runs_credential_state_hash", sql`${table.credentialStateHash} is null or length(${table.credentialStateHash}) = 64`),
  check("ck_media_metric_ingestion_runs_capability_snapshot_hash", sql`${table.capabilitySnapshotHash} is null or length(${table.capabilitySnapshotHash}) = 64`),
  index("ix_media_metric_ingestion_runs_owner_user_id").on(table.ownerUserId),
  index("ix_media_metric_ingestion_runs_project_id").on(table.projectId),
  index("ix_media_metric_ingestion_runs_provider").on(table.provider),
  index("ix_media_metric_ingestion_runs_platform_account_id").on(table.platformAccountId),
  index("ix_media_metric_ingestion_runs_platform_account_ref").on(table.platformAccountRef),
  index("ix_media_metric_ingestion_runs_window_start").on(table.windowStart),
  index("ix_media_metric_ingestion_runs_window_end").on(table.windowEnd),
  index("ix_media_metric_ingestion_runs_status").on(table.status),
  index("ix_media_metric_ingestion_runs_request_hash").on(table.requestHash),
  index("ix_media_metric_ingestion_runs_observation_hash").on(table.observationHash),
  index("ix_media_metric_ingestion_runs_platform_account_revision_id").on(table.platformAccountRevisionId),
  index("ix_media_metric_ingestion_runs_capability_snapshot_id").on(table.capabilitySnapshotId),
  index("ix_media_metric_ingestion_runs_remote_ref").on(table.remoteRef),
  index("ix_media_metric_ingestion_runs_created_at").on(table.createdAt),
  index("ix_media_metric_ingestion_runs_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_metric_ingestion_runs_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_metric_ingestion_runs_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaExperiments = pgTable("media_experiments", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  name: varchar("name", { length: 255 }).notNull(),
  hypothesis: text("hypothesis").notNull(),
  personaRefs: json("persona_refs").notNull().default([]),
  accountRefs: json("account_refs").notNull().default([]),
  variantGroups: json("variant_groups").notNull().default([]),
  primaryMetric: varchar("primary_metric", { length: 64 }).notNull(),
  secondaryMetrics: json("secondary_metrics").notNull().default([]),
  windowStart: timestamp("window_start").notNull(),
  windowEnd: timestamp("window_end").notNull(),
  minimumSampleSize: integer("minimum_sample_size").notNull().default(1),
  status: varchar("status", { length: 16 }).notNull().default("draft"),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check("ck_media_experiments_status", sql`${table.status} in ('draft', 'running', 'completed', 'inconclusive', 'cancelled')`),
  check("ck_media_experiments_sample_size", sql`${table.minimumSampleSize} >= 1 and ${table.minimumSampleSize} <= 10000000`),
  check("ck_media_experiments_create_hash", sql`length(${table.createHash}) = 64`),
  index("ix_media_experiments_owner_user_id").on(table.ownerUserId),
  index("ix_media_experiments_project_id").on(table.projectId),
  index("ix_media_experiments_status").on(table.status),
  index("ix_media_experiments_create_hash").on(table.createHash),
  index("ix_media_experiments_created_at").on(table.createdAt),
  index("ix_media_experiments_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_experiments_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_experiments_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaExperimentResults = pgTable("media_experiment_results", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  experimentId: uuid("experiment_id").references(() => mediaExperiments.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  status: varchar("status", { length: 16 }).notNull().default("inconclusive"),
  sampleSize: integer("sample_size").notNull(),
  sampleSizes: json("sample_sizes").notNull().default({}),
  groupMetrics: json("group_metrics").notNull().default({}),
  winnerVariantRef: varchar("winner_variant_ref", { length: 164 }),
  confidence: doublePrecision("confidence").notNull(),
  uncertainty: doublePrecision("uncertainty").notNull(),
  evidenceRefs: json("evidence_refs").notNull().default([]),
  analysisMethod: varchar("analysis_method", { length: 64 }).notNull().default("normalized_metric_comparison"),
  analysisVersion: varchar("analysis_version", { length: 32 }).notNull().default("1"),
  analysisDesign: varchar("analysis_design", { length: 16 }).notNull().default("observational"),
  assignmentEvidence: json("assignment_evidence").notNull().default([]),
  exposureEvidence: json("exposure_evidence").notNull().default([]),
  conclusion: text("conclusion").notNull(),
  resultHash: varchar("result_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_experiment_results_status", sql`${table.status} in ('complete', 'inconclusive')`),
  check("ck_media_experiment_results_sample_size", sql`${table.sampleSize} >= 0 and ${table.sampleSize} <= 10000000`),
  check("ck_media_experiment_results_confidence", sql`${table.confidence} >= 0 and ${table.confidence} <= 1`),
  check("ck_media_experiment_results_uncertainty", sql`${table.uncertainty} >= 0 and ${table.uncertainty} <= 1`),
  check("ck_media_experiment_results_hash", sql`length(${table.resultHash}) = 64`),
  check("ck_media_experiment_results_analysis_method", sql`length(trim(${table.analysisMethod})) > 0`),
  check("ck_media_experiment_results_analysis_version", sql`length(trim(${table.analysisVersion})) > 0`),
  check("ck_media_experiment_results_analysis_design", sql`${table.analysisDesign} in ('controlled', 'observational')`),
  unique("uq_media_experiment_results_idempotency").on(table.experimentId, table.idempotencyKey),
  index("ix_media_experiment_results_experiment_id").on(table.experimentId),
  index("ix_media_experiment_results_owner_user_id").on(table.ownerUserId),
  index("ix_media_experiment_results_project_id").on(table.projectId),
  index("ix_media_experiment_results_result_hash").on(table.resultHash),
  index("ix_media_experiment_results_created_at").on(table.createdAt),
  index("ix_media_experiment_results_owner_project").on(table.ownerUserId, table.projectId),
]);

/** Exact MetricSnapshot inputs used for one immutable experiment result. */
export const mediaExperimentResultMetricInputs = pgTable("media_experiment_result_metric_inputs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  experimentResultId: uuid("experiment_result_id")
    .references(() => mediaExperimentResults.id, { onDelete: "cascade" })
    .notNull(),
  metricSnapshotId: uuid("metric_snapshot_id")
    .references(() => mediaMetricSnapshots.id, { onDelete: "restrict" })
    .notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  metricSnapshotHash: varchar("metric_snapshot_hash", { length: 64 }).notNull(),
  groupName: varchar("group_name", { length: 64 }).notNull(),
  variantRef: varchar("variant_ref", { length: 164 }),
  ordinal: integer("ordinal").notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_experiment_result_metric_inputs_snapshot_hash", sql`length(${table.metricSnapshotHash}) = 64`),
  check("ck_media_experiment_result_metric_inputs_group_name", sql`length(trim(${table.groupName})) > 0`),
  check("ck_media_experiment_result_metric_inputs_variant_ref", sql`${table.variantRef} is null or length(trim(${table.variantRef})) > 0`),
  check("ck_media_experiment_result_metric_inputs_ordinal", sql`${table.ordinal} >= 1 and ${table.ordinal} <= 1000000`),
  unique("uq_media_experiment_result_metric_inputs_ordinal").on(table.experimentResultId, table.ordinal),
  unique("uq_media_experiment_result_metric_inputs_snapshot").on(
    table.experimentResultId,
    table.metricSnapshotId,
  ),
  index("ix_media_experiment_result_inputs_result_id").on(table.experimentResultId),
  index("ix_media_experiment_result_inputs_snapshot_id").on(table.metricSnapshotId),
  index("ix_media_experiment_result_inputs_owner_id").on(table.ownerUserId),
  index("ix_media_experiment_result_inputs_project_id").on(table.projectId),
  index("ix_media_experiment_result_inputs_snapshot_hash").on(table.metricSnapshotHash),
  index("ix_media_experiment_result_inputs_group_name").on(table.groupName),
  index("ix_media_experiment_result_inputs_variant_ref").on(table.variantRef),
  index("ix_media_experiment_result_inputs_created_at").on(table.createdAt),
  index("ix_media_experiment_result_inputs_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaRevenueEvents = pgTable("media_revenue_events", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  personaRef: varchar("persona_ref", { length: 164 }),
  platformAccountRef: varchar("platform_account_ref", { length: 164 }),
  platform: varchar("platform", { length: 32 }),
  contentRef: varchar("content_ref", { length: 164 }),
  publicationRef: varchar("publication_ref", { length: 164 }),
  productRef: varchar("product_ref", { length: 164 }),
  source: varchar("source", { length: 16 }).notNull(),
  provider: varchar("provider", { length: 128 }).notNull().default("manual"),
  eventType: varchar("event_type", { length: 16 }).notNull(),
  grossAmount: doublePrecision("gross_amount").notNull(),
  netAmount: doublePrecision("net_amount").notNull(),
  currency: varchar("currency", { length: 3 }).notNull(),
  eventAt: timestamp("event_at").notNull(),
  settlementAt: timestamp("settlement_at"),
  evidence: json("evidence").notNull().default([]),
  correctionOfId: uuid("correction_of_id"),
  eventHash: varchar("event_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_revenue_events_source", sql`${table.source} in ('manual', 'imported', 'api')`),
  check("ck_media_revenue_events_type", sql`${table.eventType} in ('sale', 'refund', 'chargeback', 'adjustment', 'reversal')`),
  check("ck_media_revenue_events_currency", sql`length(${table.currency}) = 3`),
  check("ck_media_revenue_events_hash", sql`length(${table.eventHash}) = 64`),
  index("ix_media_revenue_events_owner_user_id").on(table.ownerUserId),
  index("ix_media_revenue_events_project_id").on(table.projectId),
  index("ix_media_revenue_events_persona_ref").on(table.personaRef),
  index("ix_media_revenue_events_platform_account_ref").on(table.platformAccountRef),
  index("ix_media_revenue_events_platform").on(table.platform),
  index("ix_media_revenue_events_content_ref").on(table.contentRef),
  index("ix_media_revenue_events_publication_ref").on(table.publicationRef),
  index("ix_media_revenue_events_product_ref").on(table.productRef),
  index("ix_media_revenue_events_source").on(table.source),
  index("ix_media_revenue_events_event_type").on(table.eventType),
  index("ix_media_revenue_events_event_at").on(table.eventAt),
  index("ix_media_revenue_events_settlement_at").on(table.settlementAt),
  index("ix_media_revenue_events_correction_of_id").on(table.correctionOfId),
  index("ix_media_revenue_events_event_hash").on(table.eventHash),
  index("ix_media_revenue_events_created_at").on(table.createdAt),
  index("ix_media_revenue_events_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_revenue_events_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_revenue_events_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export const mediaLearningProposals = pgTable("media_learning_proposals", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  subjectType: varchar("subject_type", { length: 32 }).notNull(),
  subjectRef: varchar("subject_ref", { length: 164 }).notNull(),
  proposalType: varchar("proposal_type", { length: 32 }).notNull().default("learning"),
  title: varchar("title", { length: 255 }).notNull(),
  summary: text("summary").notNull(),
  recommendation: text("recommendation").notNull(),
  evidenceRefs: json("evidence_refs").notNull().default([]),
  humanDecisionRefs: json("human_decision_refs").notNull().default([]),
  targetFields: json("target_fields").notNull().default([]),
  proposedBefore: json("proposed_before").notNull().default({}),
  proposedAfter: json("proposed_after").notNull().default({}),
  expectedPersonaRevisionId: uuid("expected_persona_revision_id")
    .references(() => mediaPersonaRevisions.id, { onDelete: "set null" }),
  expectedPersonaRevisionVersion: integer("expected_persona_revision_version"),
  expectedPersonaRevisionHash: varchar("expected_persona_revision_hash", { length: 64 }),
  reviewHistory: json("review_history").notNull().default([]),
  appliedPersonaRevisionId: uuid("applied_persona_revision_id")
    .references(() => mediaPersonaRevisions.id, { onDelete: "set null" }),
  appliedPersonaRevisionVersion: integer("applied_persona_revision_version"),
  appliedPersonaRevisionHash: varchar("applied_persona_revision_hash", { length: 64 }),
  windowStart: timestamp("window_start").notNull(),
  windowEnd: timestamp("window_end").notNull(),
  confidence: doublePrecision("confidence").notNull(),
  uncertainty: doublePrecision("uncertainty").notNull(),
  humanReviewRequired: boolean("human_review_required").notNull().default(true),
  reviewPolicy: varchar("review_policy", { length: 64 }).notNull().default("human_review_before_apply"),
  status: varchar("status", { length: 16 }).notNull().default("pending_review"),
  proposalHash: varchar("proposal_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_learning_proposals_status", sql`${table.status} in ('pending_review', 'rejected', 'accepted', 'stale')`),
  check("ck_media_learning_proposals_confidence", sql`${table.confidence} >= 0 and ${table.confidence} <= 1`),
  check("ck_media_learning_proposals_uncertainty", sql`${table.uncertainty} >= 0 and ${table.uncertainty} <= 1`),
  check("ck_media_learning_proposals_human_review", sql`${table.humanReviewRequired} = true`),
  check("ck_media_learning_proposals_hash", sql`length(${table.proposalHash}) = 64`),
  check("ck_media_learning_proposals_expected_revision_hash", sql`${table.expectedPersonaRevisionHash} is null or length(${table.expectedPersonaRevisionHash}) = 64`),
  check("ck_media_learning_proposals_applied_revision_hash", sql`${table.appliedPersonaRevisionHash} is null or length(${table.appliedPersonaRevisionHash}) = 64`),
  index("ix_media_learning_proposals_owner_user_id").on(table.ownerUserId),
  index("ix_media_learning_proposals_project_id").on(table.projectId),
  index("ix_media_learning_proposals_status").on(table.status),
  index("ix_media_learning_proposals_proposal_hash").on(table.proposalHash),
  index("ix_media_learning_proposals_created_at").on(table.createdAt),
  index("ix_media_learning_proposals_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_learning_proposals_personal_idempotency")
    .on(table.ownerUserId, table.idempotencyKey)
    .where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_learning_proposals_project_idempotency")
    .on(table.projectId, table.idempotencyKey)
    .where(sql`${table.projectId} is not null`),
]);

export type MediaMetricSnapshot = typeof mediaMetricSnapshots.$inferSelect;
export type NewMediaMetricSnapshot = typeof mediaMetricSnapshots.$inferInsert;
export type MediaMetricIngestionRun = typeof mediaMetricIngestionRuns.$inferSelect;
export type NewMediaMetricIngestionRun = typeof mediaMetricIngestionRuns.$inferInsert;
export type MediaExperiment = typeof mediaExperiments.$inferSelect;
export type NewMediaExperiment = typeof mediaExperiments.$inferInsert;
export type MediaExperimentResult = typeof mediaExperimentResults.$inferSelect;
export type NewMediaExperimentResult = typeof mediaExperimentResults.$inferInsert;
export type MediaExperimentResultMetricInput = typeof mediaExperimentResultMetricInputs.$inferSelect;
export type NewMediaExperimentResultMetricInput = typeof mediaExperimentResultMetricInputs.$inferInsert;
export type MediaRevenueEvent = typeof mediaRevenueEvents.$inferSelect;
export type NewMediaRevenueEvent = typeof mediaRevenueEvents.$inferInsert;
export type MediaLearningProposal = typeof mediaLearningProposals.$inferSelect;
export type NewMediaLearningProposal = typeof mediaLearningProposals.$inferInsert;


// ─── Typed Media Operations: Generation Studio ───
// Mirrors the immutable semantic request / opaque receipt boundary. Provider
// graphs, credentials, filesystem paths, and raw responses are intentionally
// absent from these tables.

export const mediaGenerationWorkspaces = pgTable("media_generation_workspaces", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  provider: varchar("provider", { length: 64 }).default("comfyui_workbench").notNull(),
  externalWorkspaceId: varchar("external_workspace_id", { length: 164 }).notNull(),
  externalProjectId: varchar("external_project_id", { length: 164 }),
  baseUrl: varchar("base_url", { length: 512 }).notNull(),
  status: varchar("status", { length: 16 }).default("configured").notNull(),
  configHash: varchar("config_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_generation_workspaces_provider", sql`${table.provider} = 'comfyui_workbench'`),
  check("ck_media_generation_workspaces_workspace_ref", sql`${table.externalWorkspaceId} like 'wsp_%'`),
  check("ck_media_generation_workspaces_project_ref", sql`${table.externalProjectId} is null or ${table.externalProjectId} like 'prj_%'`),
  check("ck_media_generation_workspaces_status", sql`${table.status} in ('configured', 'verified', 'unavailable')`),
  check("ck_media_generation_workspaces_hash", sql`length(${table.configHash}) = 64`),
  index("ix_media_generation_workspaces_owner_user_id").on(table.ownerUserId),
  index("ix_media_generation_workspaces_project_id").on(table.projectId),
  index("ix_media_generation_workspaces_external_workspace_id").on(table.externalWorkspaceId),
  index("ix_media_generation_workspaces_external_project_id").on(table.externalProjectId),
  index("ix_media_generation_workspaces_status").on(table.status),
  index("ix_media_generation_workspaces_config_hash").on(table.configHash),
  index("ix_media_generation_workspaces_created_at").on(table.createdAt),
  index("ix_media_generation_workspaces_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_generation_workspaces_personal_identity").on(table.ownerUserId, table.externalWorkspaceId).where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_generation_workspaces_project_identity").on(table.projectId, table.externalWorkspaceId).where(sql`${table.projectId} is not null`),
  uniqueIndex("uq_media_generation_workspaces_personal_idempotency").on(table.ownerUserId, table.idempotencyKey).where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_generation_workspaces_project_idempotency").on(table.projectId, table.idempotencyKey).where(sql`${table.projectId} is not null`),
]);

export const mediaCreativeRecipes = pgTable("media_creative_recipes", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  personaId: uuid("persona_id").references(() => mediaPersonas.id, { onDelete: "cascade" }).notNull(),
  name: varchar("name", { length: 255 }).notNull().default("Untitled recipe"),
  createHash: varchar("create_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_creative_recipes_create_hash", sql`length(${table.createHash}) = 64`),
  index("ix_media_creative_recipes_owner_user_id").on(table.ownerUserId),
  index("ix_media_creative_recipes_project_id").on(table.projectId),
  index("ix_media_creative_recipes_persona_id").on(table.personaId),
  index("ix_media_creative_recipes_create_hash").on(table.createHash),
  index("ix_media_creative_recipes_owner_project").on(table.ownerUserId, table.projectId),
  uniqueIndex("uq_media_creative_recipes_personal_idempotency").on(table.ownerUserId, table.idempotencyKey).where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_creative_recipes_project_idempotency").on(table.projectId, table.idempotencyKey).where(sql`${table.projectId} is not null`),
]);

export const mediaCreativeRecipeRevisions = pgTable("media_creative_recipe_revisions", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  creativeRecipeId: uuid("creative_recipe_id").references(() => mediaCreativeRecipes.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  personaId: uuid("persona_id").references(() => mediaPersonas.id, { onDelete: "cascade" }).notNull(),
  personaRevisionId: uuid("persona_revision_id").references(() => mediaPersonaRevisions.id, { onDelete: "cascade" }),
  version: integer("version").notNull(),
  recipeType: varchar("recipe_type", { length: 24 }).notNull(),
  imageModelSelectionId: varchar("model_selection_id", { length: 255 }),
  workflowId: varchar("workflow_id", { length: 255 }),
  identityPackId: varchar("identity_pack_id", { length: 255 }),
  promptSchemaId: varchar("prompt_schema_id", { length: 255 }),
  promptTemplate: text("prompt_template").notNull(),
  negativeRequirements: text("negative_requirements"),
  referenceAssetIds: json("reference_asset_ids").default([]).notNull(),
  aspectRatio: varchar("aspect_ratio", { length: 32 }),
  width: integer("width"),
  height: integer("height"),
  durationSeconds: integer("duration_seconds"),
  frameCount: integer("frame_count"),
  storyboardJson: json("storyboard_json"),
  candidateCount: integer("candidate_count").default(1).notNull(),
  costPolicy: json("cost_policy").default({}).notNull(),
  provenanceRetentionPolicy: json("provenance_retention_policy").default({}).notNull(),
  humanReviewPolicy: json("human_review_policy").default({}).notNull(),
  contentHash: varchar("content_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_creative_recipe_revisions_version", sql`${table.version} > 0`),
  check("ck_media_creative_recipe_revisions_type", sql`${table.recipeType} in ('image', 'image_set', 'comic', 'video', 'thumbnail')`),
  check("ck_media_creative_recipe_revisions_hash", sql`length(${table.contentHash}) = 64`),
  check("ck_media_creative_recipe_revisions_candidates", sql`${table.candidateCount} >= 1 and ${table.candidateCount} <= 20`),
  check("ck_media_creative_recipe_revisions_width", sql`${table.width} is null or (${table.width} > 0 and ${table.width} <= 16384)`),
  check("ck_media_creative_recipe_revisions_height", sql`${table.height} is null or (${table.height} > 0 and ${table.height} <= 16384)`),
  check("ck_media_creative_recipe_revisions_duration", sql`${table.durationSeconds} is null or (${table.durationSeconds} > 0 and ${table.durationSeconds} <= 86400)`),
  check("ck_media_creative_recipe_revisions_frames", sql`${table.frameCount} is null or (${table.frameCount} > 0 and ${table.frameCount} <= 1000000)`),
  unique("uq_media_creative_recipe_revisions_version").on(table.creativeRecipeId, table.version),
  unique("uq_media_creative_recipe_revisions_idempotency").on(table.creativeRecipeId, table.idempotencyKey),
  index("ix_media_creative_recipe_revisions_recipe_id").on(table.creativeRecipeId),
  index("ix_media_creative_recipe_revisions_persona_revision_id").on(table.personaRevisionId),
  index("ix_media_creative_recipe_revisions_content_hash").on(table.contentHash),
  index("ix_media_creative_recipe_revisions_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationPlans = pgTable("media_generation_plans", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  personaRevisionId: uuid("persona_revision_id").references(() => mediaPersonaRevisions.id, { onDelete: "cascade" }).notNull(),
  personaRevisionHash: varchar("persona_revision_hash", { length: 64 }).notNull(),
  contentItemId: uuid("content_item_id").references(() => mediaContentItems.id, { onDelete: "set null" }),
  contentVariantId: uuid("content_variant_id"),
  creativeRecipeRevisionId: uuid("creative_recipe_revision_id").references(() => mediaCreativeRecipeRevisions.id, { onDelete: "cascade" }).notNull(),
  workspaceId: uuid("workspace_id").references(() => mediaGenerationWorkspaces.id, { onDelete: "cascade" }).notNull(),
  requestedOutputs: integer("requested_outputs").default(1).notNull(),
  requestSpec: json("request_spec").notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  planHash: varchar("plan_hash", { length: 64 }).notNull(),
  status: varchar("status", { length: 16 }).default("draft").notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_generation_plans_persona_hash", sql`length(${table.personaRevisionHash}) = 64`),
  check("ck_media_generation_plans_requested_outputs", sql`${table.requestedOutputs} >= 1 and ${table.requestedOutputs} <= 20`),
  check("ck_media_generation_plans_hash", sql`length(${table.planHash}) = 64`),
  check("ck_media_generation_plans_status", sql`${table.status} in ('draft', 'submitted', 'unavailable')`),
  uniqueIndex("uq_media_generation_plans_personal_idempotency").on(table.ownerUserId, table.idempotencyKey).where(sql`${table.projectId} is null`),
  uniqueIndex("uq_media_generation_plans_project_idempotency").on(table.projectId, table.idempotencyKey).where(sql`${table.projectId} is not null`),
  index("ix_media_generation_plans_owner_user_id").on(table.ownerUserId),
  index("ix_media_generation_plans_workspace_id").on(table.workspaceId),
  index("ix_media_generation_plans_status").on(table.status),
  index("ix_media_generation_plans_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationRunIntents = pgTable("media_generation_run_intents", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  planId: uuid("plan_id").references(() => mediaGenerationPlans.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  externalIdempotencyKey: varchar("external_idempotency_key", { length: 64 }).notNull(),
  requestHash: varchar("request_hash", { length: 64 }).notNull(),
  status: varchar("status", { length: 16 }).default("pending").notNull(),
  externalRunId: varchar("external_run_id", { length: 164 }),
  adapterRelease: varchar("adapter_release", { length: 128 }),
  errorCode: varchar("error_code", { length: 128 }),
  responseHash: varchar("response_hash", { length: 64 }),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
  updatedAt: timestamp("updated_at").notNull(),
}, (table) => [
  check("ck_media_generation_run_intents_idempotency", sql`length(${table.externalIdempotencyKey}) = 64`),
  check("ck_media_generation_run_intents_request_hash", sql`length(${table.requestHash}) = 64`),
  check("ck_media_generation_run_intents_status", sql`${table.status} in ('pending', 'submitted', 'unavailable', 'uncertain', 'failed')`),
  check("ck_media_generation_run_intents_response_hash", sql`${table.responseHash} is null or length(${table.responseHash}) = 64`),
  unique("uq_media_generation_run_intents_plan").on(table.planId),
  unique("uq_media_generation_run_intents_external_key").on(table.externalIdempotencyKey),
  index("ix_media_generation_run_intents_plan_id").on(table.planId),
  index("ix_media_generation_run_intents_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationRuns = pgTable("media_generation_runs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  planId: uuid("plan_id").references(() => mediaGenerationPlans.id, { onDelete: "cascade" }).notNull(),
  intentId: uuid("intent_id").references(() => mediaGenerationRunIntents.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  workspaceId: uuid("workspace_id").references(() => mediaGenerationWorkspaces.id, { onDelete: "cascade" }).notNull(),
  externalRunId: varchar("external_run_id", { length: 164 }).notNull(),
  externalWorkspaceId: varchar("external_workspace_id", { length: 164 }).notNull(),
  externalProjectId: varchar("external_project_id", { length: 164 }),
  status: varchar("status", { length: 16 }).notNull(),
  runHash: varchar("run_hash", { length: 64 }).notNull(),
  adapterRelease: varchar("adapter_release", { length: 128 }),
  startedAt: timestamp("started_at"),
  finishedAt: timestamp("finished_at"),
  costSummary: json("cost_summary").default({}).notNull(),
  errorCode: varchar("error_code", { length: 128 }),
  resultDeepLink: varchar("result_deep_link", { length: 2000 }),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_generation_runs_run_ref", sql`${table.externalRunId} like 'run_%'`),
  check("ck_media_generation_runs_workspace_ref", sql`${table.externalWorkspaceId} like 'wsp_%'`),
  check("ck_media_generation_runs_project_ref", sql`${table.externalProjectId} like 'prj_%'`),
  check("ck_media_generation_runs_status", sql`${table.status} in ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')`),
  check("ck_media_generation_runs_hash", sql`length(${table.runHash}) = 64`),
  unique("uq_media_generation_runs_workspace_external").on(table.workspaceId, table.externalRunId),
  index("ix_media_generation_runs_plan_id").on(table.planId),
  index("ix_media_generation_runs_workspace_id").on(table.workspaceId),
  index("ix_media_generation_runs_status").on(table.status),
  index("ix_media_generation_runs_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationRunObservations = pgTable("media_generation_run_observations", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  generationRunId: uuid("generation_run_id").references(() => mediaGenerationRuns.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  externalRunId: varchar("external_run_id", { length: 164 }).notNull(),
  status: varchar("status", { length: 16 }).notNull(),
  observationHash: varchar("observation_hash", { length: 64 }).notNull(),
  adapterRelease: varchar("adapter_release", { length: 128 }),
  costSummary: json("cost_summary").default({}).notNull(),
  errorCode: varchar("error_code", { length: 128 }),
  resultDeepLink: varchar("result_deep_link", { length: 2000 }),
  observedAt: timestamp("observed_at").notNull(),
}, (table) => [
  check("ck_media_generation_run_observations_run_ref", sql`${table.externalRunId} like 'run_%'`),
  check("ck_media_generation_run_observations_status", sql`${table.status} in ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')`),
  check("ck_media_generation_run_observations_hash", sql`length(${table.observationHash}) = 64`),
  unique("uq_media_generation_run_observations_hash").on(table.generationRunId, table.observationHash),
  index("ix_media_generation_run_observations_run_id").on(table.generationRunId),
  index("ix_media_generation_run_observations_external_run_id").on(table.externalRunId),
  index("ix_media_generation_run_observations_observation_hash").on(table.observationHash),
  index("ix_media_generation_run_observations_owner_user_id").on(table.ownerUserId),
  index("ix_media_generation_run_observations_project_id").on(table.projectId),
  index("ix_media_generation_run_observations_observed_at").on(table.observedAt),
  index("ix_media_generation_run_observations_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationOutputs = pgTable("media_generation_outputs", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  generationRunId: uuid("generation_run_id").references(() => mediaGenerationRuns.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  externalAssetId: varchar("external_asset_id", { length: 164 }).notNull(),
  externalOutputVersion: varchar("external_output_version", { length: 165 }).notNull(),
  sha256: varchar("sha256", { length: 64 }).notNull(),
  mimeType: varchar("mime_type", { length: 255 }).notNull(),
  width: integer("width"),
  height: integer("height"),
  deepLink: varchar("deep_link", { length: 2000 }),
  provenanceHash: varchar("provenance_hash", { length: 64 }).notNull(),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_generation_outputs_asset_ref", sql`${table.externalAssetId} like 'ast_%'`),
  check("ck_media_generation_outputs_version_ref", sql`${table.externalOutputVersion} like 'outv_%'`),
  check("ck_media_generation_outputs_sha256", sql`length(${table.sha256}) = 64`),
  check("ck_media_generation_outputs_provenance_hash", sql`length(${table.provenanceHash}) = 64`),
  unique("uq_media_generation_outputs_external").on(table.generationRunId, table.externalAssetId, table.externalOutputVersion),
  index("ix_media_generation_outputs_run_id").on(table.generationRunId),
  index("ix_media_generation_outputs_external_asset_id").on(table.externalAssetId),
  index("ix_media_generation_outputs_external_output_version").on(table.externalOutputVersion),
  index("ix_media_generation_outputs_sha256").on(table.sha256),
  index("ix_media_generation_outputs_provenance_hash").on(table.provenanceHash),
  index("ix_media_generation_outputs_owner_user_id").on(table.ownerUserId),
  index("ix_media_generation_outputs_project_id").on(table.projectId),
  index("ix_media_generation_outputs_created_at").on(table.createdAt),
  index("ix_media_generation_outputs_owner_project").on(table.ownerUserId, table.projectId),
]);

export const mediaGenerationOutputSelections = pgTable("media_generation_output_selections", {
  id: uuid("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  generationRunId: uuid("generation_run_id").references(() => mediaGenerationRuns.id, { onDelete: "cascade" }).notNull(),
  generationOutputId: uuid("generation_output_id").references(() => mediaGenerationOutputs.id, { onDelete: "cascade" }).notNull(),
  ownerUserId: uuid("owner_user_id").references(() => users.id, { onDelete: "cascade" }).notNull(),
  projectId: uuid("project_id").references(() => projects.id, { onDelete: "cascade" }),
  selectionHash: varchar("selection_hash", { length: 64 }).notNull(),
  idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
  createdBy: uuid("created_by").references(() => users.id, { onDelete: "set null" }),
  createdAt: timestamp("created_at").notNull(),
}, (table) => [
  check("ck_media_generation_output_selections_hash", sql`length(${table.selectionHash}) = 64`),
  unique("uq_media_generation_output_selections_idempotency").on(table.generationRunId, table.idempotencyKey),
  index("ix_media_generation_output_selections_run_id").on(table.generationRunId),
  index("ix_media_generation_output_selections_output_id").on(table.generationOutputId),
  index("ix_media_generation_output_selections_selection_hash").on(table.selectionHash),
  index("ix_media_generation_output_selections_owner_user_id").on(table.ownerUserId),
  index("ix_media_generation_output_selections_project_id").on(table.projectId),
  index("ix_media_generation_output_selections_created_at").on(table.createdAt),
  index("ix_media_generation_output_selections_owner_project").on(table.ownerUserId, table.projectId),
]);

export type MediaGenerationWorkspace = typeof mediaGenerationWorkspaces.$inferSelect;
export type NewMediaGenerationWorkspace = typeof mediaGenerationWorkspaces.$inferInsert;
export type MediaCreativeRecipe = typeof mediaCreativeRecipes.$inferSelect;
export type NewMediaCreativeRecipe = typeof mediaCreativeRecipes.$inferInsert;
export type MediaCreativeRecipeRevision = typeof mediaCreativeRecipeRevisions.$inferSelect;
export type NewMediaCreativeRecipeRevision = typeof mediaCreativeRecipeRevisions.$inferInsert;
export type MediaGenerationPlan = typeof mediaGenerationPlans.$inferSelect;
export type NewMediaGenerationPlan = typeof mediaGenerationPlans.$inferInsert;
export type MediaGenerationRunIntent = typeof mediaGenerationRunIntents.$inferSelect;
export type NewMediaGenerationRunIntent = typeof mediaGenerationRunIntents.$inferInsert;
export type MediaGenerationRun = typeof mediaGenerationRuns.$inferSelect;
export type NewMediaGenerationRun = typeof mediaGenerationRuns.$inferInsert;
export type MediaGenerationRunObservation = typeof mediaGenerationRunObservations.$inferSelect;
export type NewMediaGenerationRunObservation = typeof mediaGenerationRunObservations.$inferInsert;
export type MediaGenerationOutput = typeof mediaGenerationOutputs.$inferSelect;
export type NewMediaGenerationOutput = typeof mediaGenerationOutputs.$inferInsert;
export type MediaGenerationOutputSelection = typeof mediaGenerationOutputSelections.$inferSelect;
export type NewMediaGenerationOutputSelection = typeof mediaGenerationOutputSelections.$inferInsert;
export type MediaResearchRoutine = typeof mediaResearchRoutines.$inferSelect;
export type NewMediaResearchRoutine = typeof mediaResearchRoutines.$inferInsert;
export type MediaResearchRoutineRevision =
  typeof mediaResearchRoutineRevisions.$inferSelect;
export type NewMediaResearchRoutineRevision =
  typeof mediaResearchRoutineRevisions.$inferInsert;
export type MediaResearchRun = typeof mediaResearchRuns.$inferSelect;
export type NewMediaResearchRun = typeof mediaResearchRuns.$inferInsert;
export type MediaResearchCandidate = typeof mediaResearchCandidates.$inferSelect;
export type NewMediaResearchCandidate = typeof mediaResearchCandidates.$inferInsert;
export type MediaResearchCandidateDecision =
  typeof mediaResearchCandidateDecisions.$inferSelect;
export type NewMediaResearchCandidateDecision =
  typeof mediaResearchCandidateDecisions.$inferInsert;
export type MediaResearchFinding = typeof mediaResearchFindings.$inferSelect;
export type NewMediaResearchFinding = typeof mediaResearchFindings.$inferInsert;
export type MediaResearchFindingEvidence =
  typeof mediaResearchFindingEvidence.$inferSelect;
export type NewMediaResearchFindingEvidence =
  typeof mediaResearchFindingEvidence.$inferInsert;
export type MediaEditorialProgram = typeof mediaEditorialPrograms.$inferSelect;
export type NewMediaEditorialProgram = typeof mediaEditorialPrograms.$inferInsert;
export type MediaEditorialProgramRevision =
  typeof mediaEditorialProgramRevisions.$inferSelect;
export type NewMediaEditorialProgramRevision =
  typeof mediaEditorialProgramRevisions.$inferInsert;
export type MediaContentItem = typeof mediaContentItems.$inferSelect;
export type NewMediaContentItem = typeof mediaContentItems.$inferInsert;
export type MediaContentItemFinding =
  typeof mediaContentItemFindings.$inferSelect;
export type NewMediaContentItemFinding =
  typeof mediaContentItemFindings.$inferInsert;

export type ExternalConnection = typeof externalConnections.$inferSelect;
export type NewExternalConnection = typeof externalConnections.$inferInsert;
export type ArtifactVersion = typeof artifactVersions.$inferSelect;
export type NewArtifactVersion = typeof artifactVersions.$inferInsert;
export type EngagementOpportunity = typeof opportunities.$inferSelect;
export type NewEngagementOpportunity = typeof opportunities.$inferInsert;
export type OpportunityEvaluation = typeof opportunityEvaluations.$inferSelect;
export type NewOpportunityEvaluation = typeof opportunityEvaluations.$inferInsert;
export type ApplicationDraft = typeof applicationDrafts.$inferSelect;
export type NewApplicationDraft = typeof applicationDrafts.$inferInsert;
export type ExternalAction = typeof externalActions.$inferSelect;
export type NewExternalAction = typeof externalActions.$inferInsert;
export type ExternalActionApproval = typeof externalActionApprovals.$inferSelect;
export type NewExternalActionApproval = typeof externalActionApprovals.$inferInsert;
export type ExternalActionAttempt = typeof externalActionAttempts.$inferSelect;
export type NewExternalActionAttempt = typeof externalActionAttempts.$inferInsert;
export type ExternalActionReceipt = typeof externalActionReceipts.$inferSelect;
export type NewExternalActionReceipt = typeof externalActionReceipts.$inferInsert;
export type OperationEvent = typeof operationEvents.$inferSelect;
export type NewOperationEvent = typeof operationEvents.$inferInsert;
