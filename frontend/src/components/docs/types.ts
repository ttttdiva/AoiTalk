"use client";

import type { DocsFieldType } from "@/lib/docs-model";

export type DocsNode = {
  id: string;
  workspace_id: string;
  parent_id: string | null;
  root_page_id: string | null;
  project_id: string | null;
  system_key: string | null;
  title: string;
  aliases: string[];
  description: string;
  body_json: Record<string, unknown>;
  body_text: string;
  node_type: "node" | "search" | "day" | "system" | "page" | "block" | "object";
  display_props: Record<string, unknown>;
  query_json: Record<string, unknown> | null;
  view_json: Record<string, unknown>;
  day_date: string | null;
  sort_order: number;
  created_by?: string | null;
  updated_by?: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
};

export type DocsSupertag = {
  id: string;
  workspace_id: string;
  parent_supertag_id: string | null;
  system_key: string | null;
  name: string;
  base_type: string;
  description: string | null;
  color: string | null;
  icon: string | null;
  template_json: Record<string, unknown>;
  pinned_field_ids: string[];
  config_json: Record<string, unknown>;
  title_template: string | null;
  ai_instructions: string | null;
};

export type DocsField = {
  id: string;
  workspace_id: string;
  supertag_id: string | null;
  system_key: string | null;
  name: string;
  field_type: DocsFieldType | string;
  required: boolean;
  options_json: Record<string, unknown>;
  default_value_json: unknown;
  sort_order: number;
};

export type DocsSupertagField = {
  supertag_id: string;
  field_id: string;
  sort_order: number;
  required: boolean;
  show_in_template: boolean;
  optional: boolean;
};

export type DocsNodePlacement = {
  id: string;
  node_id: string;
  parent_node_id: string;
  sort_order: number;
  collapsed: boolean;
  created_by: string | null;
  created_at: string | null;
};

export type DocsFieldValue = {
  node_id: string;
  field_id: string;
  value_json: unknown;
  value_text: string | null;
  value_number: number | null;
  value_datetime: string | null;
  target_node_id: string | null;
};

export type DocsAttachment = {
  id: string;
  node_id: string;
  file_name: string;
  file_path: string;
  mime_type: string | null;
  size_bytes: number | null;
  metadata: Record<string, unknown>;
  created_by: string | null;
  created_at: string | null;
};

export type DocsNodeSupertag = {
  node_id: string;
  supertag_id: string;
};

export type DocsProject = {
  id: string;
  name: string;
  space_id: string | null;
  color: string | null;
};

export type DocsSavedView = {
  id: string;
  workspace_id: string;
  supertag_id: string | null;
  name: string;
  layout: "table" | "board" | "calendar" | "list" | string;
  config_json: Record<string, unknown>;
  sort_order: number;
  created_by?: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type DocsAiSuggestion = {
  id: string;
  workspace_id: string;
  node_id: string | null;
  suggestion_type: string;
  payload_json: Record<string, unknown>;
  status: "proposed" | "accepted" | "rejected" | "stale" | string;
  confidence: number | null;
  created_by?: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type DocsState = {
  nodes: DocsNode[];
  /** APIが子の存在を確認済みのノードID。子本体は必要時に遅延取得する。 */
  has_children_ids?: string[];
  /** APIが直下の子一覧を返した親ノードID。空配列なら未取得と区別する。 */
  loaded_children_parent_ids?: string[];
  /** Field値・逐語本文・bookmark等の詳細を専用APIで取得済みのノードID。 */
  details_loaded_ids?: string[];
  /** 子以外にも展開時に取得・表示する詳細があるノードID。 */
  has_details_ids?: string[];
  /** 親ごとの次ページcursor。nullは直下を最後まで取得済み。 */
  children_next_cursor_by_parent?: Record<string, string | null>;
  child_count_by_parent?: Record<string, number>;
  supertags: DocsSupertag[];
  node_supertags: DocsNodeSupertag[];
  supertag_fields: DocsSupertagField[];
  placements: DocsNodePlacement[];
  fields: DocsField[];
  field_values: DocsFieldValue[];
  attachments: DocsAttachment[];
  views: DocsSavedView[];
  ai_suggestions: DocsAiSuggestion[];
  projects: DocsProject[];
};

export type DocsReference = {
  node: DocsNode;
  kind: "placement" | "inline_ref" | "field_ref" | "reference-edge" | "wikilink" | "docs-ref";
  snippet: string;
  field_name?: string;
};

export type ReferencesState = {
  backlinks: DocsReference[];
  referenced_in: DocsReference[];
  field_refs: DocsReference[];
  outgoing: DocsReference[];
};

export type ViewMode = "document" | "saved-view" | "supertags";

export type FieldDraft = {
  name: string;
  field_type: DocsFieldType;
  required: boolean;
  options: string;
  default_value: string;
};

export type TagDraft = {
  id: string | null;
  name: string;
  base_type: string;
  parent_supertag_id: string;
  color: string;
  icon: string;
  description: string;
  title_template: string;
  template_json: string;
  ai_instructions: string;
};

export type SaveState = "idle" | "dirty" | "saving" | "error";

export const EMPTY_STATE: DocsState = {
  nodes: [],
  has_children_ids: [],
  loaded_children_parent_ids: [],
  details_loaded_ids: [],
  has_details_ids: [],
  children_next_cursor_by_parent: {},
  child_count_by_parent: {},
  supertags: [],
  node_supertags: [],
  supertag_fields: [],
  placements: [],
  fields: [],
  field_values: [],
  attachments: [],
  views: [],
  ai_suggestions: [],
  projects: [],
};

export const EMPTY_REFERENCES: ReferencesState = {
  backlinks: [],
  referenced_in: [],
  field_refs: [],
  outgoing: [],
};
