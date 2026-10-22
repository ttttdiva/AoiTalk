/**
 * Drizzle-ORM schema for the mobile local SQLite cache.
 *
 * Strategy: server models (SQLAlchemy) are mapped 1:1, DateTime columns are
 * stored as ISO8601 TEXT, UUIDs as TEXT. Every syncable entity carries
 * `updated_at` and `deleted_at` (tombstone). The `outbox` / `sync_state`
 * tables drive the Sync Engine (M2).
 *
 * Scope at M1: users / projects / spaces / tasks / task_occurrences /
 * time_entries / conversation_sessions / conversation_messages /
 * project record tables + outbox + sync_state. Richer features
 * (recurrence_rules, comments, tags) are added in later milestones. Story/
 * TRPG are handled by the Novel Graph Studio product; mobile only links to
 * them via the browser Gateway and does not cache them locally.
 */

import {
  sqliteTable,
  text,
  integer,
  real,
  index,
  uniqueIndex,
  primaryKey,
} from 'drizzle-orm/sqlite-core';

// ---------- users ----------
export const users = sqliteTable('users', {
  id: text('id').primaryKey(),
  username: text('username').notNull(),
  displayName: text('display_name'),
  avatarUrl: text('avatar_url'),
  role: text('role').notNull().default('user'),
  updatedAt: text('updated_at'),
});

// ---------- spaces ----------
export const spaces = sqliteTable('spaces', {
  id: text('id').primaryKey(),
  name: text('name').notNull(),
  slug: text('slug'),
  description: text('description'),
  color: text('color'),
  ownerId: text('owner_id'),
  sortOrder: integer('sort_order'),
  createdAt: text('created_at'),
  updatedAt: text('updated_at'),
  deletedAt: text('deleted_at'),
});

// ---------- projects ----------
export const projects = sqliteTable(
  'projects',
  {
    id: text('id').primaryKey(),
    name: text('name').notNull(),
    slug: text('slug'),
    description: text('description'),
    ownerId: text('owner_id'),
    spaceId: text('space_id'),
    isCompleted: integer('is_completed', { mode: 'boolean' })
      .notNull()
      .default(false),
    storageQuotaMb: integer('storage_quota_mb'),
    storageUsedMb: real('storage_used_mb'),
    projectMetadata: text('project_metadata', { mode: 'json' }),
    isParticipating: integer('is_participating', { mode: 'boolean' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byUpdatedAt: index('idx_projects_updated_at').on(t.updatedAt),
  }),
);

// ---------- tasks ----------
export const tasks = sqliteTable(
  'tasks',
  {
    id: text('id').primaryKey(),
    projectId: text('project_id').notNull(),
    title: text('title').notNull(),
    description: text('description'),
    status: text('status').notNull().default('todo'),
    priority: text('priority'),
    startAt: text('start_at'),
    endAt: text('end_at'),
    allDay: integer('all_day', { mode: 'boolean' }),
    reminderOffsets: text('reminder_offsets', { mode: 'json' }),
    notificationsEnabled: integer('notifications_enabled', { mode: 'boolean' })
      .notNull()
      .default(true),
    autoCloseOnDue: integer('auto_close_on_due', { mode: 'boolean' })
      .notNull()
      .default(false),
    source: text('source'),
    createdBy: text('created_by'),
    completedAt: text('completed_at'),
    archivedAt: text('archived_at'),
    estimatedHours: real('estimated_hours'),
    parentTaskId: text('parent_task_id'),
    taskMetadata: text('task_metadata', { mode: 'json' }),
    sortOrder: real('sort_order'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byProject: index('idx_tasks_project_id').on(t.projectId),
    byStatus: index('idx_tasks_status').on(t.status),
    byStartAt: index('idx_tasks_start_at').on(t.startAt),
    byUpdatedAt: index('idx_tasks_updated_at').on(t.updatedAt),
  }),
);

// ---------- task_occurrences ----------
export const taskOccurrences = sqliteTable(
  'task_occurrences',
  {
    id: text('id').primaryKey(),
    taskId: text('task_id').notNull(),
    startAt: text('start_at').notNull(),
    endAt: text('end_at'),
    // Canonical RRULE start.  Nullable for legacy materialized rows and
    // servers that predate recurrence schedule segments.
    originalStartAt: text('original_start_at'),
    status: text('status').notNull().default('todo'),
    allDay: integer('all_day', { mode: 'boolean' }),
    reminderOffsets: text('reminder_offsets', { mode: 'json' }),
    sourceKind: text('source_kind'),
    isGenerated: integer('is_generated', { mode: 'boolean' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byTask: index('idx_occ_task_id').on(t.taskId),
    byStartAt: index('idx_occ_start_at').on(t.startAt),
    byUpdatedAt: index('idx_occ_updated_at').on(t.updatedAt),
  }),
);

// ---------- time_entries ----------
export const timeEntries = sqliteTable(
  'time_entries',
  {
    id: text('id').primaryKey(),
    taskId: text('task_id'),
    occurrenceId: text('occurrence_id'),
    userId: text('user_id'),
    startedAt: text('started_at').notNull(),
    endedAt: text('ended_at'),
    source: text('source'),
    note: text('note'),
    entryMetadata: text('entry_metadata', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byTask: index('idx_te_task_id').on(t.taskId),
    byUser: index('idx_te_user_id').on(t.userId),
    byStartedAt: index('idx_te_started_at').on(t.startedAt),
    byUpdatedAt: index('idx_te_updated_at').on(t.updatedAt),
  }),
);

// ---------- conversation_sessions ----------
export const conversationSessions = sqliteTable(
  'conversation_sessions',
  {
    id: text('id').primaryKey(),
    userId: text('user_id'),
    characterName: text('character_name'),
    projectId: text('project_id'),
    title: text('title'),
    isGroupChat: integer('is_group_chat', { mode: 'boolean' }),
    /** Agent主体のbinding。character_nameとは別の責務。 */
    agentId: text('agent_id'),
    isPinned: integer('is_pinned', { mode: 'boolean' }).notNull().default(false),
    pinnedAt: text('pinned_at'),
    pinUpdatedAt: text('pin_updated_at'),
    sessionMetadata: text('session_metadata', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byProject: index('idx_cs_project_id').on(t.projectId),
    byUpdatedAt: index('idx_cs_updated_at').on(t.updatedAt),
    byPinnedActivity: index('idx_cs_pinned_activity').on(
      t.isPinned, t.updatedAt,
    ),
  }),
);

// ---------- Conversation Agent catalog (auth/server scoped read cache) ----------
//
// This table is intentionally outside general sync.  The server filters the
// catalog and all Agent-linked projections by ACL; a UUID written through the
// generic sync path could bypass that validator.  Server fingerprint keeps a
// cached catalog from one configured AoiTalk server away from another.
export const conversationAgents = sqliteTable(
  'conversation_agents',
  {
    authScope: text('auth_scope').notNull(),
    serverFingerprint: text('server_fingerprint').notNull(),
    /** ACL query scope. Never fall back between session/project scopes. */
    scopeKey: text('scope_key').notNull().default('global'),
    sessionId: text('session_id'),
    projectId: text('project_id'),
    id: text('id').notNull(),
    displayName: text('display_name').notNull(),
    state: text('state'),
    latestRevisionId: text('latest_revision_id'),
    characterId: text('character_id'),
    characterName: text('character_name'),
    utilization: text('utilization', { mode: 'json' }),
    updatedAt: text('updated_at'),
    cachedAt: text('cached_at').notNull(),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.serverFingerprint, t.scopeKey, t.id] }),
    byScope: index('idx_conversation_agents_scope').on(
      t.authScope,
      t.serverFingerprint,
      t.scopeKey,
    ),
    byUpdatedAt: index('idx_conversation_agents_updated_at').on(t.updatedAt),
  }),
);

// ---------- conversation_messages ----------
export const conversationMessages = sqliteTable(
  'conversation_messages',
  {
    id: text('id').primaryKey(),
    sessionId: text('session_id').notNull(),
    role: text('role').notNull(),
    content: text('content').notNull(),
    messageMetadata: text('message_metadata', { mode: 'json' }),
    tokenCount: integer('token_count'),
    parentMessageId: text('parent_message_id'),
    branchIndex: integer('branch_index'),
    isActiveBranch: integer('is_active_branch', { mode: 'boolean' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    bySession: index('idx_cm_session_id').on(t.sessionId),
    byUpdatedAt: index('idx_cm_updated_at').on(t.updatedAt),
  }),
);

// ---------- project records ----------
export const recordTables = sqliteTable(
  'record_tables',
  {
    id: text('id').primaryKey(),
    projectId: text('project_id').notNull(),
    name: text('name').notNull(),
    description: text('description'),
    icon: text('icon'),
    sortOrder: real('sort_order'),
    schemaVersion: integer('schema_version'),
    memoryPolicy: text('memory_policy'),
    defaultSensitivity: text('default_sensitivity'),
    tableMetadata: text('table_metadata', { mode: 'json' }),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byProject: index('idx_record_tables_project_id').on(t.projectId),
    byUpdatedAt: index('idx_record_tables_updated_at').on(t.updatedAt),
  }),
);

export const recordFields = sqliteTable(
  'record_fields',
  {
    id: text('id').primaryKey(),
    tableId: text('table_id').notNull(),
    key: text('field_key').notNull(),
    label: text('label').notNull(),
    fieldType: text('field_type').notNull(),
    options: text('options', { mode: 'json' }),
    required: integer('required', { mode: 'boolean' }),
    uniqueValue: integer('unique_value', { mode: 'boolean' }),
    sortOrder: real('sort_order'),
    isTitle: integer('is_title', { mode: 'boolean' }),
    isDue: integer('is_due', { mode: 'boolean' }),
    sensitivity: text('sensitivity'),
    fieldMetadata: text('field_metadata', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byTable: index('idx_record_fields_table_id').on(t.tableId),
    byUpdatedAt: index('idx_record_fields_updated_at').on(t.updatedAt),
  }),
);

export const recordRows = sqliteTable(
  'record_rows',
  {
    id: text('id').primaryKey(),
    tableId: text('table_id').notNull(),
    projectId: text('project_id').notNull(),
    createdBy: text('created_by'),
    values: text('values', { mode: 'json' }),
    title: text('title'),
    status: text('status'),
    dueAt: text('due_at'),
    searchText: text('search_text'),
    sensitivity: text('sensitivity'),
    rowMetadata: text('row_metadata', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byProject: index('idx_record_rows_project_id').on(t.projectId),
    byTable: index('idx_record_rows_table_id').on(t.tableId),
    byUpdatedAt: index('idx_record_rows_updated_at').on(t.updatedAt),
  }),
);


// ---------- Apps / Chat context (WS4, auth-scoped read cache) ----------
//
// Apps are fetched directly from their REST endpoints rather than being
// general Sync Engine tables. They therefore live in an isolated cache
// namespace and never create outbox rows. Every table below carries the
// token-derived auth scope in its key so an account switch cannot expose the
// previous account's App, file, job, or conversation context.
export const apps = sqliteTable(
  'apps',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    ownerUserId: text('owner_user_id'),
    originProjectId: text('origin_project_id'),
    name: text('name').notNull(),
    slug: text('slug').notNull(),
    description: text('description'),
    visibility: text('visibility').notNull().default('private'),
    defaultTargetKey: text('default_target_key'),
    readmeNodeId: text('readme_node_id'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    archivedAt: text('archived_at'),
    cachedAt: text('cached_at'),
    permission: text('permission'),
    relatedProjectIds: text('related_project_ids', { mode: 'json' }),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byUpdatedAt: index('idx_apps_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
    bySlug: index('idx_apps_scope_slug').on(t.authScope, t.slug),
  }),
);

export const appTargets = sqliteTable(
  'app_targets',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    appId: text('app_id').notNull(),
    targetKey: text('target_key').notNull(),
    displayName: text('display_name').notNull(),
    surface: text('surface').notNull(),
    runtime: text('runtime').notNull(),
    executionHost: text('execution_host').notNull(),
    entrypoint: text('entrypoint').notNull(),
    manifestSnapshot: text('manifest_snapshot', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byApp: index('idx_app_targets_scope_app').on(t.authScope, t.appId),
    byTargetKey: index('idx_app_targets_scope_app_key').on(
      t.authScope,
      t.appId,
      t.targetKey,
    ),
    byUpdatedAt: index('idx_app_targets_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
  }),
);

export const appContextCache = sqliteTable(
  'app_context_cache',
  {
    authScope: text('auth_scope').notNull(),
    appId: text('app_id').notNull(),
    // Never use NULL for this key. `__global__` denotes an App context that
    // is not attached to a Project; otherwise the value is the Project UUID.
    projectKey: text('project_key').notNull().default('__global__'),
    // Nullable only for the App-wide (__global__) entry; projectKey remains
    // the non-null canonical cache key used by all lookups.
    projectId: text('project_id'),
    targetKey: text('target_key'),
    payloadJson: text('payload_json', { mode: 'json' }),
    etag: text('etag'),
    serverUpdatedAt: text('server_updated_at'),
    cachedAt: text('cached_at'),
    expiresAt: text('expires_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.appId, t.projectKey] }),
    byApp: index('idx_app_context_cache_scope_app').on(
      t.authScope,
      t.appId,
      t.projectKey,
    ),
    byCachedAt: index('idx_app_context_cache_scope_cached').on(
      t.authScope,
      t.cachedAt,
    ),
  }),
);

export const projectApps = sqliteTable(
  'project_apps',
  {
    authScope: text('auth_scope').notNull(),
    projectId: text('project_id').notNull(),
    appId: text('app_id').notNull(),
    bindingMode: text('binding_mode').notNull().default('development'),
    installedReleaseId: text('installed_release_id'),
    enabled: integer('enabled', { mode: 'boolean' }).notNull().default(true),
    pinned: integer('pinned', { mode: 'boolean' }).notNull().default(false),
    displayAlias: text('display_alias'),
    configJson: text('config_json', { mode: 'json' }),
    capabilityGrantsJson: text('capability_grants_json', { mode: 'json' }),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.projectId, t.appId] }),
    byProject: index('idx_project_apps_scope_project').on(
      t.authScope,
      t.projectId,
    ),
    byApp: index('idx_project_apps_scope_app').on(t.authScope, t.appId),
    byUpdatedAt: index('idx_project_apps_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
  }),
);

export const taskAppLinks = sqliteTable(
  'task_app_links',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    taskId: text('task_id').notNull(),
    appId: text('app_id').notNull(),
    targetId: text('target_id'),
    relationType: text('relation_type').notNull().default('related'),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byTask: index('idx_task_app_links_scope_task').on(
      t.authScope,
      t.taskId,
    ),
    byApp: index('idx_task_app_links_scope_app').on(
      t.authScope,
      t.appId,
    ),
    byRelation: index('idx_task_app_links_scope_relation').on(
      t.authScope,
      t.relationType,
    ),
  }),
);

export const appReleases = sqliteTable(
  'app_releases',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    appId: text('app_id').notNull(),
    version: text('version').notNull(),
    gitRevision: text('git_revision').notNull(),
    manifestHash: text('manifest_hash').notNull(),
    readmeHash: text('readme_hash').notNull(),
    changelog: text('changelog'),
    status: text('status').notNull().default('published'),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byApp: index('idx_app_releases_scope_app').on(t.authScope, t.appId),
    byAppStatus: index('idx_app_releases_scope_app_status').on(
      t.authScope,
      t.appId,
      t.status,
      t.createdAt,
    ),
  }),
);

export const appJobs = sqliteTable(
  'app_jobs',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    appId: text('app_id').notNull(),
    targetId: text('target_id'),
    projectId: text('project_id'),
    releaseId: text('release_id'),
    agentRunId: text('agent_run_id'),
    jobType: text('job_type').notNull(),
    status: text('status').notNull().default('queued'),
    inputJson: text('input_json', { mode: 'json' }),
    resultJson: text('result_json', { mode: 'json' }),
    logPath: text('log_path'),
    exitCode: integer('exit_code'),
    startedBy: text('started_by'),
    startedAt: text('started_at'),
    endedAt: text('ended_at'),
    cachedAt: text('cached_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byAppStatus: index('idx_app_jobs_scope_app_status').on(
      t.authScope,
      t.appId,
      t.status,
      t.startedAt,
    ),
    byProject: index('idx_app_jobs_scope_project').on(
      t.authScope,
      t.projectId,
    ),
    byUpdatedAt: index('idx_app_jobs_scope_cached').on(
      t.authScope,
      t.cachedAt,
    ),
  }),
);

export const appFileIndex = sqliteTable(
  'app_file_index',
  {
    authScope: text('auth_scope').notNull(),
    appId: text('app_id').notNull(),
    projectKey: text('project_key').notNull().default('__global__'),
    path: text('path').notNull(),
    filename: text('filename'),
    name: text('name'),
    isDirectory: integer('is_directory', { mode: 'boolean' }),
    sizeBytes: integer('size_bytes'),
    sha256: text('sha256'),
    contentType: text('content_type'),
    extension: text('extension'),
    modifiedAt: text('modified_at'),
    metadataJson: text('metadata_json', { mode: 'json' }),
    cachedAt: text('cached_at'),
  },
  (t) => ({
    pk: primaryKey({
      columns: [t.authScope, t.appId, t.projectKey, t.path],
    }),
    byApp: index('idx_app_file_index_scope_app').on(
      t.authScope,
      t.appId,
      t.projectKey,
    ),
    byCachedAt: index('idx_app_file_index_scope_cached').on(
      t.authScope,
      t.cachedAt,
    ),
  }),
);

export const appFileContentCache = sqliteTable(
  'app_file_content_cache',
  {
    authScope: text('auth_scope').notNull(),
    appId: text('app_id').notNull(),
    projectKey: text('project_key').notNull().default('__global__'),
    path: text('path').notNull(),
    content: text('content'),
    sha256: text('sha256'),
    etag: text('etag'),
    cachedAt: text('cached_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({
      columns: [t.authScope, t.appId, t.projectKey, t.path],
    }),
    byApp: index('idx_app_file_content_scope_app').on(
      t.authScope,
      t.appId,
      t.projectKey,
    ),
    byCachedAt: index('idx_app_file_content_scope_cached').on(
      t.authScope,
      t.cachedAt,
    ),
  }),
);

export const conversationContextSnapshots = sqliteTable(
  'conversation_context_snapshots',
  {
    authScope: text('auth_scope').notNull(),
    sessionId: text('session_id').notNull(),
    projectId: text('project_id'),
    appId: text('app_id'),
    appTargetId: text('app_target_id'),
    status: text('status').notNull().default('unavailable'),
    payloadJson: text('payload_json', { mode: 'json' }),
    messageId: text('message_id'),
    snapshotVersion: integer('snapshot_version'),
    cachedAt: text('cached_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.sessionId] }),
    byApp: index('idx_conversation_context_scope_app').on(
      t.authScope,
      t.appId,
    ),
    byProject: index('idx_conversation_context_scope_project').on(
      t.authScope,
      t.projectId,
    ),
    byCachedAt: index('idx_conversation_context_scope_cached').on(
      t.authScope,
      t.cachedAt,
    ),
  }),
);

// ---------- Docs（アウトライン型ナレッジ） ----------
// Docs scope metadata is kept with each row so shared/project nodes remain
// attributable and local edits can enforce read-only ACLs after a pull.
export const knowledgeNodes = sqliteTable(
  'knowledge_nodes',
  {
    id: text('id').primaryKey(),
    workspaceId: text('workspace_id'),
    parentId: text('parent_id'),
    rootPageId: text('root_page_id'),
    projectId: text('project_id'),
    source: text('source'),
    access: text('access'),
    readOnly: integer('read_only', { mode: 'boolean' }),
    systemKey: text('system_key'),
    title: text('title').notNull(),
    aliases: text('aliases', { mode: 'json' }),
    description: text('description'),
    bodyJson: text('body_json', { mode: 'json' }),
    bodyText: text('body_text'),
    nodeType: text('node_type').notNull().default('node'),
    displayProps: text('display_props', { mode: 'json' }),
    queryJson: text('query_json', { mode: 'json' }),
    viewJson: text('view_json', { mode: 'json' }),
    dayDate: text('day_date'),
    sortOrder: real('sort_order'),
    createdBy: text('created_by'),
    updatedBy: text('updated_by'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    serverUpdatedAt: text('server_updated_at'),
    dirty: integer('dirty', { mode: 'boolean' }).notNull().default(false),
    conflictPayload: text('conflict_payload', { mode: 'json' }),
    archivedAt: text('archived_at'),
  },
  (t) => ({
    byParent: index('idx_knodes_parent').on(t.parentId, t.sortOrder),
    byRoot: index('idx_knodes_root').on(t.rootPageId),
    byUpdatedAt: index('idx_knodes_updated_at').on(t.updatedAt),
    byDay: index('idx_knodes_day').on(t.dayDate),
  }),
);

export const knowledgeSupertags = sqliteTable('knowledge_supertags', {
  id: text('id').primaryKey(),
  workspaceId: text('workspace_id'),
  parentSupertagId: text('parent_supertag_id'),
  systemKey: text('system_key'),
  name: text('name').notNull(),
  baseType: text('base_type'),
  description: text('description'),
  icon: text('icon'),
  color: text('color'),
  templateJson: text('template_json', { mode: 'json' }),
  pinnedFieldIds: text('pinned_field_ids', { mode: 'json' }),
  configJson: text('config_json', { mode: 'json' }),
  titleTemplate: text('title_template'),
  aiInstructions: text('ai_instructions'),
  createdAt: text('created_at'),
  updatedAt: text('updated_at'),
  serverUpdatedAt: text('server_updated_at'),
  dirty: integer('dirty', { mode: 'boolean' }).notNull().default(false),
  conflictPayload: text('conflict_payload', { mode: 'json' }),
});

export const knowledgeNodeSupertags = sqliteTable(
  'knowledge_node_supertags',
  {
    nodeId: text('node_id').notNull(),
    supertagId: text('supertag_id').notNull(),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    serverUpdatedAt: text('server_updated_at'),
    dirty: integer('dirty', { mode: 'boolean' }).notNull().default(false),
    conflictPayload: text('conflict_payload', { mode: 'json' }),
    createdBy: text('created_by'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.nodeId, t.supertagId] }),
    byNode: index('idx_knst_node').on(t.nodeId),
  }),
);

export const knowledgeFields = sqliteTable(
  'knowledge_fields',
  {
    id: text('id').primaryKey(),
    workspaceId: text('workspace_id'),
    supertagId: text('supertag_id'),
    systemKey: text('system_key'),
    name: text('name').notNull(),
    fieldType: text('field_type').notNull().default('text'),
    required: integer('required', { mode: 'boolean' }),
    optionsJson: text('options_json', { mode: 'json' }),
    defaultValueJson: text('default_value_json', { mode: 'json' }),
    sortOrder: real('sort_order'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({ bySupertag: index('idx_kfields_supertag').on(t.supertagId) }),
);

export const knowledgeSupertagFields = sqliteTable(
  'knowledge_supertag_fields',
  {
    supertagId: text('supertag_id').notNull(),
    fieldId: text('field_id').notNull(),
    sortOrder: real('sort_order'),
    required: integer('required', { mode: 'boolean' }),
    showInTemplate: integer('show_in_template', { mode: 'boolean' }),
    optional: integer('optional', { mode: 'boolean' }),
    createdAt: text('created_at'),
  },
  (t) => ({ pk: primaryKey({ columns: [t.supertagId, t.fieldId] }) }),
);

export const knowledgeFieldValues = sqliteTable(
  'knowledge_field_values',
  {
    nodeId: text('node_id').notNull(),
    fieldId: text('field_id').notNull(),
    valueJson: text('value_json', { mode: 'json' }),
    valueText: text('value_text'),
    valueNumber: real('value_number'),
    valueDatetime: text('value_datetime'),
    targetNodeId: text('target_node_id'),
    updatedAt: text('updated_at'),
    serverUpdatedAt: text('server_updated_at'),
    dirty: integer('dirty', { mode: 'boolean' }).notNull().default(false),
    conflictPayload: text('conflict_payload', { mode: 'json' }),
    updatedBy: text('updated_by'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.nodeId, t.fieldId] }),
    byNode: index('idx_kfv_node').on(t.nodeId),
  }),
);

export const knowledgeNodePlacements = sqliteTable(
  'knowledge_node_placements',
  {
    id: text('id').primaryKey(),
    nodeId: text('node_id').notNull(),
    parentNodeId: text('parent_node_id').notNull(),
    sortOrder: real('sort_order'),
    collapsed: integer('collapsed', { mode: 'boolean' }),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
  },
  (t) => ({ byParent: index('idx_knp_parent').on(t.parentNodeId) }),
);

export const knowledgeEdges = sqliteTable(
  'knowledge_edges',
  {
    id: text('id').primaryKey(),
    sourceNodeId: text('source_node_id').notNull(),
    targetNodeId: text('target_node_id').notNull(),
    relationType: text('relation_type'),
    confidence: real('confidence'),
    createdBy: text('created_by'),
    createdAt: text('created_at'),
  },
  (t) => ({
    bySource: index('idx_kedge_source').on(t.sourceNodeId),
    byTarget: index('idx_kedge_target').on(t.targetNodeId),
  }),
);

// ---------- outbox (sync queue) ----------
export const outbox = sqliteTable(
  'outbox',
  {
    opId: text('op_id').primaryKey(),
    createdAt: integer('created_at').notNull(), // unix ms
    tableName: text('table_name').notNull(),
    action: text('action').notNull(), // create | update | delete | reorder
    entityId: text('entity_id').notNull(),
    payload: text('payload').notNull(), // JSON string
    // The account that created the mutation. NULL is retained for legacy rows
    // whose identity cannot be recovered safely during migration.
    authScope: text('auth_scope'),
    docsScopeKey: text('docs_scope_key'),
    blockedReason: text('blocked_reason'),
    baseUpdatedAt: text('base_updated_at'),
    basePayload: text('base_payload', { mode: 'json' }),
    conflictPayload: text('conflict_payload', { mode: 'json' }),
    retryCount: integer('retry_count').notNull().default(0),
    lastError: text('last_error'),
  },
  (t) => ({
    byCreatedAt: index('idx_outbox_created_at').on(t.createdAt),
    byEntity: index('idx_outbox_entity').on(t.tableName, t.entityId),
    byAuthScope: index('idx_outbox_auth_scope').on(t.authScope),
    byDocsScope: index('idx_outbox_docs_scope').on(t.authScope, t.docsScopeKey),
    // Promotion keyset scans include legacy NULL auth scopes and must seek
    // directly by op_id instead of sorting the whole scope on every page.
    byDocsCursor: index('idx_outbox_docs_cursor').on(t.docsScopeKey, t.opId, t.authScope),
    byDocsEntity: index('idx_outbox_docs_entity').on(
      t.authScope, t.docsScopeKey, t.tableName, t.entityId,
    ),
  }),
);

// ---------- outbox resolution journal ----------
//
// This is an app-private, durable journal for the explicit resolution flow.
// It is deliberately independent from `outbox`: adopting the last queued
// operation must not remove the resolution history.  JSON columns are kept as
// TEXT so the journal can preserve the exact snapshots captured by the
// foreground resolver while the public repository only projects safe views.
export const outboxResolutionRecords = sqliteTable(
  'outbox_resolution_records',
  {
    id: text('id').primaryKey(),
    revision: integer('revision').notNull().default(1),
    authScope: text('auth_scope').notNull(),
    serverFingerprint: text('server_fingerprint').notNull(),
    credentialIdentityRevision: text('credential_identity_revision').notNull(),
    kind: text('kind').notNull(),
    tableName: text('table_name').notNull(),
    entityId: text('entity_id').notNull(),
    opId: text('op_id').notNull(),
    createdAt: integer('created_at').notNull(),
    state: text('state').notNull(),
    operationOrigin: text('operation_origin').notNull().default('unrecorded'),
    targetNamespaceJson: text('target_namespace_json'),
    operationSnapshotJson: text('operation_snapshot_json').notNull(),
    localSnapshotJson: text('local_snapshot_json').notNull(),
    relatedSnapshotJson: text('related_snapshot_json').notNull().default('{}'),
    membershipSnapshotJson: text('membership_snapshot_json').notNull().default('[]'),
    cacheSnapshotJson: text('cache_snapshot_json').notNull().default('{}'),
    guardSnapshotJson: text('guard_snapshot_json').notNull().default('{}'),
    serverSnapshotJson: text('server_snapshot_json'),
    viewJson: text('view_json').notNull(),
    draftJson: text('draft_json'),
    exportSnapshotJson: text('export_snapshot_json'),
    updatedAt: integer('updated_at').notNull(),
  },
  (t) => ({
    byAuthScope: index('idx_outbox_resolution_auth_scope').on(
      t.authScope,
      t.createdAt,
    ),
    byOperation: index('idx_outbox_resolution_operation').on(
      t.authScope,
      t.opId,
      t.entityId,
    ),
    byState: index('idx_outbox_resolution_state').on(
      t.authScope,
      t.state,
      t.updatedAt,
    ),
  }),
);

// ---------- sync_state ----------
export const syncState = sqliteTable('sync_state', {
  tableName: text('table_name').primaryKey(),
  lastPulledAt: text('last_pulled_at'),
  lastPushedAt: text('last_pushed_at'),
  cursor: text('cursor'),
});

// ---------- Docs staged sync runs ----------
// A run is scoped by account + Docs workspace.  Pages are downloaded into
// docs_sync_staging and promoted only after the server snapshot/ACL revision
// and every table cursor have been validated.  Keeping this state separate
// from sync_state makes an interrupted pull resumable without exposing a
// partial snapshot through the live Docs tables.
export const docsSyncRuns = sqliteTable(
  'docs_sync_runs',
  {
    runId: text('run_id').primaryKey(),
    authScope: text('auth_scope').notNull(),
    /** Stable composite identity: auth + library + optional project. */
    scopeKey: text('scope_key').notNull().default('personal'),
    scopeId: text('scope_id'),
    projectId: text('project_id'),
    snapshotToken: text('snapshot_token'),
    scopeRevision: text('scope_revision'),
    scopeDigest: text('scope_digest'),
    serverTime: text('server_time'),
    cursorJson: text('cursor_json', { mode: 'json' }).notNull().default({}),
    pendingJson: text('pending_json', { mode: 'json' }).notNull().default([]),
    digestJson: text('digest_json', { mode: 'json' }).notNull().default({}),
    authoritativeJson: text('authoritative_json', { mode: 'json' })
      .notNull()
      .default({}),
    scopesJson: text('scopes_json', { mode: 'json' }),
    force: integer('force', { mode: 'boolean' }).notNull().default(false),
    state: text('state').notNull().default('downloading'),
    createdAt: text('created_at').notNull(),
    updatedAt: text('updated_at').notNull(),
  },
  (t) => ({
    byAuthScope: index('idx_docs_sync_runs_auth_scope').on(
      t.authScope,
      t.scopeKey,
      t.scopeId,
      t.projectId,
      t.state,
    ),
    byUpdatedAt: index('idx_docs_sync_runs_updated_at').on(t.updatedAt),
  }),
);

export const docsSyncStaging = sqliteTable(
  'docs_sync_staging',
  {
    runId: text('run_id').notNull(),
    authScope: text('auth_scope').notNull(),
    scopeKey: text('scope_key').notNull().default('personal'),
    scopeId: text('scope_id'),
    projectId: text('project_id'),
    tableName: text('table_name').notNull(),
    entityKey: text('entity_key').notNull(),
    payloadJson: text('payload_json', { mode: 'json' }),
    isTombstone: integer('is_tombstone', { mode: 'boolean' })
      .notNull()
      .default(false),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.runId, t.tableName, t.entityKey] }),
    byRun: index('idx_docs_sync_staging_run').on(t.runId),
    byAuthScope: index('idx_docs_sync_staging_auth_scope').on(
      t.authScope,
      t.scopeKey,
      t.scopeId,
      t.projectId,
    ),
  }),
);

// A UUID may legitimately be visible through multiple libraries/projects.
// Keep scope membership separate from the shared live row so promotion of one
// scope cannot delete or hide the same entity from another scope.
export const docsScopeMembership = sqliteTable(
  'docs_scope_membership',
  {
    authScope: text('auth_scope').notNull(),
    scopeKey: text('scope_key').notNull(),
    scopeId: text('scope_id').notNull(),
    projectId: text('project_id'),
    tableName: text('table_name').notNull(),
    entityKey: text('entity_key').notNull(),
    state: text('state').notNull().default('active'),
    access: text('access'),
    readOnly: integer('read_only', { mode: 'boolean' }),
    updatedAt: text('updated_at').notNull(),
  },
  (t) => ({
    pk: primaryKey({
      columns: [t.authScope, t.scopeKey, t.tableName, t.entityKey],
    }),
    byEntity: index('idx_docs_scope_membership_entity').on(
      t.authScope,
      t.tableName,
      t.entityKey,
      t.state,
    ),
    byScope: index('idx_docs_scope_membership_scope').on(
      t.authScope,
      t.scopeKey,
      t.state,
    ),
  }),
);

// ---------- app_migrations ----------
// 同期cache消去では削除しない、端末DBの一度きりdata migration marker。
export const appMigrations = sqliteTable('app_migrations', {
  id: text('id').primaryKey(),
  appliedAt: text('applied_at'),
});

// ---------- filer_dir_cache（サーバーファイラー一覧のオフライン永続キャッシュ） ----------
// cache_key は filesLocationKey と同一形式。source==='server' のみ保存する。
export const filerDirCache = sqliteTable(
  'filer_dir_cache',
  {
    cacheKey: text('cache_key').primaryKey(),
    source: text('source'),
    scope: text('scope'),
    authScope: text('auth_scope'),
    projectId: text('project_id'),
    path: text('path'),
    currentPath: text('current_path'),
    parentPath: text('parent_path'),
    canGoUp: integer('can_go_up'),
    isAdminMode: integer('is_admin_mode'),
    itemsJson: text('items_json'),
    cachedAt: text('cached_at'),
  },
  (t) => ({
    byAuthScope: index('idx_filer_dir_cache_auth_scope').on(t.authScope),
    byCachedAt: index('idx_filer_dir_cache_cached_at').on(t.cachedAt),
  }),
);

// ---------- task_detail_cache（タスク詳細スナップショットのオフライン補完キャッシュ） ----------
// cache_key は `task:{taskId}` / `attachments:{taskId}`。SQLite 本体行に無い
// 集約フィールド（comments / subtasks / assignees / 添付一覧）を最終同期時点で保持し、
// オフライン・取得失敗時の補完に使う。上限500行、超過時 cached_at 昇順で削除する。
export const taskDetailCache = sqliteTable(
  'task_detail_cache',
  {
    cacheKey: text('cache_key').primaryKey(),
    payloadJson: text('payload_json'),
    cachedAt: text('cached_at'),
  },
  (t) => ({
    byCachedAt: index('idx_task_detail_cache_cached_at').on(t.cachedAt),
  }),
);

// ---------- pending_clip_ingests（durable ClipIngest operation journal） ----------
// Legacy rows keep operation_key=NULL forever until the user explicitly
// claims them. New rows use this table as a durable operation journal rather
// than a best-effort resend queue.
export const pendingClipIngests = sqliteTable(
  'pending_clip_ingests',
  {
    id: text('id').primaryKey(),
    source: text('source').notNull(),
    operationKey: text('operation_key'),
    requestJson: text('request_json'),
    contextJson: text('context_json'),
    delivery: text('delivery'), // remote | local
    status: text('status').notNull().default('queued'),
    authScope: text('auth_scope'),
    // Stable configured API identity. NULL is preserved for legacy rows and
    // means recovery-only/server-unknown; never infer it during migration.
    serverFingerprint: text('server_fingerprint'),
    remoteJobId: text('remote_job_id'),
    ackJson: text('ack_json'),
    resultJson: text('result_json'),
    errorJson: text('error_json'),
    retryCount: integer('retry_count').notNull().default(0),
    lastError: text('last_error'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    terminalAt: text('terminal_at'),
  },
  (t) => ({
    byStatus: index('idx_pending_clip_ingests_status').on(t.status),
    byAuthScope: index('idx_pending_clip_ingests_auth_scope').on(t.authScope),
    byCreatedAt: index('idx_pending_clip_ingests_created_at').on(t.createdAt),
    byScopeStatus: index('idx_pending_clip_ingests_scope_status').on(
      t.authScope,
      t.status,
    ),
    byScopeServerStatus: index(
      'idx_pending_clip_ingests_scope_server_status',
    ).on(t.authScope, t.serverFingerprint, t.status),
    byOperationKey: uniqueIndex(
      'idx_pending_clip_ingests_operation_key',
    ).on(t.operationKey),
  }),
);

// ---------- clip_ingest_target_cache（取り込み先設定のオフラインキャッシュ） ----------
// サーバー `GET /api/users/me/settings` の clip_ingest.targets をオンライン時に
// 取得して保持し、サーバー未到達時のモバイルLLM取り込みで参照する。
// cacheKey は認証スコープそのもの（別ユーザーのキャッシュを読まないため）。
export const clipIngestTargetCache = sqliteTable('clip_ingest_target_cache', {
  cacheKey: text('cache_key').primaryKey(),
  targetsJson: text('targets_json'),
  cachedAt: text('cached_at'),
});

// Re-export for convenience
export type User = typeof users.$inferSelect;
export type Project = typeof projects.$inferSelect;
export type Space = typeof spaces.$inferSelect;
export type Task = typeof tasks.$inferSelect;
export type TaskOccurrence = typeof taskOccurrences.$inferSelect;
export type TimeEntry = typeof timeEntries.$inferSelect;
export type ConversationSession = typeof conversationSessions.$inferSelect;
export type ConversationMessage = typeof conversationMessages.$inferSelect;
export type RecordTable = typeof recordTables.$inferSelect;
export type RecordField = typeof recordFields.$inferSelect;
export type RecordRow = typeof recordRows.$inferSelect;
export type App = typeof apps.$inferSelect;
export type AppTarget = typeof appTargets.$inferSelect;
export type AppContextCache = typeof appContextCache.$inferSelect;
export type ProjectApp = typeof projectApps.$inferSelect;
export type TaskAppLink = typeof taskAppLinks.$inferSelect;
export type AppRelease = typeof appReleases.$inferSelect;
export type AppJob = typeof appJobs.$inferSelect;
export type AppFileIndex = typeof appFileIndex.$inferSelect;
export type AppFileContentCache = typeof appFileContentCache.$inferSelect;
export type ConversationContextSnapshot =
  typeof conversationContextSnapshots.$inferSelect;
export type OutboxOp = typeof outbox.$inferSelect;
export type OutboxResolutionRecord = typeof outboxResolutionRecords.$inferSelect;
export type SyncState = typeof syncState.$inferSelect;
export type DocsSyncRun = typeof docsSyncRuns.$inferSelect;
export type DocsSyncStaging = typeof docsSyncStaging.$inferSelect;
export type DocsScopeMembership = typeof docsScopeMembership.$inferSelect;
export type AppMigration = typeof appMigrations.$inferSelect;
export type KnowledgeNode = typeof knowledgeNodes.$inferSelect;
export type KnowledgeSupertag = typeof knowledgeSupertags.$inferSelect;
export type KnowledgeNodeSupertag = typeof knowledgeNodeSupertags.$inferSelect;
export type KnowledgeField = typeof knowledgeFields.$inferSelect;
export type KnowledgeSupertagField = typeof knowledgeSupertagFields.$inferSelect;
export type KnowledgeFieldValue = typeof knowledgeFieldValues.$inferSelect;
export type KnowledgeNodePlacement = typeof knowledgeNodePlacements.$inferSelect;
export type KnowledgeEdge = typeof knowledgeEdges.$inferSelect;
export type FilerDirCache = typeof filerDirCache.$inferSelect;
export type TaskDetailCache = typeof taskDetailCache.$inferSelect;
export type PendingClipIngest = typeof pendingClipIngests.$inferSelect;
export type ClipIngestTargetCache = typeof clipIngestTargetCache.$inferSelect;
