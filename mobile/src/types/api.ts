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
  is_group_chat?: boolean;
  app_id?: string | null;
  app_target_id?: string | null;
  development_status?: "working" | "waiting_for_user" | "completed" | null;
  last_read_at?: string | null;
  is_unread?: boolean;
  context?: Record<string, unknown> | null;
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
  timeline?: AgentRunTimelineItem[];
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

// ========== Story Studio（canonical /api/story） ==========

export type StoryWorkKind = "novel" | "trpg";

export interface StoryWork {
  id: string;
  user_id: string;
  title: string;
  synopsis?: string | null;
  plot?: string | null;
  style_guide?: string | null;
  kind: StoryWorkKind | string;
  status: string;
  target_episode_chars: number;
  planned_episode_count?: number | null;
  start_episode_id?: string | null;
  ui_state: Record<string, unknown>;
  model_override: Record<string, unknown>;
  image_settings: Record<string, unknown>;
  resolved_model?: string | null;
  model_layer?: string | null;
  episode_count?: number | null;
  char_count?: number | null;
  total_chars?: number | null;
  notes_count?: number | null;
  characters_count?: number | null;
  rulebooks_count?: number | null;
  branch_count?: number | null;
  route_episode_count?: number | null;
  route_chars?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
}

export interface StoryEpisode {
  id: string;
  work_id: string;
  title: string;
  plot?: string | null;
  summary?: string | null;
  summary_locked?: boolean;
  premise_note?: string | null;
  status: string;
  target_chars?: number | null;
  char_count: number;
  body?: string | null;
  body_etag?: string | null;
  map_x?: number | null;
  map_y?: number | null;
  sort_hint?: number;
  current_rev_no?: number;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
}

export interface StoryLink {
  id: string;
  work_id: string;
  from_episode_id: string;
  to_episode_id: string;
  choice_label?: string | null;
  position?: number;
  is_primary?: boolean;
  created_at?: string | null;
}

export interface StoryGraph {
  episodes: StoryEpisode[];
  links: StoryLink[];
  start_episode_id?: string | null;
}

/** Canonical /api/story/works/{work_id}/structure operations. */
export type StoryStructureOperation =
  | { op: "add_link"; from: string; to: string; choice_label?: string | null; is_primary?: boolean; position?: number }
  | { op: "remove_link"; id: string }
  | { op: "update_link"; id: string; choice_label?: string | null; position?: number; is_primary?: boolean }
  | { op: "insert_between"; link_id: string; episode_id: string }
  | { op: "set_start"; episode_id: string }
  | { op: "reorder_linear"; episode_ids: string[] }
  | { op: "duplicate_as_branch"; episode_id: string; choice_label?: string | null; new_title?: string | null };

export interface StoryOverview {
  work: StoryWork;
  graph: StoryGraph;
  current_route: string[];
}

export interface StoryCharacter {
  id: string;
  user_id: string;
  /** Present only on /works/{work_id}/characters projections. */
  work_id?: string;
  character_id?: string;
  name: string;
  aliases: string[];
  summary?: string | null;
  description?: string | null;
  notes?: string | null;
  ai_mode: string;
  keywords: string[];
  image_path?: string | null;
  role_note?: string | null;
  position?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
}

export interface StoryWorkCharacter {
  work_id: string;
  character_id: string;
  role_note?: string | null;
  position?: number;
}

export interface StoryRulebook {
  id: string;
  user_id: string;
  /** Present only on /works/{work_id}/rulebooks projections. */
  work_id?: string;
  rulebook_id?: string;
  name: string;
  content?: string | null;
  enabled?: boolean | null;
  position?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
}

export interface StoryWorkRulebook {
  work_id: string;
  rulebook_id: string;
  enabled: boolean;
  position: number;
}

export interface StoryNote {
  id: string;
  work_id: string;
  title: string;
  content?: string | null;
  ai_mode: string;
  keywords: string[];
  position: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface StoryEpisodeRevision {
  id: string;
  episode_id: string;
  rev_no: number;
  title?: string | null;
  plot?: string | null;
  body?: string | null;
  message?: string | null;
  origin: string;
  body_sha256: string;
  char_count: number;
  created_by: string;
  created_at?: string | null;
}

export interface StoryRevisionList {
  items: StoryEpisodeRevision[];
  limit: number;
  offset: number;
}

export interface StoryBodyUpdateResponse {
  id: string;
  body_etag?: string | null;
  char_count: number;
  current_rev_no: number;
  revision?: StoryEpisodeRevision | null;
  pre_revision?: StoryEpisodeRevision | null;
}

export interface StoryJob {
  id: string;
  work_id: string;
  kind: string;
  payload: Record<string, unknown>;
  status: string;
  progress: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface StoryWritingSession {
  id: string;
  work_id: string;
  episode_id?: string | null;
  conversation_session_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface StoryContextPreview {
  prompt: string;
  injected: Array<Record<string, string>>;
  model: Record<string, string>;
  resolved_model?: string | null;
  model_layer?: string | null;
  estimated_chars: number;
}

export interface StorySearchHit {
  episode_id: string;
  title: string;
  snippet: string;
  field?: string | null;
  match_start?: number | null;
  match_end?: number | null;
}

export interface StorySearchResponse {
  query: string;
  results: StorySearchHit[];
}

export interface StorySplitResponse {
  source: Record<string, unknown>;
  created: Record<string, unknown>;
  links: Record<string, unknown>;
}

/** Local-only conflict snapshot kept beside a durable manuscript draft. */
export interface StoryLocalDraft {
  auth_scope?: string;
  episode_id: string;
  body: string;
  expected_etag?: string | null;
  server_snapshot?: StoryEpisode | null;
  conflict_status: "draft" | "conflict";
  created_at?: string | null;
  updated_at?: string | null;
}

// ========== TRPG ==========

export interface TrpgRoom {
  id: string;
  /** Story Work backing this play session (the canonical API field). */
  work_id?: string;
  /** Canonical session title. */
  title?: string;
  /**
   * Legacy names are kept as optional read-only projections for the mobile
   * websocket adapter.  They are not sent back to the server.
   */
  room_code?: string | null;
  room_title?: string;
  status: string;
  gm_mode: string;
  host_user_id?: string | null;
  invite_code?: string | null;
  snapshot?: Record<string, unknown> | null;
  image_settings?: TrpgImageSettings | null;
  participants?: TrpgParticipant[];
  recent_events?: TrpgLog[];
  logs?: TrpgLog[];
  shared_state?: Record<string, unknown> | null;
  created_at?: string | null;
  current_scene_id?: string | null;
  /** Optional scene projection returned by older TRPG snapshots. */
  current_scene?: TrpgSceneSummary | null;
  updated_at?: string | null;
  ended_at?: string | null;
}

/** Minimal scene projection used by the TRPG room view; not a Story legacy model. */
export interface TrpgSceneSummary {
  id: string;
  title: string;
  description?: string | null;
}

export interface TrpgParticipant {
  id: string;
  /** Canonical API session relationship. */
  session_id?: string;
  /** Legacy projection consumed by existing log/state views. */
  play_session_id?: string;
  user_id?: string | null;
  display_name: string;
  role: string;
  story_character_id?: string | null;
  character_id?: string | null;
  is_npc?: boolean;
  joined_at?: string | null;
  left_at?: string | null;
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
  content: string;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
}

/** Canonical /api/trpg/sessions image generation settings projection. */
export interface TrpgImageSettings {
  enabled?: boolean;
  engine?: string;
  workflow_path?: string | null;
  style?: string;
  negative_prompt?: string;
  [key: string]: unknown;
}

/** Canonical participant-owned private state returned by the TRPG Play API. */
export interface TrpgPrivateState {
  id: string;
  session_id: string;
  participant_id: string;
  state: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

/** GM-visible projection; the backend only includes entries shared with GM. */
export interface TrpgGmPrivateState {
  participant_id: string;
  display_name?: string | null;
  state: Record<string, unknown>;
  updated_at?: string | null;
}

/** Read-only TRPG rulebook profile exposed by /api/trpg/rulesets. */
export interface TrpgRulesetProfile {
  key?: string;
  ruleset_key?: string;
  name?: string;
  label?: string;
  display_name?: string;
  enabled?: boolean;
  [key: string]: unknown;
}

export interface TrpgRulesetListResponse {
  rulesets: TrpgRulesetProfile[];
  count: number;
}

/** Read-only reference search bundle exposed by the canonical TRPG API. */
export interface TrpgReferenceBundle {
  rules: Array<Record<string, unknown>>;
  creatures: Array<Record<string, unknown>>;
  mechanic_links: Array<Record<string, unknown>>;
  count: number;
}

export interface TrpgReferenceStats {
  [key: string]: unknown;
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
  /** Enterprise deployment metadata may mark a provider unavailable. */
  available?: boolean;
  disabled?: boolean;
  unavailable?: boolean;
  availability_reason?: string | null;
  models: LlmCatalogModelOption[];
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
