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
 * (recurrence_rules, comments, tags, scenarios, trpg) are added in later
 * milestones.
 */

import {
  sqliteTable,
  text,
  integer,
  real,
  index,
  primaryKey,
} from 'drizzle-orm/sqlite-core';

// ---------- users ----------
export const users = sqliteTable('users', {
  id: text('id').primaryKey(),
  username: text('username').notNull(),
  displayName: text('display_name'),
  avatarUrl: text('avatar_url'),
  role: text('role'),
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
    storageQuotaMb: integer('storage_quota_mb'),
    storageUsedMb: real('storage_used_mb'),
    projectMetadata: text('project_metadata', { mode: 'json' }),
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
    sessionMetadata: text('session_metadata', { mode: 'json' }),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byProject: index('idx_cs_project_id').on(t.projectId),
    byUpdatedAt: index('idx_cs_updated_at').on(t.updatedAt),
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


// ---------- scenarios ----------
export const scenarios = sqliteTable(
  'scenarios',
  {
    id: text('id').primaryKey(),
    title: text('title').notNull(),
    scenarioKind: text('scenario_kind'),
    ruleset: text('ruleset'),
    description: text('description'),
    genre: text('genre'),
    perspective: text('perspective'),
    setting: text('setting'),
    openingText: text('opening_text'),
    gmInstructions: text('gm_instructions'),
    tags: text('tags', { mode: 'json' }),
    coverImagePath: text('cover_image_path'),
    isPublished: integer('is_published', { mode: 'boolean' }),
    createdBy: text('created_by'),
    voiceTone: text('voice_tone'),
    voiceTenseRules: text('voice_tense_rules'),
    voiceVocabularyRegister: text('voice_vocabulary_register'),
    voiceBannedExpressions: text('voice_banned_expressions', { mode: 'json' }),
    voiceExamplePassages: text('voice_example_passages'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byUpdatedAt: index('idx_scenarios_updated_at').on(t.updatedAt),
    byGenre: index('idx_scenarios_genre').on(t.genre),
  }),
);

export const scenarioCharacters = sqliteTable(
  'scenario_characters',
  {
    id: text('id').primaryKey(),
    scenarioId: text('scenario_id').notNull(),
    characterId: text('character_id'),
    role: text('role'),
    name: text('name').notNull(),
    description: text('description'),
    personalityOverride: text('personality_override'),
    appearanceTagsOverride: text('appearance_tags_override'),
    sortOrder: integer('sort_order'),
    backstory: text('backstory'),
    psychology: text('psychology'),
    speechPatterns: text('speech_patterns'),
    relationships: text('relationships', { mode: 'json' }),
    characterArc: text('character_arc'),
    importance: integer('importance'),
    exampleDialogues: text('example_dialogues'),
    trpgRuleset: text('trpg_ruleset'),
    trpgPcState: text('trpg_pc_state', { mode: 'json' }),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byScenario: index('idx_scenario_characters_scenario_id').on(t.scenarioId),
  }),
);

export const scenarioScenes = sqliteTable(
  'scenario_scenes',
  {
    id: text('id').primaryKey(),
    scenarioId: text('scenario_id').notNull(),
    episodeId: text('episode_id'),
    title: text('title').notNull(),
    description: text('description'),
    sceneType: text('scene_type'),
    gmInstructions: text('gm_instructions'),
    imagePrompt: text('image_prompt'),
    transitions: text('transitions', { mode: 'json' }),
    sortOrder: integer('sort_order'),
    content: text('content'),
    contentVersions: text('content_versions', { mode: 'json' }),
    wordCount: integer('word_count'),
    status: text('status'),
    stateSnapshot: text('state_snapshot', { mode: 'json' }),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byScenario: index('idx_scenario_scenes_scenario_id').on(t.scenarioId),
    byEpisode: index('idx_scenario_scenes_episode_id').on(t.episodeId),
  }),
);

export const scenarioEpisodes = sqliteTable(
  'scenario_episodes',
  {
    id: text('id').primaryKey(),
    scenarioId: text('scenario_id').notNull(),
    title: text('title').notNull(),
    synopsisSentence: text('synopsis_sentence'),
    synopsisParagraph: text('synopsis_paragraph'),
    synopsisFull: text('synopsis_full'),
    beatSheet: text('beat_sheet', { mode: 'json' }),
    status: text('status'),
    sortOrder: integer('sort_order'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    deletedAt: text('deleted_at'),
  },
  (t) => ({
    byScenario: index('idx_scenario_episodes_scenario_id').on(t.scenarioId),
    byUpdatedAt: index('idx_scenario_episodes_updated_at').on(t.updatedAt),
  }),
);

// ---------- Docs（アウトライン型ナレッジ） ----------
// 詳細設計書 2.1。workspace 単一前提のため workspace 列は保持のみ。
export const knowledgeNodes = sqliteTable(
  'knowledge_nodes',
  {
    id: text('id').primaryKey(),
    workspaceId: text('workspace_id'),
    parentId: text('parent_id'),
    rootPageId: text('root_page_id'),
    projectId: text('project_id'),
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
    action: text('action').notNull(), // create | update | delete
    entityId: text('entity_id').notNull(),
    payload: text('payload').notNull(), // JSON string
    baseUpdatedAt: text('base_updated_at'),
    basePayload: text('base_payload', { mode: 'json' }),
    conflictPayload: text('conflict_payload', { mode: 'json' }),
    retryCount: integer('retry_count').notNull().default(0),
    lastError: text('last_error'),
  },
  (t) => ({
    byCreatedAt: index('idx_outbox_created_at').on(t.createdAt),
    byEntity: index('idx_outbox_entity').on(t.tableName, t.entityId),
  }),
);

// ---------- sync_state ----------
export const syncState = sqliteTable('sync_state', {
  tableName: text('table_name').primaryKey(),
  lastPulledAt: text('last_pulled_at'),
  lastPushedAt: text('last_pushed_at'),
  cursor: text('cursor'),
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
export type Scenario = typeof scenarios.$inferSelect;
export type ScenarioCharacter = typeof scenarioCharacters.$inferSelect;
export type ScenarioScene = typeof scenarioScenes.$inferSelect;
export type ScenarioEpisode = typeof scenarioEpisodes.$inferSelect;
export type OutboxOp = typeof outbox.$inferSelect;
export type SyncState = typeof syncState.$inferSelect;
export type KnowledgeNode = typeof knowledgeNodes.$inferSelect;
export type KnowledgeSupertag = typeof knowledgeSupertags.$inferSelect;
export type KnowledgeNodeSupertag = typeof knowledgeNodeSupertags.$inferSelect;
export type KnowledgeField = typeof knowledgeFields.$inferSelect;
export type KnowledgeSupertagField = typeof knowledgeSupertagFields.$inferSelect;
export type KnowledgeFieldValue = typeof knowledgeFieldValues.$inferSelect;
export type KnowledgeNodePlacement = typeof knowledgeNodePlacements.$inferSelect;
export type KnowledgeEdge = typeof knowledgeEdges.$inferSelect;
