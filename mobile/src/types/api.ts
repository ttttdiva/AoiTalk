/**
 * AoiTalk API 型定義（frontend/src/lib/ から移植）
 */

// ========== チャット ==========

export interface ConversationSession {
  id: string;
  user_id: string;
  character_name: string;
  title: string;
  session_start?: string | null;
  last_activity?: string | null;
  message_count: number;
  is_active: boolean;
  project_id?: string | null;
}

export interface ConversationMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  metadata: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  token_count?: number | null;
  branch_count?: number | null;
  parent_message_id?: string | null;
  branch_index: number;
  is_active_branch: boolean;
}

// ========== タスク ==========

export interface Tag {
  id: string;
  project_id: string;
  name: string;
  color?: string | null;
  created_by?: string | null;
  created_at?: string | null;
}

export interface TaskAssignee {
  id: string;
  task_id: string;
  user_id: string;
  is_primary: boolean;
  display_name?: string | null;
  username?: string | null;
}

export interface TaskComment {
  id: string;
  task_id: string;
  user_id?: string | null;
  content: string;
  created_at?: string | null;
  updated_at?: string | null;
  username?: string | null;
  display_name?: string | null;
}

export interface TaskAttachment {
  id: string;
  task_id: string;
  project_id: string;
  file_path: string;
  display_name: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  kind: "image" | "file" | string;
  created_by?: string | null;
  created_at?: string | null;
  metadata?: Record<string, unknown>;
  url?: string;
}

export interface TimeEntry {
  id: string;
  task_id: string;
  occurrence_id?: string | null;
  user_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  source: string;
  note?: string | null;
  task_title?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  metadata?: Record<string, unknown>;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export interface TaskOccurrence {
  id: string;
  task_id: string;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  title?: string | null;
  status: string;
  start_at: string;
  end_at?: string | null;
  all_day: boolean;
  reminder_offsets: number[];
  source_kind?: string | null;
  is_generated?: boolean;
  tags?: Tag[];
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export interface Task {
  id: string;
  project_id: string;
  project_name?: string | null;
  project_color?: string | null;
  title: string;
  description?: string | null;
  status: string;
  priority: string;
  start_at?: string | null;
  end_at?: string | null;
  all_day: boolean;
  reminder_offsets: number[];
  notifications_enabled: boolean;
  source: string;
  created_by?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  metadata: Record<string, unknown>;
  assignees: TaskAssignee[];
  tags: Tag[];
  active_time_entry?: TimeEntry | null;
  estimated_hours?: number | null;
  sort_order?: number;
  total_time_seconds?: number;
  parent_task_id?: string | null;
  subtasks?: Task[];
  comments?: TaskComment[];
  attachments?: TaskAttachment[];
  has_recurrence?: boolean;
}

// ========== プロジェクト ==========

export interface Project {
  id: string;
  name: string;
  description?: string | null;
  slug: string;
  aliases?: string[];
  color?: string | null;
  metadata?: Record<string, unknown> | null;
  owner_id?: string | null;
  space_id?: string | null;
  allow_join_requests?: boolean;
  storage_quota_mb?: number;
  storage_used_mb?: number;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
  membership?: {
    role?: string | null;
    permissions?: Record<string, unknown> | null;
  } | null;
}

export interface Space {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  color?: string | null;
  owner_id?: string | null;
  sort_order?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProjectMember {
  id: string;
  project_id: string;
  user_id: string;
  role?: string | null;
  permissions?: Record<string, unknown> | null;
  username?: string | null;
  display_name?: string | null;
}

export interface ProjectStorageUsage {
  success: boolean;
  path: string;
  storage_root?: string;
  quota_mb?: number | null;
  usage?: {
    total_bytes?: number;
    total_mb?: number;
    file_count?: number;
    directory_count?: number;
  };
}

// ========== シナリオ ==========

export interface Scenario {
  id: string;
  title: string;
  description?: string | null;
  genre?: string | null;
  perspective?: string | null;
  setting?: string | null;
  opening_text?: string | null;
  gm_instructions?: string | null;
  tags?: string[];
  difficulty?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ScenarioCharacter {
  id: string;
  scenario_id: string;
  name: string;
  role: string;
  description?: string | null;
}

export interface ScenarioScene {
  id: string;
  scenario_id: string;
  title: string;
  description?: string | null;
  scene_type?: string | null;
  gm_instructions?: string | null;
  image_prompt?: string | null;
  sort_order?: number;
  status?: string | null;
}

export interface ScenarioEpisode {
  id: string;
  scenario_id: string;
  title: string;
  synopsis_sentence?: string | null;
  synopsis_paragraph?: string | null;
  synopsis_full?: string | null;
  status?: string | null;
  sort_order?: number;
}

export interface CanonEntry {
  id: string;
  scenario_id: string;
  category: string;
  fact: string;
  source_scene_id?: string | null;
}

export interface ScenarioDetail extends Scenario {
  characters: ScenarioCharacter[];
  scenes: ScenarioScene[];
  episodes?: ScenarioEpisode[];
}

export interface ScenarioWritingSession {
  id: string;
  scenario_id: string;
  conversation_session_id?: string | null;
  target_episode_id?: string | null;
  target_scene_id?: string | null;
  writing_prompt?: string;
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
  scenario?: Scenario | null;
}

// ========== TRPG ==========

export interface TrpgRoom {
  id: string;
  room_code: string;
  room_title: string;
  status: string;
  max_players: number;
  player_count: number;
  is_public: boolean;
  gm_mode: string;
  host_user_id?: string | null;
  perspective?: string | null;
  scenario?: Scenario | null;
  participants?: TrpgParticipant[];
  logs?: TrpgLog[];
  shared_state?: Record<string, unknown> | null;
  current_scene_id?: string | null;
  current_scene?: ScenarioScene | null;
  updated_at?: string | null;
}

export interface TrpgParticipant {
  id: string;
  play_session_id: string;
  user_id?: string | null;
  display_name: string;
  role: string;
  character_id?: string | null;
  avatar_url?: string | null;
  color?: string | null;
  seat_index?: number | null;
  pc_state?: Record<string, unknown> | null;
  is_connected?: boolean;
  is_active_participant?: boolean;
}

export interface TrpgLog {
  id: string;
  play_session_id: string;
  participant_id?: string | null;
  log_type: string;
  content: string;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
  participant_name?: string | null;
}

export interface TrpgPrivateMessage {
  id: string;
  play_session_id: string;
  sender_participant_id?: string | null;
  sender_label: string;
  target_participant_ids: string[];
  message_type: "private" | "gm" | "mention";
  content: string;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
}

// ========== WebSocket ==========

export interface ChatResponseModelSelection {
  provider: string;
  model: string;
}

export interface ChatResponseModelOption extends ChatResponseModelSelection {
  label: string;
  providerLabel: string;
  modelLabel: string;
  isCurrent?: boolean;
}

export interface LlmCatalogModelOption {
  id: string;
  label: string;
  description?: string;
  installed?: boolean;
  source?: string;
  source_label?: string;
  provider_configured?: boolean;
  custom_current?: boolean;
}

export interface LlmCatalogProvider {
  id: string;
  label: string;
  configured_model?: string;
  models: LlmCatalogModelOption[];
  settings?: {
    api_key_configured?: boolean;
  };
}

export interface LlmModelCatalogResponse {
  current: ChatResponseModelSelection;
  providers: LlmCatalogProvider[];
}

export interface WSMessage {
  type: string;
  content?: string;
  session_id?: string;
  [key: string]: unknown;
}

export interface UserMessagePayload {
  type: "user_message";
  data: {
    message: string;
    session_id: string;
    project_id?: string;
    agent_mode?: string;
    include_project_context?: boolean;
    edit_message_id?: string;
    response_model?: ChatResponseModelSelection;
  };
}

// ========== User Memories ==========

export interface UserMemory {
  id: string;
  user_id: string;
  content: string;
  source: "auto" | "manual";
  category: string;
  is_active: boolean;
  metadata: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

// ========== Project Records ==========

export interface RecordTable {
  id: string;
  project_id: string;
  name: string;
  description?: string | null;
  icon?: string | null;
  sort_order?: number | null;
  schema_version?: number | null;
  memory_policy?: string | null;
  default_sensitivity?: string | null;
  metadata?: Record<string, unknown> | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export interface RecordField {
  id: string;
  table_id: string;
  key: string;
  label: string;
  field_type: string;
  options?: Record<string, unknown> | null;
  required?: boolean;
  unique_value?: boolean;
  sort_order?: number | null;
  is_title?: boolean;
  is_due?: boolean;
  sensitivity?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export interface RecordRow {
  id: string;
  table_id: string;
  project_id: string;
  created_by?: string | null;
  values?: Record<string, unknown> | null;
  title?: string | null;
  status?: string | null;
  due_at?: string | null;
  search_text?: string | null;
  sensitivity?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

// ========== ファイラー ==========

export interface FilerEntry {
  name: string;
  type: "file" | "directory";
  path: string;
  size?: number;
  mime_type?: string;
  modified?: string;
  thumbnail?: string;
}

// ========== レポート ==========

export interface TimeReportBucket {
  key: string;
  label: string;
  seconds: number;
  entries: number;
  project_id?: string | null;
  project_name?: string | null;
}

export interface TimeReport {
  summary: {
    total_seconds: number;
    entry_count: number;
    active_entries: number;
  };
  by_project: TimeReportBucket[];
  by_day: TimeReportBucket[];
  by_user: TimeReportBucket[];
  by_task: TimeReportBucket[];
}

export interface ProjectNotificationSetting {
  id?: string;
  project_id?: string;
  discord_webhook_url?: string | null;
  default_reminder_offsets: number[];
  notify_overdue: boolean;
}

export interface UserNotificationPreferences {
  task_notification_minutes_before: number;
}

export interface UserSettings {
  custom_instructions?: string;
  restart_shortcut_enabled?: boolean;
  task_notification_minutes_before?: number;
  audio_player?: {
    playback_scope?: "folder_loop" | "global_next";
    shuffle?: boolean;
    repeat_one?: boolean;
  };
  calendar_view?: {
    selected_date?: string;
    show_closed?: boolean;
    hide_recurring?: boolean;
  };
  reports_view?: {
    range?: "7d" | "30d";
    timeline_days?: 7 | 30;
    show_schedule_frames?: boolean;
  };
}

export interface UserSettingsResponse {
  settings: UserSettings;
}

export interface AppSettings {
  external_llm: {
    auto_approve: boolean;
  };
  rag: {
    enabled: boolean;
  };
  reasoning: {
    enabled: boolean;
    display_mode: "silent" | "progress" | "detailed" | "debug";
  };
  agents: {
    filesystem: {
      enabled: boolean;
    };
    project_management: {
      enabled: boolean;
    };
    mcp: {
      enabled: boolean;
    };
    spotify: {
      enabled: boolean;
    };
  };
  spotify: {
    enabled: boolean;
  };
}

export interface AppSettingsResponse {
  settings: AppSettings;
  schema: Record<string, { type: string; values?: string[] }>;
}

// ========== 認証 ==========

export interface AuthResult {
  success: boolean;
  user_id?: string;
  username?: string;
  role?: string;
  access_token?: string;
  token_type?: string;
  expires_in?: number;
  is_password_reset_required?: boolean;
  error?: string;
}

export interface UserInfo {
  user_id: string;
  username: string;
  role: string;
}

// ========== キャラクター管理 ==========

export interface ManagedCharacter {
  id: string;
  name: string;
  slug: string;
  character_type?: string | null;
  description?: string | null;
  personality_summary?: string | null;
  model?: string | null;
  is_enabled?: boolean;
  avatar_image_path?: string | null;
  updated_at?: string | null;
}
