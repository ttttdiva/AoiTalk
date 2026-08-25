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

// ---------- Story Studio (canonical, auth-scoped cache) ----------
//
// Story is deliberately kept separate from the legacy `scenarios` projection.
// Every table carries the token-derived auth scope in its primary key so a
// token/account switch can never make a cached work, episode, or draft visible
// to another account.  Story mutations are sent directly to /api/story; the
// local tables are an offline read cache, except for storyLocalDrafts which is
// durable local-first manuscript state.
export const storyWorks = sqliteTable(
  'story_works',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    userId: text('user_id'),
    title: text('title').notNull(),
    synopsis: text('synopsis'),
    plot: text('plot'),
    styleGuide: text('style_guide'),
    kind: text('kind').notNull().default('novel'),
    status: text('status').notNull().default('planning'),
    targetEpisodeChars: integer('target_episode_chars'),
    plannedEpisodeCount: integer('planned_episode_count'),
    startEpisodeId: text('start_episode_id'),
    uiState: text('ui_state', { mode: 'json' }),
    modelOverride: text('model_override', { mode: 'json' }),
    imageSettings: text('image_settings', { mode: 'json' }),
    resolvedModel: text('resolved_model'),
    modelLayer: text('model_layer'),
    episodeCount: integer('episode_count'),
    charCount: integer('char_count'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    archivedAt: text('archived_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byUpdatedAt: index('idx_story_works_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
    byKind: index('idx_story_works_scope_kind').on(t.authScope, t.kind),
  }),
);

export const storyEpisodes = sqliteTable(
  'story_episodes',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    workId: text('work_id').notNull(),
    title: text('title').notNull(),
    plot: text('plot'),
    summary: text('summary'),
    summaryLocked: integer('summary_locked', { mode: 'boolean' }),
    premiseNote: text('premise_note'),
    status: text('status').notNull().default('unwritten'),
    targetChars: integer('target_chars'),
    charCount: integer('char_count'),
    body: text('body'),
    bodyEtag: text('body_etag'),
    mapX: real('map_x'),
    mapY: real('map_y'),
    sortHint: real('sort_hint'),
    currentRevNo: integer('current_rev_no'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    archivedAt: text('archived_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byWork: index('idx_story_episodes_scope_work').on(t.authScope, t.workId),
    byWorkSort: index('idx_story_episodes_scope_work_sort').on(
      t.authScope,
      t.workId,
      t.sortHint,
    ),
  }),
);

export const storyLinks = sqliteTable(
  'story_links',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    workId: text('work_id').notNull(),
    fromEpisodeId: text('from_episode_id').notNull(),
    toEpisodeId: text('to_episode_id').notNull(),
    choiceLabel: text('choice_label'),
    position: real('position'),
    isPrimary: integer('is_primary', { mode: 'boolean' }),
    createdAt: text('created_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byWork: index('idx_story_links_scope_work').on(t.authScope, t.workId),
    byFrom: index('idx_story_links_scope_from').on(
      t.authScope,
      t.fromEpisodeId,
    ),
  }),
);

export const storyCharacters = sqliteTable(
  'story_characters',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    userId: text('user_id'),
    name: text('name').notNull(),
    aliases: text('aliases', { mode: 'json' }),
    summary: text('summary'),
    description: text('description'),
    notes: text('notes'),
    aiMode: text('ai_mode').notNull().default('keyword'),
    keywords: text('keywords', { mode: 'json' }),
    imagePath: text('image_path'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    archivedAt: text('archived_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byName: index('idx_story_characters_scope_name').on(t.authScope, t.name),
  }),
);

export const storyWorkCharacters = sqliteTable(
  'story_work_characters',
  {
    authScope: text('auth_scope').notNull(),
    workId: text('work_id').notNull(),
    characterId: text('character_id').notNull(),
    roleNote: text('role_note'),
    position: real('position'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.workId, t.characterId] }),
    byWork: index('idx_story_work_characters_scope_work').on(
      t.authScope,
      t.workId,
    ),
  }),
);

export const storyRulebooks = sqliteTable(
  'story_rulebooks',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    userId: text('user_id'),
    name: text('name').notNull(),
    content: text('content'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
    archivedAt: text('archived_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byName: index('idx_story_rulebooks_scope_name').on(t.authScope, t.name),
  }),
);

export const storyWorkRulebooks = sqliteTable(
  'story_work_rulebooks',
  {
    authScope: text('auth_scope').notNull(),
    workId: text('work_id').notNull(),
    rulebookId: text('rulebook_id').notNull(),
    enabled: integer('enabled', { mode: 'boolean' }),
    position: real('position'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.workId, t.rulebookId] }),
    byWork: index('idx_story_work_rulebooks_scope_work').on(
      t.authScope,
      t.workId,
    ),
  }),
);

export const storyNotes = sqliteTable(
  'story_notes',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    workId: text('work_id').notNull(),
    title: text('title').notNull(),
    content: text('content'),
    aiMode: text('ai_mode').notNull().default('keyword'),
    keywords: text('keywords', { mode: 'json' }),
    position: real('position'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byWork: index('idx_story_notes_scope_work').on(t.authScope, t.workId),
  }),
);

export const storyEpisodeRevisions = sqliteTable(
  'story_episode_revisions',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    episodeId: text('episode_id').notNull(),
    revNo: integer('rev_no').notNull(),
    title: text('title'),
    plot: text('plot'),
    body: text('body'),
    message: text('message'),
    origin: text('origin').notNull(),
    bodySha256: text('body_sha256').notNull(),
    charCount: integer('char_count'),
    createdBy: text('created_by').notNull(),
    createdAt: text('created_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byEpisodeRev: index('idx_story_revisions_scope_episode_rev').on(
      t.authScope,
      t.episodeId,
      t.revNo,
    ),
  }),
);

export const storyGenerationJobs = sqliteTable(
  'story_generation_jobs',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    workId: text('work_id').notNull(),
    kind: text('kind').notNull(),
    payload: text('payload', { mode: 'json' }),
    status: text('status').notNull().default('queued'),
    progress: text('progress', { mode: 'json' }),
    result: text('result', { mode: 'json' }),
    error: text('error'),
    createdAt: text('created_at'),
    startedAt: text('started_at'),
    finishedAt: text('finished_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byWorkStatus: index('idx_story_jobs_scope_work_status').on(
      t.authScope,
      t.workId,
      t.status,
    ),
  }),
);

export const storyWritingSessions = sqliteTable(
  'story_writing_sessions',
  {
    authScope: text('auth_scope').notNull(),
    id: text('id').notNull(),
    workId: text('work_id').notNull(),
    episodeId: text('episode_id'),
    conversationSessionId: text('conversation_session_id'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.id] }),
    byWork: index('idx_story_writing_sessions_scope_work').on(
      t.authScope,
      t.workId,
    ),
    byConversation: index('idx_story_writing_sessions_scope_conversation').on(
      t.authScope,
      t.conversationSessionId,
    ),
  }),
);

/** Durable manuscript draft; unlike Story cache rows this is local-only. */
export const storyLocalDrafts = sqliteTable(
  'story_local_drafts',
  {
    authScope: text('auth_scope').notNull(),
    episodeId: text('episode_id').notNull(),
    body: text('body').notNull(),
    expectedEtag: text('expected_etag'),
    serverSnapshot: text('server_snapshot', { mode: 'json' }),
    conflictStatus: text('conflict_status').notNull().default('draft'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.episodeId] }),
    byUpdatedAt: index('idx_story_local_drafts_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
  }),
);

/**
 * Recovery copy of the pre-Story AsyncStorage writing-session draft.
 *
 * This is separate from manuscript body drafts because the legacy record may
 * have no target episode and can contain arbitrary unknown fields.  The raw
 * JSON is retained so a future migration/UI can recover data that the
 * canonical Story model does not currently understand.
 */
export const storyLegacyWritingDrafts = sqliteTable(
  'story_legacy_writing_drafts',
  {
    authScope: text('auth_scope').notNull(),
    legacyKey: text('legacy_key').notNull(),
    workId: text('work_id').notNull(),
    targetEpisodeId: text('target_episode_id'),
    targetSceneId: text('target_scene_id'),
    prompt: text('prompt'),
    rawPayload: text('raw_payload', { mode: 'json' }).notNull(),
    status: text('status').notNull().default('recovered'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.authScope, t.legacyKey] }),
    byWork: index('idx_story_legacy_writing_drafts_scope_work').on(
      t.authScope,
      t.workId,
    ),
    byUpdatedAt: index('idx_story_legacy_writing_drafts_scope_updated').on(
      t.authScope,
      t.updatedAt,
    ),
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

// ---------- pending_clip_ingests（サーバー未到達時のクリップ取り込み保留キュー） ----------
// AoiTalk サーバーへ到達できず、モバイルLLMでもローカル完結できなかった入力を保持する。
// 同期時に `POST /api/docs/ingest` へ再送し、成功したら行を削除する。
// 恒久エラー（4xx）は status='failed' として残し、ユーザーに見える形で保持する。
// authScope は enqueue 時点の認証スコープ（`auth:<user_id>` / 'anonymous'）で、
// 別ユーザーの保留を再送しないための絞り込みに使う。
export const pendingClipIngests = sqliteTable(
  'pending_clip_ingests',
  {
    id: text('id').primaryKey(),
    source: text('source').notNull(),
    status: text('status').notNull().default('queued'), // queued | failed
    authScope: text('auth_scope'),
    retryCount: integer('retry_count').notNull().default(0),
    lastError: text('last_error'),
    createdAt: text('created_at'),
    updatedAt: text('updated_at'),
  },
  (t) => ({
    byStatus: index('idx_pending_clip_ingests_status').on(t.status),
    byAuthScope: index('idx_pending_clip_ingests_auth_scope').on(t.authScope),
    byCreatedAt: index('idx_pending_clip_ingests_created_at').on(t.createdAt),
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
export type StoryWork = typeof storyWorks.$inferSelect;
export type StoryEpisode = typeof storyEpisodes.$inferSelect;
export type StoryLink = typeof storyLinks.$inferSelect;
export type StoryCharacter = typeof storyCharacters.$inferSelect;
export type StoryWorkCharacter = typeof storyWorkCharacters.$inferSelect;
export type StoryRulebook = typeof storyRulebooks.$inferSelect;
export type StoryWorkRulebook = typeof storyWorkRulebooks.$inferSelect;
export type StoryNote = typeof storyNotes.$inferSelect;
export type StoryEpisodeRevision = typeof storyEpisodeRevisions.$inferSelect;
export type StoryGenerationJob = typeof storyGenerationJobs.$inferSelect;
export type StoryWritingSession = typeof storyWritingSessions.$inferSelect;
export type StoryLocalDraft = typeof storyLocalDrafts.$inferSelect;
export type StoryLegacyWritingDraft = typeof storyLegacyWritingDrafts.$inferSelect;
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
