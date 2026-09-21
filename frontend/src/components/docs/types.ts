"use client";

import type { DocsFieldType } from "@/lib/docs-model";
import { isExplicitBlankParagraph } from "@/lib/docs-block-model";

/**
 * Server-owned lifecycle projection for a Docs node.
 *
 * The API deliberately keeps this DTO additive so older clients can continue
 * to render a node.  Consumers must nevertheless treat an unknown/missing
 * lifecycle on a system-keyed canonical node as protected (fail closed).
 */
export type DocsNodeLifecycle = {
  /** Stable category supplied by the lifecycle authority. */
  kind?: string | null;
  type?: string | null;
  category?: string | null;
  /** Lifecycle state (for example active, retained, stale, unresolved). */
  state?: string | null;
  status?: string | null;
  phase?: string | null;
  ownership?: string | null;
  reason?: string | null;
  detail?: string | null;
  /** Whether this is the current Project-information identity. */
  canonical?: boolean;
  active?: boolean;
  retained?: boolean;
  resolved?: boolean;
  pointer_valid?: boolean;
  pointer_state?: string | null;
  pointer?: { valid?: boolean; state?: string | null } | null;
  /** Server-normalized identity title for a Project canonical root. */
  canonical_title?: string | null;
  expected_title?: string | null;
  expectedTitle?: string | null;
  canonicalTitle?: string | null;
  project_title?: string | null;
  project_name?: string | null;
  title_valid?: boolean;
  titleValid?: boolean;
  pointer_id?: string | null;
  /** Explicit per-action capabilities. Missing values are not permissions. */
  can_archive?: boolean;
  can_delete?: boolean;
  can_rename?: boolean;
  can_move?: boolean;
  can_taskify?: boolean;
  can_tag?: boolean;
  can_edit_title?: boolean;
  can_edit_content?: boolean;
  actions?: Partial<Record<DocsNodeMutation, boolean>>;
  /** Optional API version for forward-compatible projections. */
  version?: number | string | null;
};

export type DocsNodeMutation =
  | "archive"
  | "delete"
  | "rename"
  | "move"
  | "taskify"
  | "tag"
  | "title"
  | "content";

/** Extract a Project id from a canonical/duplicate identity key without
 * treating an arbitrary title or chip as identity. */
export function docsProjectIdFromSystemKey(systemKey: string | null | undefined): string | null {
  const match = typeof systemKey === "string"
    ? systemKey.trim().match(/^project_information:(?:duplicate:)?([^:]+)(?::|$)/u)
    : null;
  return match?.[1] ? match[1] : null;
}

/** Shared blank predicate for UI structural actions.
 *
 * Once the server sends the SQL-visible discriminator it is authoritative;
 * a contradictory encrypted marker must not make a row appear blank after a
 * failed/tampered migration.  Older payloads without the additive column use
 * the strict body marker as a compatibility fallback.
 */
export function isDocsExplicitBlankNode(
  node: Pick<DocsNode, "title" | "node_type" | "system_key"> & Partial<Pick<DocsNode, "body_json" | "is_explicit_blank">>,
) {
  if (typeof node.is_explicit_blank === "boolean") {
    return node.is_explicit_blank === true
      && node.title === ""
      && node.node_type === "node"
      && !node.system_key;
  }
  return isExplicitBlankParagraph(node.title, node.body_json, node.node_type);
}

const PROTECTED_LIFECYCLE_STATES = new Set([
  "active",
  "retained",
  "protected",
  "unresolved",
  "pointer_invalid",
  "pointer-invalid",
  "invalid_pointer",
  "invalid-pointer",
  "unknown",
  "missing",
  "library_missing",
  "library-missing",
  "hub_invalid",
  "hub-invalid",
  "stale",
  "orphan",
  "orphaned",
]);

const CANONICAL_LIFECYCLE_KINDS = new Set([
  "canonical",
  "project_canonical",
  "project-canonical",
  "project_information",
  "project-information",
  "project_information_root",
  "project-information-root",
  "protected",
]);

const STALE_LIFECYCLE_KINDS = new Set([
  "stale",
  "stale_orphan",
  "stale-orphan",
  "orphan",
  "orphaned",
  "project_orphan",
  "project-orphan",
]);

function lifecycleRecord(node: Pick<DocsNode, "system_key"> & { lifecycle?: DocsNodeLifecycle | null; lifecycle_state?: string | null; lifecycle_kind?: string | null; lifecycle_dto?: DocsNodeLifecycle | null; project_lifecycle?: DocsNodeLifecycle | null; project_information_lifecycle?: DocsNodeLifecycle | null }) {
  const candidate = node.lifecycle ?? node.lifecycle_dto ?? node.project_lifecycle ?? node.project_information_lifecycle;
  return candidate && typeof candidate === "object" ? candidate : null;
}

/** Return the additive lifecycle projection without trusting arbitrary text. */
export function docsNodeLifecycle(node: Pick<DocsNode, "system_key"> & Partial<Pick<DocsNode, "lifecycle" | "lifecycle_state" | "lifecycle_kind" | "lifecycle_dto" | "project_lifecycle" | "project_information_lifecycle">>): DocsNodeLifecycle | null {
  const record = lifecycleRecord(node);
  if (record) return record;
  const legacyState = node.lifecycle_state;
  const legacyKind = node.lifecycle_kind;
  if (typeof legacyState === "string" || typeof legacyKind === "string") {
    return {
      state: typeof legacyState === "string" ? legacyState : null,
      kind: typeof legacyKind === "string" ? legacyKind : null,
    };
  }
  return null;
}

function lifecycleValue(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim().toLowerCase();
  }
  return null;
}

/**
 * System-managed Docs rows are visible but not editable through generic UI
 * affordances.  The server policy is authoritative; this mirror merely
 * prevents opening rename/move/archive controls before the request is sent.
 * Keep the stable system-key fallback for rows written by an older deploy
 * before managed metadata was added.
 */
function docsManagedDomain(
  node: Pick<DocsNode, "system_key"> & Partial<Pick<DocsNode, "display_props">>,
) {
  const key = typeof node.system_key === "string" ? node.system_key.trim() : "";
  if (key === "aoitalk_guide" || key.startsWith("aoitalk_guide:")) return "aoitalk_guide";
  const props = node.display_props;
  if (props && typeof props === "object" && !Array.isArray(props) && props.system_managed === true) {
    const domain = typeof props.managed_domain === "string" ? props.managed_domain.trim() : "";
    return domain || "system_managed";
  }
  return null;
}

/** True for a Project-information identity, including an unresolved DTO. */
export function isDocsProjectCanonicalNode(node: Pick<DocsNode, "system_key"> & Partial<Pick<DocsNode, "lifecycle" | "lifecycle_state" | "lifecycle_kind">>) {
  const lifecycle = docsNodeLifecycle(node);
  const kind = lifecycleValue(lifecycle?.kind, lifecycle?.type, lifecycle?.category, lifecycle?.ownership);
  const systemKey = typeof node.system_key === "string" ? node.system_key.trim() : "";
  return lifecycle?.canonical === true
    || (kind !== null && CANONICAL_LIFECYCLE_KINDS.has(kind))
    || systemKey === "project_information_root"
    || systemKey.startsWith("project_information:");
}

/** True when the lifecycle authority explicitly marks a node as stale/orphan. */
export function isDocsStaleProjectNode(node: Pick<DocsNode, "system_key"> & Partial<Pick<DocsNode, "lifecycle" | "lifecycle_state" | "lifecycle_kind">>) {
  const lifecycle = docsNodeLifecycle(node);
  const kind = lifecycleValue(lifecycle?.kind, lifecycle?.type, lifecycle?.category, lifecycle?.ownership, lifecycle?.state, lifecycle?.status, lifecycle?.phase);
  const state = lifecycleValue(lifecycle?.state, lifecycle?.status, lifecycle?.phase, lifecycle?.pointer_state, lifecycle?.pointer?.state);
  // Lightweight serializers intentionally mark every system-keyed row as
  // ``canonical:false/state:unresolved`` until a Project pointer is joined.
  // That fail-closed projection is protected, not proof that the row is stale;
  // only an explicit stale/orphan kind or lifecycle state should enable the
  // dedicated cleanup affordance.
  return (kind !== null && STALE_LIFECYCLE_KINDS.has(kind))
    || (state !== null && STALE_LIFECYCLE_KINDS.has(state));
}

/** Best-effort reason shown when a protected action is attempted. */
export function docsNodeProtectionMessage(node: Pick<DocsNode, "system_key"> & Partial<Pick<DocsNode, "display_props" | "lifecycle" | "lifecycle_state" | "lifecycle_kind">>) {
  const lifecycle = docsNodeLifecycle(node);
  const detail = lifecycle?.reason ?? lifecycle?.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  const managedDomain = docsManagedDomain(node);
  if (managedDomain === "aoitalk_guide") {
    return "AoiTalk ガイドはシステム管理されているため、通常のDocs操作では変更できません";
  }
  if (managedDomain) {
    return `${managedDomain} はシステム管理されているため、通常のDocs操作では変更できません`;
  }
  if (isDocsProjectCanonicalNode(node) && !isDocsStaleProjectNode(node)) {
    return "Projectが管理するcanonical情報rootは通常のDocs操作で変更できません";
  }
  return "このDocsノードは現在のライフサイクルにより変更できません";
}

/**
 * Decide whether a UI mutation is safe.  Explicit false capabilities always
 * deny.  For canonical/protected/unknown lifecycle states, destructive and
 * identity-changing operations are denied even if a stale client omitted a
 * capability field.  Ordinary content edits remain available where the
 * server permits them.
 */
export function canMutateDocsNode(
  node: Pick<DocsNode, "system_key" | "permission"> & Partial<Pick<DocsNode, "display_props" | "lifecycle" | "lifecycle_state" | "lifecycle_kind">>,
  action: DocsNodeMutation,
) {
  if (node.permission === "read") return false;
  if (docsManagedDomain(node)) return false;
  const identityKey = typeof node.system_key === "string" ? node.system_key.trim() : "";
  const identityChangingActions: DocsNodeMutation[] = [
    "archive",
    "delete",
    "rename",
    "move",
    "taskify",
    "tag",
    "title",
  ];
  // A system identity is authoritative even when an older API response has
  // an incomplete or contradictory lifecycle DTO.  Never fail open into a
  // generic destructive/identity action for canonical or hub rows.
  if (
    (identityKey === "project_information_root" || identityKey.startsWith("project_information:"))
    && identityChangingActions.includes(action)
  ) {
    return false;
  }
  const lifecycle = docsNodeLifecycle(node);
  const key = `can_${action}` as keyof DocsNodeLifecycle;
  if (lifecycle && (lifecycle[key] === false || lifecycle.actions?.[action] === false)) return false;
  const state = lifecycleValue(lifecycle?.state, lifecycle?.status, lifecycle?.phase, lifecycle?.pointer_state, lifecycle?.pointer?.state);
  const kind = lifecycleValue(lifecycle?.kind, lifecycle?.type, lifecycle?.category, lifecycle?.ownership);
  const stale = isDocsStaleProjectNode(node);
  const protectedIdentity = isDocsProjectCanonicalNode(node)
    && !stale
    && (lifecycle?.canonical !== false || lifecycle === null || state === null || PROTECTED_LIFECYCLE_STATES.has(state) || lifecycle?.retained === true || lifecycle?.active === true || lifecycle?.resolved === false || lifecycle?.pointer_valid === false || lifecycle?.pointer?.valid === false);
  if (protectedIdentity && identityChangingActions.includes(action)) return false;
  if (lifecycle?.canonical === true && identityChangingActions.includes(action)) return false;
  const protectedState = state !== null
    && PROTECTED_LIFECYCLE_STATES.has(state)
    && (isDocsProjectCanonicalNode(node) || kind !== null && CANONICAL_LIFECYCLE_KINDS.has(kind) || lifecycle?.canonical === true || lifecycle?.pointer_valid === false || lifecycle?.resolved === false);
  if (protectedState && identityChangingActions.includes(action)) return false;
  return true;
}

/** Return a server-provided canonical title, if one is available. */
export function docsCanonicalNodeTitle(node: Pick<DocsNode, "title" | "system_key"> & Partial<Pick<DocsNode, "lifecycle" | "lifecycle_state" | "lifecycle_kind" | "canonical_title">>) {
  const lifecycle = docsNodeLifecycle(node);
  const candidate = lifecycle?.canonical_title
    ?? lifecycle?.expected_title
    ?? lifecycle?.expectedTitle
    ?? lifecycle?.canonicalTitle
    ?? lifecycle?.project_title
    ?? lifecycle?.project_name
    ?? node.canonical_title;
  return typeof candidate === "string" && candidate.trim() ? candidate : null;
}

export type DocsNode = {
  id: string;
  docs_library_id: string;
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
  /** Effective ACL for the current actor (owner/read/write). */
  permission?: "owner" | "read" | "write";
  /** Server-owned lifecycle/identity projection (additive across versions). */
  lifecycle?: DocsNodeLifecycle | null;
  /** SQL-visible discriminator for an encrypted explicit blank paragraph. */
  is_explicit_blank?: boolean;
  lifecycle_dto?: DocsNodeLifecycle | null;
  project_lifecycle?: DocsNodeLifecycle | null;
  project_information_lifecycle?: DocsNodeLifecycle | null;
  lifecycle_state?: string | null;
  lifecycle_kind?: string | null;
  /** Canonical title supplied when a Project-information root is normalized. */
  canonical_title?: string | null;
};

export type DocsLibrary = {
  id: string;
  docs_library_id: string;
  name?: string | null;
  description?: string | null;
  /** Library discriminator; project identity belongs to node.project_id. */
  library_type?: "personal" | string;
  project_id?: string | null;
  owner_user_id?: string | null;
  settings?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
};

export type DocsSupertag = {
  id: string;
  docs_library_id: string;
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
  docs_library_id: string;
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
  owner_user_id?: string | null;
  owner_id?: string | null;
  /** Denormalized canonical Project-information pointer, when available. */
  knowledge_node_id?: string | null;
  knowledge_node_id_raw?: string | null;
  knowledge_node_id_valid?: boolean;
  knowledge_node_id_validated?: boolean;
  is_completed?: boolean;
  deleted_at?: string | null;
  space_id: string | null;
  color: string | null;
};

export type DocsSavedView = {
  id: string;
  docs_library_id: string;
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
  docs_library_id: string;
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
  library?: DocsLibrary;
  /**
   * Legacy/bootstrap responses also expose the library id at the top level.
   * Keep it in the client shape so a focused foreign-library tree can use the
   * id as its authoritative scope before a full library DTO is available.
   */
  docs_library_id?: string;
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
  /** Descendant IDs archived by the most recent server mutation. */
  archived_node_ids?: string[];
  /** Optional lifecycle projections returned out-of-band by a bootstrap API. */
  node_lifecycle?: Record<string, DocsNodeLifecycle>;
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
  archived_node_ids: [],
  node_lifecycle: {},
};

export const EMPTY_REFERENCES: ReferencesState = {
  backlinks: [],
  referenced_in: [],
  field_refs: [],
  outgoing: [],
};
