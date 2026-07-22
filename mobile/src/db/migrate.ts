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
     role TEXT,
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

  // ---------- Docs（アウトライン型ナレッジ） ----------
  `CREATE TABLE IF NOT EXISTS knowledge_nodes (
     id TEXT PRIMARY KEY,
     workspace_id TEXT,
     parent_id TEXT,
     root_page_id TEXT,
     project_id TEXT,
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
];

let _applied = false;

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

/** Apply DDL idempotently. Safe to call multiple times; no-op after the first. */
export function ensureSchema(): void {
  if (_applied) return;
  const db = getSqlite();
  db.withTransactionSync(() => {
    for (const stmt of DDL) {
      db.execSync(stmt);
    }
    ensureColumn(
      "tasks",
      "notifications_enabled",
      "ALTER TABLE tasks ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1;",
    );
    ensureColumn(
      "tasks",
      "estimated_hours",
      "ALTER TABLE tasks ADD COLUMN estimated_hours REAL;",
    );
    ensureColumn(
      "tasks",
      "parent_task_id",
      "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;",
    );
    const docsColumns: Array<[string, string, string]> = [
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
    ];
    for (const [tableName, columnName, ddl] of docsColumns) {
      ensureColumn(tableName, columnName, ddl);
    }
    // 既存outboxがある行は、ローカル編集時刻ではなくoutboxに保存された
    // サーバー基準版を引き継ぐ。create（base null）は null のままにし、
    // 未同期編集を端末時計の値でサーバー版と誤認しない。
    db.execSync(`
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
    db.execSync(`
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
    db.execSync("UPDATE knowledge_node_supertags SET updated_at = COALESCE(updated_at, created_at) WHERE updated_at IS NULL;");
    db.execSync(`
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
    db.execSync(`
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
    db.execSync("UPDATE knowledge_nodes SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_nodes' AND entity_id = knowledge_nodes.id);");
    db.execSync("UPDATE knowledge_supertags SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_supertags' AND entity_id = knowledge_supertags.id);");
    db.execSync("UPDATE knowledge_node_supertags SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_node_supertags' AND entity_id = knowledge_node_supertags.node_id || ':' || knowledge_node_supertags.supertag_id);");
    db.execSync("UPDATE knowledge_field_values SET server_updated_at = updated_at WHERE server_updated_at IS NULL AND NOT EXISTS (SELECT 1 FROM outbox WHERE table_name = 'knowledge_field_values' AND entity_id = knowledge_field_values.node_id || ':' || knowledge_field_values.field_id);");
    db.execSync("UPDATE tasks SET status = 'closed' WHERE status = 'done';");
    db.execSync(
      "UPDATE task_occurrences SET status = 'closed' WHERE status = 'done';",
    );
  });
  _applied = true;
}
