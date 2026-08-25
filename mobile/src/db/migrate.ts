/**
 * One-shot schema bootstrap for the local SQLite cache.
 *
 * At M1 we ship a hand-written CREATE TABLE IF NOT EXISTS bootstrap instead of
 * drizzle-kit generated migrations. This keeps the first install self-sufficient
 * and lets us iterate on the schema without regenerating SQL bundles.
 * From M2 onwards we'll switch to drizzle-kit `generate` + `migrate()` once
 * the schema stabilises.
 */

import { getSqlite } from "./client";

const DDL: string[] = [
  `CREATE TABLE IF NOT EXISTS users (
     id TEXT PRIMARY KEY,
     username TEXT NOT NULL,
     display_name TEXT,
     avatar_url TEXT,
     role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
     updated_at TEXT
   );`,

  `CREATE TABLE IF NOT EXISTS spaces (
     id TEXT PRIMARY KEY,
     name TEXT NOT NULL,
     slug TEXT,
     description TEXT,
     color TEXT,
     owner_id TEXT,
     sort_order INTEGER,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,

  `CREATE TABLE IF NOT EXISTS projects (
     id TEXT PRIMARY KEY,
     name TEXT NOT NULL,
     slug TEXT,
     description TEXT,
     owner_id TEXT,
     space_id TEXT,
     is_completed INTEGER NOT NULL DEFAULT 0,
     storage_quota_mb INTEGER,
     storage_used_mb REAL,
     project_metadata TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at);`,

  `CREATE TABLE IF NOT EXISTS tasks (
     id TEXT PRIMARY KEY,
     project_id TEXT NOT NULL,
     title TEXT NOT NULL,
     description TEXT,
     status TEXT NOT NULL DEFAULT 'todo',
     priority TEXT,
     start_at TEXT,
     end_at TEXT,
     all_day INTEGER,
     reminder_offsets TEXT,
     notifications_enabled INTEGER NOT NULL DEFAULT 1,
     auto_close_on_due INTEGER NOT NULL DEFAULT 0,
     source TEXT,
     created_by TEXT,
     completed_at TEXT,
     archived_at TEXT,
     estimated_hours REAL,
     parent_task_id TEXT,
     task_metadata TEXT,
     sort_order REAL,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_start_at ON tasks(start_at);`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);`,

  `CREATE TABLE IF NOT EXISTS task_occurrences (
     id TEXT PRIMARY KEY,
     task_id TEXT NOT NULL,
     start_at TEXT NOT NULL,
     end_at TEXT,
     status TEXT NOT NULL DEFAULT 'todo',
     all_day INTEGER,
     reminder_offsets TEXT,
     source_kind TEXT,
     is_generated INTEGER,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_occ_task_id ON task_occurrences(task_id);`,
  `CREATE INDEX IF NOT EXISTS idx_occ_start_at ON task_occurrences(start_at);`,
  `CREATE INDEX IF NOT EXISTS idx_occ_updated_at ON task_occurrences(updated_at);`,

  `CREATE TABLE IF NOT EXISTS time_entries (
     id TEXT PRIMARY KEY,
     task_id TEXT,
     occurrence_id TEXT,
     user_id TEXT,
     started_at TEXT NOT NULL,
     ended_at TEXT,
     source TEXT,
     note TEXT,
     entry_metadata TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_te_task_id ON time_entries(task_id);`,
  `CREATE INDEX IF NOT EXISTS idx_te_user_id ON time_entries(user_id);`,
  `CREATE INDEX IF NOT EXISTS idx_te_started_at ON time_entries(started_at);`,
  `CREATE INDEX IF NOT EXISTS idx_te_updated_at ON time_entries(updated_at);`,

  `CREATE TABLE IF NOT EXISTS conversation_sessions (
     id TEXT PRIMARY KEY,
     user_id TEXT,
     character_name TEXT,
     project_id TEXT,
     title TEXT,
     is_group_chat INTEGER,
     session_metadata TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_cs_project_id ON conversation_sessions(project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_cs_updated_at ON conversation_sessions(updated_at);`,

  `CREATE TABLE IF NOT EXISTS conversation_messages (
     id TEXT PRIMARY KEY,
     session_id TEXT NOT NULL,
     role TEXT NOT NULL,
     content TEXT NOT NULL,
     message_metadata TEXT,
     token_count INTEGER,
     parent_message_id TEXT,
     branch_index INTEGER,
     is_active_branch INTEGER,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_cm_session_id ON conversation_messages(session_id);`,
  `CREATE INDEX IF NOT EXISTS idx_cm_updated_at ON conversation_messages(updated_at);`,

  `CREATE TABLE IF NOT EXISTS record_tables (
     id TEXT PRIMARY KEY,
     project_id TEXT NOT NULL,
     name TEXT NOT NULL,
     description TEXT,
     icon TEXT,
     sort_order REAL,
     schema_version INTEGER,
     memory_policy TEXT,
     default_sensitivity TEXT,
     table_metadata TEXT,
     created_by TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_record_tables_project_id ON record_tables(project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_record_tables_updated_at ON record_tables(updated_at);`,

  `CREATE TABLE IF NOT EXISTS record_fields (
     id TEXT PRIMARY KEY,
     table_id TEXT NOT NULL,
     field_key TEXT NOT NULL,
     label TEXT NOT NULL,
     field_type TEXT NOT NULL,
     options TEXT,
     required INTEGER,
     unique_value INTEGER,
     sort_order REAL,
     is_title INTEGER,
     is_due INTEGER,
     sensitivity TEXT,
     field_metadata TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_record_fields_table_id ON record_fields(table_id);`,
  `CREATE INDEX IF NOT EXISTS idx_record_fields_updated_at ON record_fields(updated_at);`,

  `CREATE TABLE IF NOT EXISTS record_rows (
     id TEXT PRIMARY KEY,
     table_id TEXT NOT NULL,
     project_id TEXT NOT NULL,
     created_by TEXT,
     "values" TEXT,
     title TEXT,
     status TEXT,
     due_at TEXT,
     search_text TEXT,
     sensitivity TEXT,
     row_metadata TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_record_rows_project_id ON record_rows(project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_record_rows_table_id ON record_rows(table_id);`,
  `CREATE INDEX IF NOT EXISTS idx_record_rows_updated_at ON record_rows(updated_at);`,



  `CREATE TABLE IF NOT EXISTS scenarios (
     id TEXT PRIMARY KEY,
     title TEXT NOT NULL,
     scenario_kind TEXT,
     ruleset TEXT,
     description TEXT,
     genre TEXT,
     perspective TEXT,
     setting TEXT,
     opening_text TEXT,
     gm_instructions TEXT,
     tags TEXT,
     cover_image_path TEXT,
     is_published INTEGER,
     created_by TEXT,
     voice_tone TEXT,
     voice_tense_rules TEXT,
     voice_vocabulary_register TEXT,
     voice_banned_expressions TEXT,
     voice_example_passages TEXT,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_scenarios_updated_at ON scenarios(updated_at);`,
  `CREATE INDEX IF NOT EXISTS idx_scenarios_genre ON scenarios(genre);`,

  `CREATE TABLE IF NOT EXISTS scenario_characters (
     id TEXT PRIMARY KEY,
     scenario_id TEXT NOT NULL,
     character_id TEXT,
     role TEXT,
     name TEXT NOT NULL,
     description TEXT,
     personality_override TEXT,
     appearance_tags_override TEXT,
     sort_order INTEGER,
     backstory TEXT,
     psychology TEXT,
     speech_patterns TEXT,
     relationships TEXT,
     character_arc TEXT,
     importance INTEGER,
     example_dialogues TEXT,
     trpg_ruleset TEXT,
     trpg_pc_state TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_scenario_characters_scenario_id ON scenario_characters(scenario_id);`,

  `CREATE TABLE IF NOT EXISTS scenario_scenes (
     id TEXT PRIMARY KEY,
     scenario_id TEXT NOT NULL,
     episode_id TEXT,
     title TEXT NOT NULL,
     description TEXT,
     scene_type TEXT,
     gm_instructions TEXT,
     image_prompt TEXT,
     transitions TEXT,
     sort_order INTEGER,
     content TEXT,
     content_versions TEXT,
     word_count INTEGER,
     status TEXT,
     state_snapshot TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_scenario_scenes_scenario_id ON scenario_scenes(scenario_id);`,
  `CREATE INDEX IF NOT EXISTS idx_scenario_scenes_episode_id ON scenario_scenes(episode_id);`,

  `CREATE TABLE IF NOT EXISTS scenario_episodes (
     id TEXT PRIMARY KEY,
     scenario_id TEXT NOT NULL,
     title TEXT NOT NULL,
     synopsis_sentence TEXT,
     synopsis_paragraph TEXT,
     synopsis_full TEXT,
     beat_sheet TEXT,
     status TEXT,
     sort_order INTEGER,
     created_at TEXT,
     updated_at TEXT,
     deleted_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_scenario_episodes_scenario_id ON scenario_episodes(scenario_id);`,
  `CREATE INDEX IF NOT EXISTS idx_scenario_episodes_updated_at ON scenario_episodes(updated_at);`,

  // ---------- Story Studio（canonical, auth-scoped cache） ----------
  // Story is additive: legacy scenario tables above remain untouched.  The
  // auth_scope prefix is part of every primary key so upgrading an existing
  // install cannot expose one account's Story cache or drafts to another.
  `CREATE TABLE IF NOT EXISTS story_works (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     user_id TEXT,
     title TEXT NOT NULL,
     synopsis TEXT,
     plot TEXT,
     style_guide TEXT,
     kind TEXT NOT NULL DEFAULT 'novel',
     status TEXT NOT NULL DEFAULT 'planning',
     target_episode_chars INTEGER,
     planned_episode_count INTEGER,
     start_episode_id TEXT,
     ui_state TEXT,
     model_override TEXT,
     image_settings TEXT,
     resolved_model TEXT,
     model_layer TEXT,
     episode_count INTEGER,
     char_count INTEGER,
     created_at TEXT,
     updated_at TEXT,
     archived_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_works_scope_updated
     ON story_works(auth_scope, updated_at);`,
  `CREATE INDEX IF NOT EXISTS idx_story_works_scope_kind
     ON story_works(auth_scope, kind);`,
  `CREATE TABLE IF NOT EXISTS story_episodes (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     work_id TEXT NOT NULL,
     title TEXT NOT NULL,
     plot TEXT,
     summary TEXT,
     summary_locked INTEGER,
     premise_note TEXT,
     status TEXT NOT NULL DEFAULT 'unwritten',
     target_chars INTEGER,
     char_count INTEGER,
     body TEXT,
     body_etag TEXT,
     map_x REAL,
     map_y REAL,
     sort_hint REAL,
     current_rev_no INTEGER,
     created_at TEXT,
     updated_at TEXT,
     archived_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_episodes_scope_work
     ON story_episodes(auth_scope, work_id);`,
  `CREATE INDEX IF NOT EXISTS idx_story_episodes_scope_work_sort
     ON story_episodes(auth_scope, work_id, sort_hint);`,
  `CREATE TABLE IF NOT EXISTS story_links (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     work_id TEXT NOT NULL,
     from_episode_id TEXT NOT NULL,
     to_episode_id TEXT NOT NULL,
     choice_label TEXT,
     position REAL,
     is_primary INTEGER,
     created_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_links_scope_work
     ON story_links(auth_scope, work_id);`,
  `CREATE INDEX IF NOT EXISTS idx_story_links_scope_from
     ON story_links(auth_scope, from_episode_id);`,
  `CREATE TABLE IF NOT EXISTS story_characters (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     user_id TEXT,
     name TEXT NOT NULL,
     aliases TEXT,
     summary TEXT,
     description TEXT,
     notes TEXT,
     ai_mode TEXT NOT NULL DEFAULT 'keyword',
     keywords TEXT,
     image_path TEXT,
     created_at TEXT,
     updated_at TEXT,
     archived_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_characters_scope_name
     ON story_characters(auth_scope, name);`,
  `CREATE TABLE IF NOT EXISTS story_work_characters (
     auth_scope TEXT NOT NULL,
     work_id TEXT NOT NULL,
     character_id TEXT NOT NULL,
     role_note TEXT,
     position REAL,
     PRIMARY KEY (auth_scope, work_id, character_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_work_characters_scope_work
     ON story_work_characters(auth_scope, work_id);`,
  `CREATE TABLE IF NOT EXISTS story_rulebooks (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     user_id TEXT,
     name TEXT NOT NULL,
     content TEXT,
     created_at TEXT,
     updated_at TEXT,
     archived_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_rulebooks_scope_name
     ON story_rulebooks(auth_scope, name);`,
  `CREATE TABLE IF NOT EXISTS story_work_rulebooks (
     auth_scope TEXT NOT NULL,
     work_id TEXT NOT NULL,
     rulebook_id TEXT NOT NULL,
     enabled INTEGER,
     position REAL,
     PRIMARY KEY (auth_scope, work_id, rulebook_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_work_rulebooks_scope_work
     ON story_work_rulebooks(auth_scope, work_id);`,
  `CREATE TABLE IF NOT EXISTS story_notes (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     work_id TEXT NOT NULL,
     title TEXT NOT NULL,
     content TEXT,
     ai_mode TEXT NOT NULL DEFAULT 'keyword',
     keywords TEXT,
     position REAL,
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_notes_scope_work
     ON story_notes(auth_scope, work_id);`,
  `CREATE TABLE IF NOT EXISTS story_episode_revisions (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     episode_id TEXT NOT NULL,
     rev_no INTEGER NOT NULL,
     title TEXT,
     plot TEXT,
     body TEXT,
     message TEXT,
     origin TEXT NOT NULL,
     body_sha256 TEXT NOT NULL,
     char_count INTEGER,
     created_by TEXT NOT NULL,
     created_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_revisions_scope_episode_rev
     ON story_episode_revisions(auth_scope, episode_id, rev_no);`,
  `CREATE TABLE IF NOT EXISTS story_generation_jobs (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     work_id TEXT NOT NULL,
     kind TEXT NOT NULL,
     payload TEXT,
     status TEXT NOT NULL DEFAULT 'queued',
     progress TEXT,
     result TEXT,
     error TEXT,
     created_at TEXT,
     started_at TEXT,
     finished_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_jobs_scope_work_status
     ON story_generation_jobs(auth_scope, work_id, status);`,
  `CREATE TABLE IF NOT EXISTS story_writing_sessions (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     work_id TEXT NOT NULL,
     episode_id TEXT,
     conversation_session_id TEXT,
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_writing_sessions_scope_work
     ON story_writing_sessions(auth_scope, work_id);`,
  `CREATE INDEX IF NOT EXISTS idx_story_writing_sessions_scope_conversation
     ON story_writing_sessions(auth_scope, conversation_session_id);`,
  `CREATE TABLE IF NOT EXISTS story_local_drafts (
     auth_scope TEXT NOT NULL,
     episode_id TEXT NOT NULL,
     body TEXT NOT NULL,
     expected_etag TEXT,
     server_snapshot TEXT,
     conflict_status TEXT NOT NULL DEFAULT 'draft',
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, episode_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_local_drafts_scope_updated
     ON story_local_drafts(auth_scope, updated_at);`,
  `CREATE TABLE IF NOT EXISTS story_legacy_writing_drafts (
     auth_scope TEXT NOT NULL,
     legacy_key TEXT NOT NULL,
     work_id TEXT NOT NULL,
     target_episode_id TEXT,
     target_scene_id TEXT,
     prompt TEXT,
     raw_payload TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'recovered',
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, legacy_key)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_story_legacy_writing_drafts_scope_work
     ON story_legacy_writing_drafts(auth_scope, work_id);`,
  `CREATE INDEX IF NOT EXISTS idx_story_legacy_writing_drafts_scope_updated
     ON story_legacy_writing_drafts(auth_scope, updated_at);`,

  // ---------- Apps / Chat context (WS4, auth-scoped read cache) ----------
  // These tables are deliberately not part of the general Sync Engine and do
  // not participate in the outbox.  The DDL is additive and contains no
  // payload parsing/backfill, so malformed legacy cache data cannot abort an
  // upgrade.  Every table is keyed by auth_scope to isolate account changes.
  `CREATE TABLE IF NOT EXISTS apps (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     owner_user_id TEXT,
     origin_project_id TEXT,
     name TEXT NOT NULL,
     slug TEXT NOT NULL,
     description TEXT,
     visibility TEXT NOT NULL DEFAULT 'private',
     default_target_key TEXT,
     readme_node_id TEXT,
     created_at TEXT,
     updated_at TEXT,
     archived_at TEXT,
     cached_at TEXT,
     permission TEXT,
     related_project_ids TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_apps_scope_updated
     ON apps(auth_scope, updated_at);`,
  `CREATE INDEX IF NOT EXISTS idx_apps_scope_slug
     ON apps(auth_scope, slug);`,
  `CREATE TABLE IF NOT EXISTS app_targets (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     app_id TEXT NOT NULL,
     target_key TEXT NOT NULL,
     display_name TEXT NOT NULL,
     surface TEXT NOT NULL,
     runtime TEXT NOT NULL,
     execution_host TEXT NOT NULL,
     entrypoint TEXT NOT NULL,
     manifest_snapshot TEXT,
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_targets_scope_app
     ON app_targets(auth_scope, app_id);`,
  `CREATE INDEX IF NOT EXISTS idx_app_targets_scope_app_key
     ON app_targets(auth_scope, app_id, target_key);`,
  `CREATE INDEX IF NOT EXISTS idx_app_targets_scope_updated
     ON app_targets(auth_scope, updated_at);`,
  `CREATE TABLE IF NOT EXISTS app_context_cache (
     auth_scope TEXT NOT NULL,
     app_id TEXT NOT NULL,
     -- __global__ means an App-wide context; otherwise this is a Project UUID.
     project_key TEXT NOT NULL DEFAULT '__global__',
     project_id TEXT,
     target_key TEXT,
     payload_json TEXT,
     etag TEXT,
     server_updated_at TEXT,
     cached_at TEXT,
     expires_at TEXT,
     PRIMARY KEY (auth_scope, app_id, project_key)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_context_cache_scope_app
     ON app_context_cache(auth_scope, app_id, project_key);`,
  `CREATE INDEX IF NOT EXISTS idx_app_context_cache_scope_cached
     ON app_context_cache(auth_scope, cached_at);`,
  `CREATE TABLE IF NOT EXISTS project_apps (
     auth_scope TEXT NOT NULL,
     project_id TEXT NOT NULL,
     app_id TEXT NOT NULL,
     binding_mode TEXT NOT NULL DEFAULT 'development',
     installed_release_id TEXT,
     enabled INTEGER NOT NULL DEFAULT 1,
     pinned INTEGER NOT NULL DEFAULT 0,
     display_alias TEXT,
     config_json TEXT,
     capability_grants_json TEXT,
     created_by TEXT,
     created_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, project_id, app_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_project_apps_scope_project
     ON project_apps(auth_scope, project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_project_apps_scope_app
     ON project_apps(auth_scope, app_id);`,
  `CREATE INDEX IF NOT EXISTS idx_project_apps_scope_updated
     ON project_apps(auth_scope, updated_at);`,
  `CREATE TABLE IF NOT EXISTS task_app_links (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     task_id TEXT NOT NULL,
     app_id TEXT NOT NULL,
     target_id TEXT,
     relation_type TEXT NOT NULL DEFAULT 'related',
     created_by TEXT,
     created_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_task_app_links_scope_task
     ON task_app_links(auth_scope, task_id);`,
  `CREATE INDEX IF NOT EXISTS idx_task_app_links_scope_app
     ON task_app_links(auth_scope, app_id);`,
  `CREATE INDEX IF NOT EXISTS idx_task_app_links_scope_relation
     ON task_app_links(auth_scope, relation_type);`,
  `CREATE TABLE IF NOT EXISTS app_releases (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     app_id TEXT NOT NULL,
     version TEXT NOT NULL,
     git_revision TEXT NOT NULL,
     manifest_hash TEXT NOT NULL,
     readme_hash TEXT NOT NULL,
     changelog TEXT,
     status TEXT NOT NULL DEFAULT 'published',
     created_by TEXT,
     created_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_releases_scope_app
     ON app_releases(auth_scope, app_id);`,
  `CREATE INDEX IF NOT EXISTS idx_app_releases_scope_app_status
     ON app_releases(auth_scope, app_id, status, created_at);`,
  `CREATE TABLE IF NOT EXISTS app_jobs (
     auth_scope TEXT NOT NULL,
     id TEXT NOT NULL,
     app_id TEXT NOT NULL,
     target_id TEXT,
     project_id TEXT,
     release_id TEXT,
     agent_run_id TEXT,
     job_type TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'queued',
     input_json TEXT,
     result_json TEXT,
     log_path TEXT,
     exit_code INTEGER,
     started_by TEXT,
     started_at TEXT,
     ended_at TEXT,
     cached_at TEXT,
     PRIMARY KEY (auth_scope, id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_jobs_scope_app_status
     ON app_jobs(auth_scope, app_id, status, started_at);`,
  `CREATE INDEX IF NOT EXISTS idx_app_jobs_scope_project
     ON app_jobs(auth_scope, project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_app_jobs_scope_cached
     ON app_jobs(auth_scope, cached_at);`,
  `CREATE TABLE IF NOT EXISTS app_file_index (
     auth_scope TEXT NOT NULL,
     app_id TEXT NOT NULL,
     project_key TEXT NOT NULL DEFAULT '__global__',
     path TEXT NOT NULL,
     filename TEXT,
     name TEXT,
     is_directory INTEGER,
     size_bytes INTEGER,
     sha256 TEXT,
     content_type TEXT,
     extension TEXT,
     modified_at TEXT,
     metadata_json TEXT,
     cached_at TEXT,
     PRIMARY KEY (auth_scope, app_id, project_key, path)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_file_index_scope_app
     ON app_file_index(auth_scope, app_id, project_key);`,
  `CREATE INDEX IF NOT EXISTS idx_app_file_index_scope_cached
     ON app_file_index(auth_scope, cached_at);`,
  `CREATE TABLE IF NOT EXISTS app_file_content_cache (
     auth_scope TEXT NOT NULL,
     app_id TEXT NOT NULL,
     project_key TEXT NOT NULL DEFAULT '__global__',
     path TEXT NOT NULL,
     content TEXT,
     sha256 TEXT,
     etag TEXT,
     cached_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, app_id, project_key, path)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_app_file_content_scope_app
     ON app_file_content_cache(auth_scope, app_id, project_key);`,
  `CREATE INDEX IF NOT EXISTS idx_app_file_content_scope_cached
     ON app_file_content_cache(auth_scope, cached_at);`,
  `CREATE TABLE IF NOT EXISTS conversation_context_snapshots (
     auth_scope TEXT NOT NULL,
     session_id TEXT NOT NULL,
     project_id TEXT,
     app_id TEXT,
     app_target_id TEXT,
     status TEXT NOT NULL DEFAULT 'unavailable',
     payload_json TEXT,
     message_id TEXT,
     snapshot_version INTEGER,
     cached_at TEXT,
     updated_at TEXT,
     PRIMARY KEY (auth_scope, session_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_conversation_context_scope_app
     ON conversation_context_snapshots(auth_scope, app_id);`,
  `CREATE INDEX IF NOT EXISTS idx_conversation_context_scope_project
     ON conversation_context_snapshots(auth_scope, project_id);`,
  `CREATE INDEX IF NOT EXISTS idx_conversation_context_scope_cached
     ON conversation_context_snapshots(auth_scope, cached_at);`,

  // ---------- Docs（アウトライン型ナレッジ） ----------
  `CREATE TABLE IF NOT EXISTS knowledge_nodes (
     id TEXT PRIMARY KEY,
     workspace_id TEXT,
     parent_id TEXT,
     root_page_id TEXT,
     project_id TEXT,
     source TEXT,
     access TEXT,
     read_only INTEGER,
     system_key TEXT,
     title TEXT NOT NULL,
     aliases TEXT,
     description TEXT,
     body_json TEXT,
     body_text TEXT,
     node_type TEXT NOT NULL DEFAULT 'node',
     display_props TEXT,
     query_json TEXT,
     view_json TEXT,
     day_date TEXT,
     sort_order REAL,
     created_by TEXT,
     updated_by TEXT,
      created_at TEXT,
      updated_at TEXT,
      server_updated_at TEXT,
      dirty INTEGER NOT NULL DEFAULT 0,
      conflict_payload TEXT,
      archived_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_knodes_parent ON knowledge_nodes(parent_id, sort_order);`,
  `CREATE INDEX IF NOT EXISTS idx_knodes_root ON knowledge_nodes(root_page_id);`,
  `CREATE INDEX IF NOT EXISTS idx_knodes_updated_at ON knowledge_nodes(updated_at);`,
  `CREATE INDEX IF NOT EXISTS idx_knodes_day ON knowledge_nodes(day_date);`,

  `CREATE TABLE IF NOT EXISTS knowledge_supertags (
     id TEXT PRIMARY KEY,
     workspace_id TEXT,
     parent_supertag_id TEXT,
     system_key TEXT,
     name TEXT NOT NULL,
     base_type TEXT,
     description TEXT,
     icon TEXT,
     color TEXT,
     template_json TEXT,
     pinned_field_ids TEXT,
     config_json TEXT,
     title_template TEXT,
     ai_instructions TEXT,
      created_at TEXT,
      updated_at TEXT,
      server_updated_at TEXT,
      dirty INTEGER NOT NULL DEFAULT 0,
      conflict_payload TEXT
   );`,

  `CREATE TABLE IF NOT EXISTS knowledge_node_supertags (
      node_id TEXT NOT NULL,
      supertag_id TEXT NOT NULL,
      created_at TEXT,
      updated_at TEXT,
      server_updated_at TEXT,
      dirty INTEGER NOT NULL DEFAULT 0,
      conflict_payload TEXT,
      created_by TEXT,
     PRIMARY KEY (node_id, supertag_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_knst_node ON knowledge_node_supertags(node_id);`,

  `CREATE TABLE IF NOT EXISTS knowledge_fields (
     id TEXT PRIMARY KEY,
     workspace_id TEXT,
     supertag_id TEXT,
     system_key TEXT,
     name TEXT NOT NULL,
     field_type TEXT NOT NULL DEFAULT 'text',
     required INTEGER,
     options_json TEXT,
     default_value_json TEXT,
     sort_order REAL,
     created_at TEXT,
     updated_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_kfields_supertag ON knowledge_fields(supertag_id);`,

  `CREATE TABLE IF NOT EXISTS knowledge_supertag_fields (
     supertag_id TEXT NOT NULL,
     field_id TEXT NOT NULL,
     sort_order REAL,
     required INTEGER,
     show_in_template INTEGER,
     optional INTEGER,
     created_at TEXT,
     PRIMARY KEY (supertag_id, field_id)
   );`,

  `CREATE TABLE IF NOT EXISTS knowledge_field_values (
     node_id TEXT NOT NULL,
     field_id TEXT NOT NULL,
     value_json TEXT,
     value_text TEXT,
     value_number REAL,
     value_datetime TEXT,
      target_node_id TEXT,
      updated_at TEXT,
      server_updated_at TEXT,
      dirty INTEGER NOT NULL DEFAULT 0,
      conflict_payload TEXT,
      updated_by TEXT,
     PRIMARY KEY (node_id, field_id)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_kfv_node ON knowledge_field_values(node_id);`,

  `CREATE TABLE IF NOT EXISTS knowledge_node_placements (
     id TEXT PRIMARY KEY,
     node_id TEXT NOT NULL,
     parent_node_id TEXT NOT NULL,
     sort_order REAL,
     collapsed INTEGER,
     created_by TEXT,
     created_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_knp_parent ON knowledge_node_placements(parent_node_id);`,

  `CREATE TABLE IF NOT EXISTS knowledge_edges (
     id TEXT PRIMARY KEY,
     source_node_id TEXT NOT NULL,
     target_node_id TEXT NOT NULL,
     relation_type TEXT,
     confidence REAL,
     created_by TEXT,
     created_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_kedge_source ON knowledge_edges(source_node_id);`,
  `CREATE INDEX IF NOT EXISTS idx_kedge_target ON knowledge_edges(target_node_id);`,

  `CREATE TABLE IF NOT EXISTS outbox (
     op_id TEXT PRIMARY KEY,
     created_at INTEGER NOT NULL,
     table_name TEXT NOT NULL,
     action TEXT NOT NULL,
     entity_id TEXT NOT NULL,
     payload TEXT NOT NULL,
     auth_scope TEXT,
     docs_scope_key TEXT,
     blocked_reason TEXT,
     base_updated_at TEXT,
     base_payload TEXT,
     conflict_payload TEXT,
     retry_count INTEGER NOT NULL DEFAULT 0,
     last_error TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_outbox_created_at ON outbox(created_at);`,
  `CREATE INDEX IF NOT EXISTS idx_outbox_entity ON outbox(table_name, entity_id);`,

  `CREATE TABLE IF NOT EXISTS sync_state (
     table_name TEXT PRIMARY KEY,
     last_pulled_at TEXT,
     last_pushed_at TEXT,
     cursor TEXT
   );`,
  // Docs pull pages land here before a validated snapshot is promoted to the
  // live knowledge_* tables.  All keys include the auth scope so an account
  // transition can never resume another account's partial download.
  `CREATE TABLE IF NOT EXISTS docs_sync_runs (
     run_id TEXT PRIMARY KEY,
     auth_scope TEXT NOT NULL,
     scope_key TEXT NOT NULL DEFAULT 'personal',
     scope_id TEXT,
     project_id TEXT,
     snapshot_token TEXT,
     scope_revision TEXT,
     scope_digest TEXT,
     server_time TEXT,
     cursor_json TEXT NOT NULL DEFAULT '{}',
     pending_json TEXT NOT NULL DEFAULT '[]',
     digest_json TEXT NOT NULL DEFAULT '{}',
     authoritative_json TEXT NOT NULL DEFAULT '{}',
     scopes_json TEXT,
     force INTEGER NOT NULL DEFAULT 0,
     state TEXT NOT NULL DEFAULT 'downloading',
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
   );`,
  `CREATE INDEX IF NOT EXISTS idx_docs_sync_runs_auth_scope
     ON docs_sync_runs(auth_scope, scope_key, scope_id, project_id, state);`,
  `CREATE INDEX IF NOT EXISTS idx_docs_sync_runs_updated_at
     ON docs_sync_runs(updated_at);`,
  `CREATE TABLE IF NOT EXISTS docs_sync_staging (
     run_id TEXT NOT NULL,
     auth_scope TEXT NOT NULL,
     scope_key TEXT NOT NULL DEFAULT 'personal',
     scope_id TEXT,
     project_id TEXT,
     table_name TEXT NOT NULL,
     entity_key TEXT NOT NULL,
     payload_json TEXT,
     is_tombstone INTEGER NOT NULL DEFAULT 0,
     PRIMARY KEY (run_id, table_name, entity_key)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_docs_sync_staging_run
     ON docs_sync_staging(run_id);`,
  `CREATE INDEX IF NOT EXISTS idx_docs_sync_staging_auth_scope
     ON docs_sync_staging(auth_scope, scope_key, scope_id, project_id);`,
  `CREATE TABLE IF NOT EXISTS docs_scope_membership (
     auth_scope TEXT NOT NULL,
     scope_key TEXT NOT NULL,
     scope_id TEXT NOT NULL,
     project_id TEXT,
     table_name TEXT NOT NULL,
     entity_key TEXT NOT NULL,
     state TEXT NOT NULL DEFAULT 'active',
     access TEXT,
     read_only INTEGER,
     updated_at TEXT NOT NULL,
     PRIMARY KEY (auth_scope, scope_key, table_name, entity_key)
   );`,
  `CREATE INDEX IF NOT EXISTS idx_docs_scope_membership_entity
     ON docs_scope_membership(auth_scope, table_name, entity_key, state);`,
  `CREATE INDEX IF NOT EXISTS idx_docs_scope_membership_scope
     ON docs_scope_membership(auth_scope, scope_key, state);`,
  `CREATE TABLE IF NOT EXISTS app_migrations (
     id TEXT PRIMARY KEY,
     applied_at TEXT
   );`,

  // ---------- filer_dir_cache（サーバーファイラー一覧のオフライン永続キャッシュ） ----------
  `CREATE TABLE IF NOT EXISTS filer_dir_cache (
     cache_key TEXT PRIMARY KEY,
     source TEXT,
     scope TEXT,
     auth_scope TEXT,
     project_id TEXT,
     path TEXT,
     current_path TEXT,
     parent_path TEXT,
     can_go_up INTEGER,
     is_admin_mode INTEGER,
     items_json TEXT,
     cached_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_filer_dir_cache_auth_scope ON filer_dir_cache(auth_scope);`,
  `CREATE INDEX IF NOT EXISTS idx_filer_dir_cache_cached_at ON filer_dir_cache(cached_at);`,

  // ---------- task_detail_cache（タスク詳細スナップショットのオフライン補完キャッシュ） ----------
  `CREATE TABLE IF NOT EXISTS task_detail_cache (
     cache_key TEXT PRIMARY KEY,
     payload_json TEXT,
     cached_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_task_detail_cache_cached_at ON task_detail_cache(cached_at);`,

  // ---------- pending_clip_ingests（サーバー未到達時のクリップ取り込み保留キュー） ----------
  // auth_scope は enqueue 時点の認証スコープ（`auth:<user_id>` / 'anonymous'）。
  // 別ユーザーの保留を再送しないための絞り込みに使う。
  `CREATE TABLE IF NOT EXISTS pending_clip_ingests (
     id TEXT PRIMARY KEY,
     source TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'queued',
     auth_scope TEXT,
     retry_count INTEGER NOT NULL DEFAULT 0,
     last_error TEXT,
     created_at TEXT,
     updated_at TEXT
   );`,
  `CREATE INDEX IF NOT EXISTS idx_pending_clip_ingests_status ON pending_clip_ingests(status);`,
  `CREATE INDEX IF NOT EXISTS idx_pending_clip_ingests_auth_scope ON pending_clip_ingests(auth_scope);`,
  `CREATE INDEX IF NOT EXISTS idx_pending_clip_ingests_created_at ON pending_clip_ingests(created_at);`,

  // ---------- clip_ingest_target_cache（取り込み先設定のオフラインキャッシュ） ----------
  // cache_key は認証スコープそのもの。別ユーザーの取り込み先を読まないため。
  `CREATE TABLE IF NOT EXISTS clip_ingest_target_cache (
     cache_key TEXT PRIMARY KEY,
     targets_json TEXT,
     cached_at TEXT
   );`,
];

let _applied = false;
let _asyncEnsurePromise: Promise<void> | null = null;
let _asyncSchemaReady = false;
const PENDING_CLIP_REQUEUE_V0100_MARKER =
  "migration:pending_clip_ingests_requeue_v0.1.100";
const CLIP_TARGET_FILM_POLICY_V0103_MARKER =
  "migration:clip_ingest_target_cache_film_policy_v0.1.103";
const DOCS_SCOPE_MEMBERSHIP_BACKFILL_MARKER =
  "migration:docs_scope_membership_backfill_v0.1.111";
const DOCS_OUTBOX_SCOPE_BACKFILL_MARKER =
  "migration:docs_outbox_scope_backfill_v0.1.112";
// The maintenance below used to run on every process start.  Keep a durable
// marker so large local caches are not rewritten on every launch.  The marker
// is written in the same transaction as the maintenance, so a failed/partial
// migration is retried safely on the next attempt.
const SCHEMA_MAINTENANCE_V01142_MARKER =
  "migration:schema_maintenance_v0.1.142";

function ensureColumn(
  tableName: string,
  columnName: string,
  ddl: string,
): void {
  const db = getSqlite();
  const columns = db.getAllSync(`PRAGMA table_info(${tableName});`) as Array<{
    name?: string;
  }>;
  const exists = columns.some((column) => column.name === columnName);
  if (!exists) {
    db.execSync(ddl);
  }
}

/**
 * The transaction object supplied by expo-sqlite's
 * `withExclusiveTransactionAsync`.  Keeping this structural avoids relying on
 * expo-sqlite's intentionally unexported Transaction class and, importantly,
 * prevents maintenance helpers from reaching for the global sync connection.
 */
type AsyncSqliteExecutor = {
  execAsync: (source: string) => Promise<void>;
  getFirstAsync: <T>(source: string) => Promise<T | null>;
  getAllAsync: <T>(source: string) => Promise<T[]>;
};

const COLUMN_MIGRATIONS: Array<[string, string, string]> = [
  ["tasks", "notifications_enabled", "ALTER TABLE tasks ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1;"],
  ["tasks", "estimated_hours", "ALTER TABLE tasks ADD COLUMN estimated_hours REAL;"],
  ["tasks", "parent_task_id", "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;"],
  ["tasks", "auto_close_on_due", "ALTER TABLE tasks ADD COLUMN auto_close_on_due INTEGER NOT NULL DEFAULT 0;"],
  ["projects", "is_completed", "ALTER TABLE projects ADD COLUMN is_completed INTEGER NOT NULL DEFAULT 0;"],
  ["knowledge_nodes", "source", "ALTER TABLE knowledge_nodes ADD COLUMN source TEXT;"],
  ["knowledge_nodes", "access", "ALTER TABLE knowledge_nodes ADD COLUMN access TEXT;"],
  ["knowledge_nodes", "read_only", "ALTER TABLE knowledge_nodes ADD COLUMN read_only INTEGER;"],
  ["knowledge_nodes", "server_updated_at", "ALTER TABLE knowledge_nodes ADD COLUMN server_updated_at TEXT;"],
  ["knowledge_nodes", "dirty", "ALTER TABLE knowledge_nodes ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0;"],
  ["knowledge_nodes", "conflict_payload", "ALTER TABLE knowledge_nodes ADD COLUMN conflict_payload TEXT;"],
  ["knowledge_supertags", "server_updated_at", "ALTER TABLE knowledge_supertags ADD COLUMN server_updated_at TEXT;"],
  ["knowledge_supertags", "dirty", "ALTER TABLE knowledge_supertags ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0;"],
  ["knowledge_supertags", "conflict_payload", "ALTER TABLE knowledge_supertags ADD COLUMN conflict_payload TEXT;"],
  ["knowledge_node_supertags", "updated_at", "ALTER TABLE knowledge_node_supertags ADD COLUMN updated_at TEXT;"],
  ["knowledge_node_supertags", "server_updated_at", "ALTER TABLE knowledge_node_supertags ADD COLUMN server_updated_at TEXT;"],
  ["knowledge_node_supertags", "dirty", "ALTER TABLE knowledge_node_supertags ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0;"],
  ["knowledge_node_supertags", "conflict_payload", "ALTER TABLE knowledge_node_supertags ADD COLUMN conflict_payload TEXT;"],
  ["knowledge_field_values", "server_updated_at", "ALTER TABLE knowledge_field_values ADD COLUMN server_updated_at TEXT;"],
  ["knowledge_field_values", "dirty", "ALTER TABLE knowledge_field_values ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0;"],
  ["knowledge_field_values", "conflict_payload", "ALTER TABLE knowledge_field_values ADD COLUMN conflict_payload TEXT;"],
  ["outbox", "base_payload", "ALTER TABLE outbox ADD COLUMN base_payload TEXT;"],
  ["outbox", "conflict_payload", "ALTER TABLE outbox ADD COLUMN conflict_payload TEXT;"],
  ["outbox", "auth_scope", "ALTER TABLE outbox ADD COLUMN auth_scope TEXT;"],
  ["outbox", "docs_scope_key", "ALTER TABLE outbox ADD COLUMN docs_scope_key TEXT;"],
  ["outbox", "blocked_reason", "ALTER TABLE outbox ADD COLUMN blocked_reason TEXT;"],
  ["docs_sync_runs", "scope_key", "ALTER TABLE docs_sync_runs ADD COLUMN scope_key TEXT NOT NULL DEFAULT 'personal';"],
  ["docs_sync_runs", "project_id", "ALTER TABLE docs_sync_runs ADD COLUMN project_id TEXT;"],
  ["docs_sync_runs", "scope_digest", "ALTER TABLE docs_sync_runs ADD COLUMN scope_digest TEXT;"],
  ["docs_sync_runs", "server_time", "ALTER TABLE docs_sync_runs ADD COLUMN server_time TEXT;"],
  ["docs_sync_staging", "scope_key", "ALTER TABLE docs_sync_staging ADD COLUMN scope_key TEXT NOT NULL DEFAULT 'personal';"],
  ["docs_sync_staging", "project_id", "ALTER TABLE docs_sync_staging ADD COLUMN project_id TEXT;"],
];

const INDEX_MIGRATIONS = [
  "CREATE INDEX IF NOT EXISTS idx_outbox_auth_scope ON outbox(auth_scope);",
  "CREATE INDEX IF NOT EXISTS idx_outbox_docs_scope ON outbox(auth_scope, docs_scope_key);",
  "CREATE INDEX IF NOT EXISTS idx_docs_sync_runs_auth_scope ON docs_sync_runs(auth_scope, scope_key, scope_id, project_id, state);",
  "CREATE INDEX IF NOT EXISTS idx_docs_sync_staging_auth_scope ON docs_sync_staging(auth_scope, scope_key, scope_id, project_id);",
];

/**
 * Attach legacy live Docs rows to the authenticated scope projection that was
 * already persisted by a previous sync.  The live tables predate
 * docs_scope_membership and most relation tables do not carry workspace or
 * project columns, so this migration is deliberately conservative: rows are
 * attributed only when an existing `docs_scopes:v2:<user>` snapshot names the
 * workspace, and project-scoped rows are backfilled only for tables that have
 * an unambiguous project_id column (nodes).  The next validated pull fills in
 * the remaining relation memberships atomically.
 */
async function backfillDocsScopeMembershipAsync(tx: AsyncSqliteExecutor): Promise<void> {
  const applied = await tx.getFirstAsync(
    `SELECT id FROM app_migrations WHERE id = '${DOCS_SCOPE_MEMBERSHIP_BACKFILL_MARKER}' LIMIT 1;`,
  );
  if (applied) return;
  const scopeRows = await tx.getAllAsync(
    "SELECT table_name, cursor FROM sync_state WHERE table_name LIKE 'docs_scopes:v2:%';",
  ) as Array<{ table_name?: string; cursor?: string | null }>;
  const sqlLiteral = (value: string): string =>
    `'${value.replace(/'/g, "''")}'`;
  const insertMembership = async (
    authScope: string,
    scopeKey: string,
    workspaceId: string,
    projectId: string | null,
    tableName: string,
    entityExpression: string,
    fromClause: string,
    whereClause: string,
    access: string | null,
    readOnly: boolean | null,
  ): Promise<void> => {
    const accessSql = access == null ? "NULL" : sqlLiteral(access);
    const readOnlySql = readOnly == null ? "NULL" : readOnly ? "1" : "0";
    const projectSql = projectId == null ? "NULL" : sqlLiteral(projectId);
    await tx.execAsync(`
      INSERT OR IGNORE INTO docs_scope_membership(
        auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
        state, access, read_only, updated_at
      )
      SELECT ${sqlLiteral(authScope)}, ${sqlLiteral(scopeKey)},
        ${sqlLiteral(workspaceId)}, ${projectSql}, ${sqlLiteral(tableName)},
        ${entityExpression}, 'active', ${accessSql}, ${readOnlySql}, CURRENT_TIMESTAMP
      FROM ${fromClause}
      WHERE ${whereClause};
    `);
  };
  let processedScope = false;

  for (const row of scopeRows) {
    const tableName = row.table_name ?? "";
    const prefix = "docs_scopes:v2:";
    if (!tableName.startsWith(prefix) || !row.cursor) continue;
    let scopes: Array<{
      workspace_id?: string;
    project_id?: string | null;
    access?: string;
    read_only?: boolean;
      source?: string;
    }> = [];
    try {
      const parsed = JSON.parse(row.cursor) as unknown;
      if (Array.isArray(parsed)) scopes = parsed.filter((scope): scope is {
        workspace_id?: string;
        project_id?: string | null;
        access?: string;
        read_only?: boolean;
        source?: string;
      } => Boolean(scope && typeof scope === "object"));
    } catch {
      continue;
    }
    const authScope = `auth:${tableName.slice(prefix.length)}`;
    for (const scope of scopes) {
      const workspaceId = scope.workspace_id;
      if (typeof workspaceId !== "string" || !workspaceId) continue;
      const rawProjectId = scope.project_id;
      if (rawProjectId != null && typeof rawProjectId !== "string") continue;
      if (scope.access != null && typeof scope.access !== "string") continue;
      if (scope.read_only != null && typeof scope.read_only !== "boolean") continue;
      if (scope.source != null && typeof scope.source !== "string") continue;
      processedScope = true;
      const projectId = rawProjectId ?? null;
      const scopeKey = `${workspaceId}|project:${projectId ?? ""}`;
      // A project_id=NULL shared scope identifies the same workspace as the
      // personal library.  Legacy rows cannot carry the scope source, so
      // bulk attribution would leak personal templates into a shared view.
      // Only the explicit personal owner scope is safe to backfill directly;
      // a validated pull will populate shared/non-owner memberships later.
      const canBulkAttributeLibrary =
        projectId == null
        && scope.source === "personal"
        && scope.access === "owner";
      if (projectId == null && !canBulkAttributeLibrary) {
        // Shared/non-owner roots reuse the workspace identifier of the
        // personal library.  No legacy table can disambiguate those rows, so
        // defer every membership (nodes and all relation/template tables) to
        // the next validated pull rather than exposing personal cache data.
        continue;
      }
      const workspaceSql = sqlLiteral(workspaceId);
      const projectPredicate = projectId == null
        ? "project_id IS NULL"
        : `project_id = ${sqlLiteral(projectId)}`;
      if (projectId != null || canBulkAttributeLibrary) {
        await insertMembership(
          authScope,
          scopeKey,
          workspaceId,
          projectId,
          "knowledge_nodes",
          "id",
          "knowledge_nodes",
          `workspace_id = ${workspaceSql} AND ${projectPredicate}`,
          scope.access ?? null,
          scope.read_only ?? null,
        );
      }

      // Tables without project_id are only safe to attribute directly to the
      // library scope.  For a project scope we still infer relation rows from
      // project-owned nodes below, which keeps revoke/quarantine safe on an
      // upgraded device without guessing ownership of standalone templates.
      if (projectId == null && canBulkAttributeLibrary) {
        const common = `workspace_id = ${workspaceSql}`;
        await insertMembership(
          authScope,
          scopeKey,
          workspaceId,
          projectId,
          "knowledge_supertags",
          "id",
          "knowledge_supertags",
          common,
          scope.access ?? null,
          scope.read_only ?? null,
        );
        await insertMembership(
          authScope,
          scopeKey,
          workspaceId,
          projectId,
          "knowledge_fields",
          "id",
          "knowledge_fields",
          common,
          scope.access ?? null,
          scope.read_only ?? null,
        );
      }
      const nodeProjectPredicate = projectId == null
        ? "n.project_id IS NULL"
        : `n.project_id = ${sqlLiteral(projectId)}`;
      await insertMembership(
        authScope,
        scopeKey,
        workspaceId,
        projectId,
        "knowledge_node_supertags",
        "CAST(node_id AS TEXT) || ':' || CAST(supertag_id AS TEXT)",
        "knowledge_node_supertags JOIN knowledge_nodes AS n ON n.id = knowledge_node_supertags.node_id",
        `n.workspace_id = ${workspaceSql} AND ${nodeProjectPredicate}`,
        null,
        null,
      );
      await insertMembership(
        authScope,
        scopeKey,
        workspaceId,
        projectId,
        "knowledge_field_values",
        "CAST(node_id AS TEXT) || ':' || CAST(field_id AS TEXT)",
        "knowledge_field_values JOIN knowledge_nodes AS n ON n.id = knowledge_field_values.node_id",
        `n.workspace_id = ${workspaceSql} AND ${nodeProjectPredicate}`,
        null,
        null,
      );
      await insertMembership(
        authScope,
        scopeKey,
        workspaceId,
        projectId,
        "knowledge_supertag_fields",
        "CAST(sf.supertag_id AS TEXT) || ':' || CAST(sf.field_id AS TEXT)",
        projectId == null
          ? "knowledge_supertag_fields AS sf JOIN knowledge_supertags AS s ON s.id = sf.supertag_id"
          : "knowledge_supertag_fields AS sf JOIN knowledge_node_supertags AS ns ON ns.supertag_id = sf.supertag_id JOIN knowledge_nodes AS n ON n.id = ns.node_id",
        projectId == null
          ? `s.workspace_id = ${workspaceSql}`
          : `n.workspace_id = ${workspaceSql} AND ${nodeProjectPredicate}`,
        null,
        null,
      );
      await insertMembership(
        authScope,
        scopeKey,
        workspaceId,
        projectId,
        "knowledge_node_placements",
        "knowledge_node_placements.id",
        "knowledge_node_placements JOIN knowledge_nodes AS n ON n.id = knowledge_node_placements.node_id",
        `n.workspace_id = ${workspaceSql} AND ${nodeProjectPredicate}`,
        null,
        null,
      );
      await insertMembership(
        authScope,
        scopeKey,
        workspaceId,
        projectId,
        "knowledge_edges",
        "knowledge_edges.id",
        "knowledge_edges JOIN knowledge_nodes AS n ON n.id = knowledge_edges.source_node_id",
        `n.workspace_id = ${workspaceSql} AND ${nodeProjectPredicate}`,
        null,
        null,
      );
    }
  }
  if (processedScope) {
    await tx.execAsync(`
      INSERT OR IGNORE INTO app_migrations(id, applied_at)
      VALUES ('${DOCS_SCOPE_MEMBERSHIP_BACKFILL_MARKER}', CURRENT_TIMESTAMP);
    `);
  }
}

/**
 * Recover the composite scope key for legacy Docs outbox rows without ever
 * guessing between sibling project scopes.  Payload metadata is authoritative
 * when it contains a valid workspace/project pair; otherwise a single active
 * membership for (auth, table, entity) is safe.  Ambiguous/legacy NULL rows
 * are terminally blocked instead of being attached to whichever scope happens
 * to revoke first.
 */
async function backfillDocsOutboxScopeKeysAsync(tx: AsyncSqliteExecutor): Promise<void> {
  const applied = await tx.getFirstAsync(
    `SELECT id FROM app_migrations WHERE id = '${DOCS_OUTBOX_SCOPE_BACKFILL_MARKER}' LIMIT 1;`,
  );
  if (applied) return;
  const docsTables = new Set([
    "knowledge_nodes",
    "knowledge_supertags",
    "knowledge_node_supertags",
    "knowledge_supertag_fields",
    "knowledge_fields",
    "knowledge_field_values",
    "knowledge_node_placements",
    "knowledge_edges",
  ]);
  type LegacyOutboxRow = {
    op_id?: string;
    table_name?: string;
    entity_id?: string;
    payload?: string | null;
    auth_scope?: string | null;
    docs_scope_key?: string | null;
  };
  type MembershipRow = {
    auth_scope?: string | null;
    table_name?: string;
    entity_key?: string;
    scope_key?: string;
    state?: string | null;
  };
  let rows: LegacyOutboxRow[] = [];
  let memberships: MembershipRow[] = [];
  try {
    rows = await tx.getAllAsync(`
      SELECT op_id, table_name, entity_id, payload, auth_scope, docs_scope_key
      FROM outbox
      WHERE docs_scope_key IS NULL
        AND table_name IN (
          'knowledge_nodes', 'knowledge_supertags',
          'knowledge_node_supertags', 'knowledge_supertag_fields',
          'knowledge_fields', 'knowledge_field_values',
          'knowledge_node_placements', 'knowledge_edges'
        );
    `) as LegacyOutboxRow[];
    memberships = await tx.getAllAsync(`
      SELECT auth_scope, table_name, entity_key, scope_key, state
      FROM docs_scope_membership
      WHERE state = 'active';
    `) as MembershipRow[];
  } catch {
    // Rolling upgrades/test doubles may not expose the new columns yet.  The
    // schema DDL is idempotent and a later startup will retry this migration.
    rows = [];
    memberships = [];
  }
  const membershipKeys = new Map<string, Set<string>>();
  for (const row of memberships) {
    if (
      typeof row.auth_scope !== "string"
      || typeof row.table_name !== "string"
      || typeof row.entity_key !== "string"
      || typeof row.scope_key !== "string"
      || !row.scope_key
    ) continue;
    const index = `${row.auth_scope}|${row.table_name}|${row.entity_key}`;
    const keys = membershipKeys.get(index) ?? new Set<string>();
    keys.add(row.scope_key);
    membershipKeys.set(index, keys);
  }
  const sqlLiteral = (value: string): string =>
    `'${value.replace(/'/g, "''")}'`;
  const validProject = (value: unknown): value is string | null =>
    value == null || typeof value === "string";
  for (const row of rows) {
    if (
      typeof row.op_id !== "string"
      || typeof row.table_name !== "string"
      || !docsTables.has(row.table_name)
      || typeof row.entity_id !== "string"
    ) continue;
    let scopeKey: string | null = null;
    try {
      const parsed = row.payload ? JSON.parse(row.payload) as unknown : null;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        const payload = parsed as Record<string, unknown>;
        const workspace = payload.workspace_id ?? payload.workspaceId;
        const project = payload.project_id ?? payload.projectId ?? null;
        if (
          typeof workspace === "string"
          && workspace.length > 0
          && validProject(project)
        ) {
          scopeKey = `${workspace}|project:${project ?? ""}`;
        }
      }
    } catch {
      // Invalid payloads are handled by the ambiguous branch below.
    }
    if (!scopeKey && typeof row.auth_scope === "string") {
      const keys = membershipKeys.get(
        `${row.auth_scope}|${row.table_name}|${row.entity_id}`,
      );
      if (keys?.size === 1) scopeKey = [...keys][0];
    }
    if (scopeKey) {
      await tx.execAsync(`
        UPDATE outbox
        SET docs_scope_key = ${sqlLiteral(scopeKey)}
        WHERE op_id = ${sqlLiteral(row.op_id)}
          AND docs_scope_key IS NULL;
      `);
    } else {
      await tx.execAsync(`
        UPDATE outbox
        SET blocked_reason = 'docs_scope_ambiguous',
            retry_count = 5,
            last_error = 'quarantine:scope_ambiguous'
        WHERE op_id = ${sqlLiteral(row.op_id)}
          AND docs_scope_key IS NULL;
      `);
    }
  }
  await tx.execAsync(`
    INSERT OR IGNORE INTO app_migrations(id, applied_at)
    VALUES ('${DOCS_OUTBOX_SCOPE_BACKFILL_MARKER}', CURRENT_TIMESTAMP);
  `);
}


async function runSchemaMaintenanceAsync(tx: AsyncSqliteExecutor): Promise<void> {
  const marker = await tx.getFirstAsync(
    `SELECT id FROM app_migrations WHERE id = '${SCHEMA_MAINTENANCE_V01142_MARKER}' LIMIT 1;`,
  ) as { id?: unknown } | null;
  if (marker?.id === SCHEMA_MAINTENANCE_V01142_MARKER) {
    // The coarse marker predates the individual backfill markers.  Always
    // revisit the helpers so a partial/legacy run can finish safely.
    await backfillDocsScopeMembershipAsync(tx);
    await backfillDocsOutboxScopeKeysAsync(tx);
    return;
  }

  // Older builds created this column as nullable. Preserve those rows and
  // normalize the legacy NULL representation before repositories rely on the
  // current non-null contract.
  await tx.execAsync(
    "UPDATE projects SET is_completed = 0 WHERE is_completed IS NULL;",
  );
  // Existing interrupted runs predate composite scope identity.  Preserve
  // their staged pages under the conservative library-only key so they can
  // resume without being attributed to a different project.
  await tx.execAsync(`
    UPDATE docs_sync_runs
    SET scope_key = COALESCE(scope_id, 'personal') || '|project:' || COALESCE(project_id, '')
    WHERE scope_key IS NULL OR scope_key = 'personal';
  `);
  await tx.execAsync(`
    UPDATE docs_sync_staging
    SET scope_key = COALESCE(scope_id, 'personal') || '|project:' || COALESCE(project_id, '')
    WHERE scope_key IS NULL OR scope_key = 'personal';
  `);
  await backfillDocsScopeMembershipAsync(tx);
  await backfillDocsOutboxScopeKeysAsync(tx);
  // v0.1.99 は401/408/409/429でもretry_countを消費し、5回でfailedへ
  // 固定していた。v0.1.100の初回起動だけ既存failed行を再キューし、
  // 新しい一時エラー処理で再ログイン・待機後に自動復旧できるようにする。
  // 恒久4xxは次回再送時に再びfailedへ戻るため、誤送信を継続しない。
  const clipRequeueApplied = await tx.getFirstAsync(
    `SELECT id FROM app_migrations WHERE id = '${PENDING_CLIP_REQUEUE_V0100_MARKER}' LIMIT 1;`,
  );
  if (!clipRequeueApplied) {
    await tx.execAsync(`
      UPDATE pending_clip_ingests
      SET status = 'queued', retry_count = 0,
          last_error = NULL, updated_at = CURRENT_TIMESTAMP
      WHERE status = 'failed';
    `);
    await tx.execAsync(`
      INSERT OR IGNORE INTO app_migrations(id, applied_at)
      VALUES ('${PENDING_CLIP_REQUEUE_V0100_MARKER}', CURRENT_TIMESTAMP);
    `);
  }
  // v0.1.102以前の端末には、Film配下を含む取り込み先がSQLiteへ残り得る。
  // v0.1.103の初回起動で全認証スコープの旧cacheを破棄し、オンライン時は
  // サーバー権威設定を再取得、オフライン時は誤保存せず保留キューへ送る。
  const filmPolicyApplied = await tx.getFirstAsync(
    `SELECT id FROM app_migrations WHERE id = '${CLIP_TARGET_FILM_POLICY_V0103_MARKER}' LIMIT 1;`,
  );
  if (!filmPolicyApplied) {
    await tx.execAsync(`DELETE FROM clip_ingest_target_cache;`);
    await tx.execAsync(`
      INSERT OR IGNORE INTO app_migrations(id, applied_at)
      VALUES ('${CLIP_TARGET_FILM_POLICY_V0103_MARKER}', CURRENT_TIMESTAMP);
    `);
  }
  // 既存outboxがある行は、ローカル編集時刻ではなくoutboxに保存された
  // サーバー基準版を引き継ぐ。create（base null）は null のままにし、
  // 未同期編集を端末時計の値でサーバー版と誤認しない。
  await tx.execAsync(`
    UPDATE knowledge_nodes
    SET server_updated_at = (
      SELECT base_updated_at FROM outbox
      WHERE table_name = 'knowledge_nodes'
        AND entity_id = knowledge_nodes.id
      ORDER BY created_at LIMIT 1
    ), dirty = 1
    WHERE EXISTS (
      SELECT 1 FROM outbox
      WHERE table_name = 'knowledge_nodes' AND entity_id = knowledge_nodes.id
    );
  `);
  await tx.execAsync(`
    UPDATE knowledge_supertags
    SET server_updated_at = (
      SELECT base_updated_at FROM outbox
      WHERE table_name = 'knowledge_supertags'
        AND entity_id = knowledge_supertags.id
      ORDER BY created_at LIMIT 1
    ), dirty = 1
    WHERE EXISTS (
      SELECT 1 FROM outbox
      WHERE table_name = 'knowledge_supertags' AND entity_id = knowledge_supertags.id
    );
  `);
  await tx.execAsync("UPDATE knowledge_node_supertags SET updated_at = COALESCE(updated_at, created_at) WHERE updated_at IS NULL;");
  await tx.execAsync(`
    UPDATE knowledge_node_supertags
    SET server_updated_at = (
      SELECT base_updated_at FROM outbox
      WHERE table_name = 'knowledge_node_supertags'
        AND entity_id = knowledge_node_supertags.node_id || ':' || knowledge_node_supertags.supertag_id
      ORDER BY created_at LIMIT 1
    ), dirty = 1
    WHERE EXISTS (
      SELECT 1 FROM outbox
      WHERE table_name = 'knowledge_node_supertags'
        AND entity_id = knowledge_node_supertags.node_id || ':' || knowledge_node_supertags.supertag_id
    );
  `);
  await tx.execAsync(`
    UPDATE knowledge_field_values
    SET server_updated_at = (
      SELECT base_updated_at FROM outbox
      WHERE table_name = 'knowledge_field_values'
        AND entity_id = knowledge_field_values.node_id || ':' || knowledge_field_values.field_id
      ORDER BY created_at LIMIT 1
    ), dirty = 1
    WHERE EXISTS (
      SELECT 1 FROM outbox
      WHERE table_name = 'knowledge_field_values'
        AND entity_id = knowledge_field_values.node_id || ':' || knowledge_field_values.field_id
    );
  `);
  // outbox がない既存行だけは、従来の updated_at を最後に観測した
  // サーバー版として引き継ぐ。既存outboxの行は上の値を保持する。
  await tx.execAsync("UPDATE knowledge_nodes SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_nodes' AND entity_id = knowledge_nodes.id);");
  await tx.execAsync("UPDATE knowledge_supertags SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_supertags' AND entity_id = knowledge_supertags.id);");
  await tx.execAsync("UPDATE knowledge_node_supertags SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_node_supertags' AND entity_id = knowledge_node_supertags.node_id || ':' || knowledge_node_supertags.supertag_id);");
  await tx.execAsync("UPDATE knowledge_field_values SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_field_values' AND entity_id = knowledge_field_values.node_id || ':' || knowledge_field_values.field_id);");
  await tx.execAsync("UPDATE tasks SET status = 'closed' WHERE status = 'done';");
  await tx.execAsync(
    "UPDATE task_occurrences SET status = 'closed' WHERE status = 'done';",
  );
  await tx.execAsync(`
    INSERT OR IGNORE INTO app_migrations(id, applied_at)
    VALUES ('${SCHEMA_MAINTENANCE_V01142_MARKER}', CURRENT_TIMESTAMP);
  `);
}

/** Apply DDL idempotently. Safe to call multiple times; no-op after the first. */
export function ensureSchema(): void {
  if (_applied || _asyncEnsurePromise) return;
  const db = getSqlite();
  db.withTransactionSync(() => {
    for (const stmt of DDL) {
      db.execSync(stmt);
    }
    for (const [tableName, columnName, ddl] of COLUMN_MIGRATIONS) {
      ensureColumn(tableName, columnName, ddl);
    }
    for (const stmt of INDEX_MIGRATIONS) {
      db.execSync(stmt);
    }
  });
  // Synchronous callers retain the historical schema bootstrap contract, but
  // intentionally leave large-table maintenance to ensureSchemaAsync().
  _applied = true;
}

/**
 * Bootstrap the local schema without blocking the JS thread on large caches.
 * The promise is shared while an upgrade is in flight; a rejected run clears
 * the slot so a later startup/retry can safely execute the transaction again.
 */
export function ensureSchemaAsync(): Promise<void> {
  if (_asyncSchemaReady) return Promise.resolve();
  if (_asyncEnsurePromise) return _asyncEnsurePromise;

  const db = getSqlite();
  const promise = Promise.resolve()
    .then(() => db.withExclusiveTransactionAsync(async (tx) => {
      // A synchronous caller may have completed the lightweight bootstrap
      // before this async upgrade starts.  In that case only maintenance is
      // missing; otherwise apply the full DDL/column/index bootstrap here.
      if (!_applied) {
        for (const stmt of DDL) {
          await tx.execAsync(stmt);
        }
        for (const [tableName, columnName, ddl] of COLUMN_MIGRATIONS) {
          const columns = await tx.getAllAsync<{ name?: string }>(
            `PRAGMA table_info(${tableName});`,
          );
          if (!columns.some((column) => column.name === columnName)) {
            await tx.execAsync(ddl);
          }
        }
        for (const stmt of INDEX_MIGRATIONS) {
          await tx.execAsync(stmt);
        }
      }
      await runSchemaMaintenanceAsync(tx);
    }))
    .then(() => {
      _applied = true;
      _asyncSchemaReady = true;
    })
    .catch((error) => {
      // Do not leave a rejected promise cached: retry must get a fresh
      // transaction and marker checks after SQLite rolls back.
      throw error;
    })
    .finally(() => {
      _asyncEnsurePromise = null;
    });
  _asyncEnsurePromise = promise;
  return promise;
}
