/**
 * Mobile domain/view-model compatibility types.
 *
 * The FastAPI wire contract is generated in `api-types.gen.ts` from the
 * shared `contracts/openapi/fastapi.json` artifact.  The interfaces below
 * remain only as native adapters while call sites migrate; new wire DTOs
 * must be generated rather than copied from the Web client.
 */

export type {
  components as GeneratedApiComponents,
  paths as GeneratedApiPaths,
} from "./api-types.gen";

import type { components } from "./api-types.gen";

// ========== チャット ==========

/** Server-owned state projection used by every native conversation history surface. */
export type ConversationHistoryStatus =
  components["schemas"]["ConversationHistorySessionProjection"]["history_status"];

export interface ConversationSession {
  /** Native-only state: owned locally, not yet registered on the server. */
  is_local_only?: boolean;
  id: string;
  user_id: string;
  character_name: string;
  title: string;
  session_start?: string | null;
  last_activity?: string | null;
  message_count: number;
  is_active: boolean;
  is_pinned?: boolean;
  pinned_at?: string | null;
  pin_updated_at?: string | null;
  project_id?: string | null;
  is_group_chat?: boolean;
  app_id?: string | null;
  app_target_id?: string | null;
  /** Agent主体のbinding。キャラクターpresentationとは独立している。 */
  agent_id?: string | null;
  agent_display_name?: string | null;
  agent_state?: string | null;
  latest_revision_id?: string | null;
  agent_character_id?: string | null;
  /** Offline binding is waiting for the dedicated CAS endpoint. */
  agent_binding_pending?: boolean;
  agent_binding_error?: string | null;
  development_status?: "working" | "waiting_for_user" | "completed" | null;
  /**
   * Server-owned, cross-surface history state.  Mobile must not infer this
   * from development_status or from a per-session generation poll.
   */
  history_status?: ConversationHistoryStatus | null;
  /** Completion fence for the latest assistant response used by read races. */
  history_completed_at?: string | null;
  last_read_at?: string | null;
  is_unread?: boolean;
  context?: Record<string, unknown> | null;
  /** Durable server-owned marker for the private AoiDots root. */
  private_agent_root?: boolean | null;
  purpose?: string | null;
  readonly_binding?: boolean | null;
  private_agent_root_metadata?: Record<string, unknown> | null;
  parent_session_id?: string | null;
  forked_from_message_id?: string | null;
  group_character_names?: string[];
  participants?: Array<Record<string, unknown>>;
  rp_settings?: Record<string, number>;
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
  /** Server-owned sender projection; Agent messages are never shown as Human. */
  sender_type?: "user" | "human" | "agent" | "character" | "assistant" | "service" | string | null;
  sender_id?: string | null;
  sender_display_name?: string | null;
}

export interface AgentRunTimelineItem {
  id: string;
  source: "event" | "tool_call" | string;
  run_id: string;
  event_type?: string | null;
  status?: string | null;
  display_status?: string | null;
  actor_type?: string | null;
  actor_label?: string | null;
  action: string;
  message?: string | null;
  tool_name?: string | null;
  arguments?: Record<string, unknown>;
  result?: string | null;
  result_preview?: string | null;
  error?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string | null;
}

export interface AgentRun {
  id: string;
  run_type: string;
  status: string;
  title?: string;
  error?: string | null;
  agent_id?: string | null;
  revision_id?: string | null;
  agent_revision_id?: string | null;
  work_item_id?: string | null;
  session_id?: string | null;
  task_id?: string | null;
  acting_subagent_id?: string | null;
  previous_attempt_run_id?: string | null;
  resolved_execution_manifest?: Record<string, unknown> | null;
  execution_manifest?: Record<string, unknown> | null;
  execution_manifest_hash?: string | null;
  timeline?: AgentRunTimelineItem[];
}

/** Safe Agent catalog projection. Never contains authority, tools, or prompts. */
export interface ConversationAgent {
  id: string;
  display_name: string;
  state?: string | null;
  latest_revision_id?: string | null;
  character_id?: string | null;
  character_name?: string | null;
  updated_at?: string | null;
  utilization?: {
    active_sessions?: number;
    active_runs?: number;
    [key: string]: unknown;
  } | null;
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
  deletion_batch_id?: string | null;
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
  /** Canonical RRULE occurrence start (actual start may include an override). */
  original_start_at?: string | null;
  all_day: boolean;
  reminder_offsets: number[];
  auto_close_on_due?: boolean;
  source_kind?: string | null;
  is_generated?: boolean;
  tags?: Tag[];
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
  deletion_batch_id?: string | null;
}

/** Canonical recurrence rule returned by /api/tasks/{task_id}/recurrence. */
export interface TaskRecurrence {
  id?: string;
  task_id?: string;
  rrule: string;
  timezone?: string | null;
  horizon_days?: number | null;
  trigger_status?: string | null;
  create_new?: boolean | null;
  recur_forever?: boolean | null;
  reset_status_to?: string | null;
  end_count?: number | null;
  end_date?: string | null;
  skip_weekend?: boolean | null;
  skip_holiday?: boolean | null;
  skip_mode?: "shift_forward" | "omit" | string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export type TaskRecurrencePayload = Partial<
  Omit<TaskRecurrence, "id" | "task_id" | "created_at" | "updated_at">
>;

export type TaskReferenceType =
  | "conversation_session"
  | "conversation_message"
  | "docs_node"
  | "task"
  | "workspace_file"
  | "url";

export interface TaskReference {
  id: string;
  task_id: string;
  reference_type: TaskReferenceType;
  relation_type: "source" | "related" | string;
  target_id?: string | null;
  target_path?: string | null;
  target_url?: string | null;
  display_name?: string | null;
  subtitle?: string | null;
  exists?: boolean;
  can_remove?: boolean;
  open?: { id?: string | null; path?: string | null; url?: string | null };
  metadata?: Record<string, unknown> | null;
}

export interface TaskReferencePayload {
  reference_type: TaskReferenceType;
  relation_type?: "source" | "related";
  target_id?: string | null;
  target_path?: string | null;
  target_url?: string | null;
  display_name?: string | null;
  metadata?: Record<string, unknown> | null;
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
  /** Close this task when its end_at due boundary is reached. */
  auto_close_on_due?: boolean;
  reminder_offsets: number[];
  notifications_enabled: boolean;
  source: string;
  created_by?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** Server tombstone timestamp. Deleted tasks are never resurrected by sync. */
  deleted_at?: string | null;
  deletion_batch_id?: string | null;
  metadata: Record<string, unknown>;
  assignees: TaskAssignee[];
  /**
   * Whether the response carried an authoritative assignee collection.
   *
   * SQLite task rows do not store this aggregate, and older task endpoints
   * may omit it (or expose only primary_assignee_user_id).  Mobile uses this
   * marker to avoid turning an unknown collection into an explicit clear
   * during an unrelated PATCH.  It is a client-side response marker, not a
   * wire request field.
   */
  assignees_complete?: boolean;
  tags: Tag[];
  /** See assignees_complete; omitted/null tag aggregates are not clears. */
  tags_complete?: boolean;
  active_time_entry?: TimeEntry | null;
  estimated_hours?: number | null;
  sort_order?: number;
  total_time_seconds?: number;
  parent_task_id?: string | null;
  subtasks?: Task[];
  comments?: TaskComment[];
  attachments?: TaskAttachment[];
  has_recurrence?: boolean;
  recurrence_rule?: TaskRecurrence | null;
}

/** Response returned by the retention-window task restore endpoint. */
export interface TaskRestoreResponse {
  id: string;
  task_id?: string;
  task_ids?: string[];
  deletion_batch_id?: string | null;
  restored_at?: string | null;
  restored?: boolean;
  idempotent?: boolean;
  updated_at?: string | null;
  deleted_at?: string | null;
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
  is_completed?: boolean;
  allow_join_requests?: boolean;
  storage_quota_mb?: number;
  storage_used_mb?: number;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
  /** Per-user operational scope, independent of global-admin read access. */
  is_participating?: boolean | null;
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

export interface TaskAssigneeCandidate {
  user_id: string;
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

// ========== WebSocket ==========

export type ChatResponseModelSelection = components["schemas"]["ResponseModelSelection"];

export interface ChatResponseModelOption extends ChatResponseModelSelection {
  label: string;
  providerLabel: string;
  modelLabel: string;
  reasoningEffortOptions?: string[];
  reasoningEffortDefault?: string | null;
  reasoningEffortKind?: string;
  supportsReasoning?: boolean;
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
  reasoning_effort_options?: string[];
  reasoning_effort_default?: string | null;
  reasoning_effort_kind?: string;
  supports_reasoning?: boolean;
}

export interface LlmCatalogProvider {
  id: string;
  label: string;
  configured_model?: string;
  /** Enterprise deployment metadata may mark a provider unavailable. */
  available?: boolean;
  disabled?: boolean;
  unavailable?: boolean;
  availability_reason?: string | null;
  models: LlmCatalogModelOption[];
  /** Runtime-served models for chat; settings keep the full catalog above. */
  chat_models?: LlmCatalogModelOption[];
  available_models?: LlmCatalogModelOption[];
  availability?: {
    state?: string;
    error?: string | null;
  };
  settings?: {
    api_key_configured?: boolean;
  };
}

/**
 * Effective server-side LLM deployment constraints.
 *
 * This metadata is intentionally optional: older servers and direct-mobile
 * provider flows do not send it, so their existing behaviour remains intact.
 */
export interface LlmDeploymentMetadata {
  backend?: string | null;
  transport?: string | null;
  fixed?: boolean | null;
  ready?: boolean | null;
  effective_provider?: string | null;
  effective_model?: string | null;
  fixed_provider?: string | null;
  fixed_model?: string | null;
  allowed_provider_ids?: unknown;
  unavailable_provider_ids?: unknown;
  reason?: string | null;
  persisted?: {
    provider?: string | null;
    model?: string | null;
    base_url?: string | null;
  } | null;
  effective?: {
    provider?: string | null;
    model?: string | null;
    base_url?: string | null;
    server_profile?: string | null;
    tool_capability?: string | null;
  } | null;
}

export interface LlmModelCatalogResponse {
  current: ChatResponseModelSelection;
  providers: LlmCatalogProvider[];
  provider_visibility?: {
    hidden_provider_ids?: unknown;
  };
  deployment?: LlmDeploymentMetadata | null;
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

// ========== Scoped Memory ==========

export type MemoryScope = "global" | "user" | "project" | "task" | "session";

/** Versioned memory projection returned by the canonical Scoped Memory API. */
export interface ScopedMemory {
  id: string;
  user_id?: string | null;
  project_id?: string | null;
  task_id?: string | null;
  session_id?: string | null;
  scope_type: MemoryScope;
  scope_id?: string | null;
  memory_type: string;
  title?: string | null;
  content: string;
  structured_data?: Record<string, unknown>;
  source_type: string;
  source_ref?: string | null;
  confidence?: number;
  importance?: number;
  trust_level?: string | null;
  sensitivity?: string | null;
  evidence_refs?: Array<Record<string, unknown>>;
  evidence_span?: Record<string, unknown>;
  dedupe_key?: string | null;
  supersedes_id?: string | null;
  version: number;
  created_by_actor?: string | null;
  rejection_reason?: string | null;
  projection_metadata?: Record<string, unknown>;
  migration_id?: string | null;
  status: string;
  is_pinned?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
}

/** Legacy symbol retained only as a type alias for existing screen imports. */
export type UserMemory = ScopedMemory;

export interface ScopedMemorySettings {
  user_auto_enabled: boolean;
  project_auto_enabled: boolean | null;
  project_id: string | null;
}

export interface ScopedMemoryMutation {
  success: boolean;
  memory_id: string;
  scope: MemoryScope;
  scope_id?: string | null;
  operation: string;
  replaced_id?: string | null;
  reason: string;
  memory?: ScopedMemory;
  project_information_node_id?: string;
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
    range?: "7d" | "30d" | "custom";
    timeline_days?: 7 | 30;
    show_schedule_frames?: boolean;
  };
  clip_ingest?: {
    targets?: ClipIngestTargetSetting[];
  };
  llm_provider_visibility?: {
    hidden_provider_ids?: string[];
  };
}

/** クリップ取り込み先（サーバー `user_settings.clip_ingest.targets` の1件）。 */
export interface ClipIngestTargetSetting {
  node_id?: string | null;
  node_system_key?: string | null;
  label?: string | null;
  breadcrumb?: string[] | null;
  routing_hint?: string | null;
  enabled?: boolean | null;
  fallback?: boolean | null;
}

export interface UserSettingsResponse {
  settings: UserSettings;
}

export interface AppSettings {
  external_llm: {
    auto_approve: boolean;
  };
  search: {
    provider: string;
    knowledge_enabled: boolean;
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

export interface CharacterProfileSnapshot {
  name: string;
  slug: string;
  character_type?: string | null;
  system_prompt?: string | null;
  description?: string | null;
  personality_summary?: string | null;
  scenario?: string | null;
  example_messages?: string | null;
}

export interface ManagedCharacter extends CharacterProfileSnapshot {
  id: string;
  model?: string | null;
  allowed_tools?: string[];
  is_enabled?: boolean;
  greeting?: string | null;
  invalid_content_reply?: string | null;
  fallback_reply?: string | null;
  goodbye_reply?: string | null;
  recognition_aliases?: string[];
  first_message?: string | null;
  alternate_greetings?: string[];
  auto_image_gen?: boolean;
  image_gen_trigger?: string | null;
  image_gen_interval?: number | null;
  appearance_tags?: string | null;
  negative_tags?: string | null;
  image_gen_engine?: string | null;
  comfyui_config?: Record<string, unknown>;
  avatar_image_path?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ========== Docs（アウトライン型ナレッジ） ==========
//
// 詳細設計書 1.4 / 2.9 の JSON スキーマに対応。サーバ（docs_sync.py）の
// serialize* と同一キー名・同一意味で往復する。body_json / body_text は
// 復号済み平文。このセクションは mobile-data の単独所有（mobile-ui は import のみ）。

/** ノード種別。page/block/object はサーバ側で node に正規化される。 */
export type DocsNodeType = "node" | "search" | "day" | "system";

/** User-editable multiline Docs block stored on a child KnowledgeNode. */
export type DocsBlockType = "markdown" | "code";

export interface DocsBlockBodyJson extends Record<string, unknown> {
  format: "doc_block";
  block_type: DocsBlockType;
  content: string;
  label: string;
  clip_ingest?: Record<string, unknown>;
}

/** フィールド型（Web docs-model.ts の 11 種と一致）。 */
export type DocsFieldType =
  | "text"
  | "long_text"
  | "options"
  | "options_from_supertag"
  | "number"
  | "date"
  | "checkbox"
  | "url"
  | "email"
  | "user"
  | "reference";

export interface DocsNode {
  id: string;
  workspace_id: string | null;
  parent_id: string | null;
  root_page_id: string | null;
  project_id: string | null;
  /** Docs scope metadata supplied by sync (personal/shared/project). */
  source?: "personal" | "shared" | "project" | string | null;
  access?: "owner" | "read" | "write" | string | null;
  read_only?: boolean;
  system_key: string | null;
  /** Server-derived discriminator for a persisted blank paragraph. */
  is_explicit_blank?: boolean;
  /** Server-authoritative Project-information title (additive sync field). */
  canonical_title?: string | null;
  lifecycle?: Record<string, unknown> | null;
  title: string;
  aliases: string[];
  description: string | null;
  body_json: Record<string, unknown> | null;
  body_text: string | null;
  node_type: DocsNodeType;
  display_props: Record<string, unknown> | null;
  query_json: Record<string, unknown> | null;
  view_json: Record<string, unknown> | null;
  day_date: string | null;
  sort_order: number | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
}

export interface DocsSupertag {
  id: string;
  workspace_id: string | null;
  parent_supertag_id: string | null;
  system_key: string | null;
  name: string;
  base_type: string | null;
  description: string | null;
  icon: string | null;
  color: string | null;
  template_json: Record<string, unknown> | null;
  pinned_field_ids: string[];
  config_json: Record<string, unknown> | null;
  title_template: string | null;
  ai_instructions: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface DocsField {
  id: string;
  workspace_id: string | null;
  supertag_id: string | null;
  system_key: string | null;
  name: string;
  field_type: DocsFieldType;
  required: boolean;
  options_json: Record<string, unknown> | null;
  default_value_json: unknown;
  sort_order: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface DocsSupertagField {
  supertag_id: string;
  field_id: string;
  sort_order: number | null;
  required: boolean;
  show_in_template: boolean;
  optional: boolean;
  created_at: string | null;
}

export interface DocsNodeSupertag {
  node_id: string;
  supertag_id: string;
  created_at: string | null;
  updated_at?: string | null;
  created_by: string | null;
}

export interface DocsFieldValue {
  node_id: string;
  field_id: string;
  value_json: unknown;
  value_text: string | null;
  value_number: number | null;
  value_datetime: string | null;
  target_node_id: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface DocsNodePlacement {
  id: string;
  node_id: string;
  parent_node_id: string;
  sort_order: number | null;
  collapsed: boolean;
  created_by: string | null;
  created_at: string | null;
}

export interface DocsEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  relation_type: string | null;
  confidence: number | null;
  created_by: string | null;
  created_at: string | null;
}

export interface DocsSearchHit {
  id: string;
  title: string;
  tags: string[];
  project_id: string | null;
  parent_title: string | null;
}
