"use client";

import {
  DOCS_FIELD_TYPES,
  type DocsFieldType,
} from "@/lib/docs-model";
import { sortNodesByPosition } from "@/lib/docs-block-model";
import type {
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsProject,
  DocsState,
  DocsSupertag,
  FieldDraft,
  TagDraft,
} from "./types";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export function formatUpdatedAt(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function docsFieldType(field: DocsField | FieldDraft): DocsFieldType {
  if (field.field_type === "select" || field.field_type === "multi_select") return "options";
  if (field.field_type === "project_ref") return "reference";
  return DOCS_FIELD_TYPES.includes(field.field_type as DocsFieldType)
    ? (field.field_type as DocsFieldType)
    : "text";
}

export function splitDraftList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function fieldOptions(field: DocsField): string[] {
  const values = field.options_json?.values;
  return Array.isArray(values)
    ? values.filter((item): item is string => typeof item === "string")
    : [];
}

export function fieldValueToDraft(value: DocsFieldValue | undefined): string {
  if (!value) return "";
  if (value.value_text) return formatStructuredDraft(value.value_text);
  if (value.target_node_id) return value.target_node_id;
  if (value.value_number !== null && value.value_number !== undefined) return String(value.value_number);
  if (value.value_datetime) return value.value_datetime.slice(0, 10);
  if (value.value_json !== null && value.value_json !== undefined) {
    if (
      typeof value.value_json === "object" &&
      !Array.isArray(value.value_json) &&
      "value" in value.value_json &&
      typeof value.value_json.value === "boolean"
    ) {
      return value.value_json.value ? "true" : "false";
    }
    return typeof value.value_json === "string"
      ? formatStructuredDraft(value.value_json)
      : formatStructuredValue(value.value_json);
  }
  return "";
}

export function resolveReferenceLabel(
  value: string,
  nodes: DocsNode[],
  projects: DocsProject[],
) {
  const id = value.trim();
  if (!id) return "";
  const node = nodes.find((item) => item.id === id);
  if (node) return node.title || node.body_text.slice(0, 60);
  const project = projects.find((item) => item.id === id);
  return project?.name ?? "";
}

export function referenceOptions(
  nodes: DocsNode[],
  projects: DocsProject[],
  currentNodeId?: string,
) {
  return [
    ...projects.map((project) => ({
      id: `project:${project.id}`,
      value: project.id,
      label: project.name,
    })),
    ...nodes
      .filter((node) => node.id !== currentNodeId)
      .slice(0, 300)
      .map((node) => ({
        id: `node:${node.id}`,
        value: node.id,
        label: node.title || node.body_text.slice(0, 60),
      })),
  ].filter((option) => option.label.trim());
}

export function formatFieldSummaryValue(
  field: DocsField,
  value: DocsFieldValue | undefined,
  nodes: DocsNode[],
  projects: DocsProject[],
) {
  const draft = fieldValueToDraft(value).trim();
  if (!draft) return "";
  const fieldType = docsFieldType(field);
  const isProjectReference =
    field.field_type === "project_ref" ||
    field.name.toLowerCase() === "project" ||
    field.system_key === "project" ||
    field.system_key?.endsWith("_project") === true;
  // 参照系フィールドは UUID を生表示せず、参照先ノード/プロジェクト名に解決する。
  // field_type の分類漏れ（node_ref 等）に備え、target_node_id があれば常に解決する。
  if (fieldType === "reference" || isProjectReference || value?.target_node_id) {
    return resolveReferenceLabel(value?.target_node_id ?? draft, nodes, projects);
  }
  if (fieldType === "date" && /^\d{4}-\d{2}-\d{2}/.test(draft)) {
    const [, month = "", day = ""] = draft.match(/^\d{4}-(\d{2})-(\d{2})/) ?? [];
    return month && day ? `${Number(month)}/${Number(day)}` : draft;
  }
  return draft.length > 32 ? `${draft.slice(0, 31)}...` : draft;
}

export function fieldDraftToPayload(field: DocsField, rawDraft: string): unknown {
  const type = docsFieldType(field);
  const draft = rawDraft.trim();
  if (!draft) return null;
  if (type === "number") {
    const value = Number(draft);
    if (!Number.isFinite(value)) throw new Error(`${field.name} must be a number`);
    return value;
  }
  if (type === "checkbox") return rawDraft === "true";
  if (type === "long_text") return parseStructuredDraft(draft) ?? draft;
  if (type === "options_from_supertag") return splitDraftList(rawDraft);
  return rawDraft;
}

function parsePrimitiveLiteral(value: string): unknown {
  const trimmed = value.trim();
  if (trimmed === "true" || trimmed === "True") return true;
  if (trimmed === "false" || trimmed === "False") return false;
  if (trimmed === "null" || trimmed === "None") return null;
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  return trimmed.replace(/^['"]|['"]$/g, "");
}

export function parsePythonReprObject(value: string): Record<string, unknown> | null {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  const pairs = trimmed
    .slice(1, -1)
    .split(/,(?=(?:[^'"]|'[^']*'|"[^"]*")*$)/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (pairs.length === 0) return {};
  const result: Record<string, unknown> = {};
  for (const pair of pairs) {
    const match = pair.match(/^['"]?([^'":]+)['"]?\s*:\s*(.+)$/);
    if (!match) return null;
    result[match[1].trim()] = parsePrimitiveLiteral(match[2]);
  }
  return result;
}

export function parseStructuredDraft(value: string): Record<string, unknown> | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    /* fall through */
  }
  const pythonRepr = parsePythonReprObject(trimmed);
  if (pythonRepr) return pythonRepr;
  const lines = trimmed.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0 || !lines.every((line) => /^[^:]{1,80}:\s+/.test(line))) return null;
  const result: Record<string, unknown> = {};
  for (const line of lines) {
    const index = line.indexOf(":");
    result[line.slice(0, index).trim()] = parsePrimitiveLiteral(line.slice(index + 1));
  }
  return result;
}

export function formatStructuredValue(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return String(value ?? "");
  return Object.entries(value as Record<string, unknown>)
    .map(([key, entry]) => `${key}: ${typeof entry === "object" && entry !== null ? JSON.stringify(entry) : String(entry)}`)
    .join("\n");
}

export function formatStructuredDraft(value: string): string {
  const parsed = parseStructuredDraft(value);
  return parsed ? formatStructuredValue(parsed) : value;
}

export function parseJsonDraft(value: string, fallback: Record<string, unknown>) {
  if (!value.trim()) return fallback;
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON object is required.");
  }
  return parsed as Record<string, unknown>;
}

export function tagColorStyle(color: string | null) {
  return color ? { borderColor: color, color } : undefined;
}

export function emptyTagDraft(): TagDraft {
  return {
    id: null,
    name: "",
    base_type: "note",
    parent_supertag_id: "",
    color: "#2563eb",
    icon: "hash",
    description: "",
    title_template: "",
    template_json: "",
    ai_instructions: "",
  };
}

export function fieldToDraft(field: DocsField): FieldDraft {
  return {
    name: field.name,
    field_type: docsFieldType(field),
    required: field.required,
    options: fieldOptions(field).join(", "),
    default_value:
      field.default_value_json === null || field.default_value_json === undefined
        ? ""
        : typeof field.default_value_json === "string"
          ? field.default_value_json
          : JSON.stringify(field.default_value_json),
  };
}

export function buildChildrenByParent(nodes: DocsNode[]) {
  const map = new Map<string | null, DocsNode[]>();
  for (const node of nodes) {
    const key = node.parent_id;
    const next = map.get(key) ?? [];
    next.push(node);
    map.set(key, next);
  }
  for (const [key, value] of map.entries()) {
    map.set(key, sortNodesByPosition(value));
  }
  return map;
}

export function resolveFieldsForTag(
  tag: DocsSupertag,
  tagById: Map<string, DocsSupertag>,
  fieldsByTagId: Map<string, DocsField[]>,
) {
  const chain: DocsSupertag[] = [];
  const seen = new Set<string>();
  let cursor: DocsSupertag | undefined = tag;
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    chain.unshift(cursor);
    cursor = cursor.parent_supertag_id ? tagById.get(cursor.parent_supertag_id) : undefined;
  }
  const fields = chain.flatMap((item) => fieldsByTagId.get(item.id) ?? []);
  const byId = new Map<string, DocsField>();
  for (const field of fields) byId.set(field.id, field);
  return Array.from(byId.values()).sort((a, b) => a.sort_order - b.sort_order);
}

export function buildBreadcrumb(node: DocsNode | null, nodesById: Map<string, DocsNode>) {
  if (!node) return [];
  const chain: DocsNode[] = [];
  const seen = new Set<string>();
  let cursor: DocsNode | undefined = node.parent_id ? nodesById.get(node.parent_id) : undefined;
  while (cursor && !seen.has(cursor.id)) {
    chain.unshift(cursor);
    seen.add(cursor.id);
    cursor = cursor.parent_id ? nodesById.get(cursor.parent_id) : undefined;
  }
  return chain;
}

export function flattenDescendants(
  parentId: string,
  childrenByParent: Map<string | null, DocsNode[]>,
  collapsedIds: Set<string>,
  depth = 0,
): Array<{ node: DocsNode; depth: number }> {
  const rows: Array<{ node: DocsNode; depth: number }> = [];
  for (const child of childrenByParent.get(parentId) ?? []) {
    rows.push({ node: child, depth });
    if (!collapsedIds.has(child.id)) {
      rows.push(...flattenDescendants(child.id, childrenByParent, collapsedIds, depth + 1));
    }
  }
  return rows;
}

export function collectDescendantTagIds(tagId: string, tags: DocsSupertag[]) {
  const childrenByParent = new Map<string, string[]>();
  for (const tag of tags) {
    if (!tag.parent_supertag_id) continue;
    const next = childrenByParent.get(tag.parent_supertag_id) ?? [];
    next.push(tag.id);
    childrenByParent.set(tag.parent_supertag_id, next);
  }
  const ids = new Set([tagId]);
  const queue = [...(childrenByParent.get(tagId) ?? [])];
  while (queue.length > 0) {
    const id = queue.shift();
    if (!id || ids.has(id)) continue;
    ids.add(id);
    queue.push(...(childrenByParent.get(id) ?? []));
  }
  return ids;
}

export function mergeDocsState(current: DocsState, patch: Partial<DocsState>): DocsState {
  return {
    nodes: patch.nodes ?? current.nodes,
    supertags: patch.supertags ?? current.supertags,
    node_supertags: patch.node_supertags ?? current.node_supertags,
    fields: patch.fields ?? current.fields,
    supertag_fields: patch.supertag_fields ?? current.supertag_fields,
    placements: patch.placements ?? current.placements,
    field_values: patch.field_values ?? current.field_values,
    attachments: patch.attachments ?? current.attachments,
    views: patch.views ?? current.views,
    ai_suggestions: patch.ai_suggestions ?? current.ai_suggestions,
    projects: patch.projects ?? current.projects,
  };
}

export function projectsFromContext(
  docsProjects: DocsProject[],
  allProjects: Array<{ id: string; name: string; space_id?: string | null; color?: string | null }>,
) {
  return docsProjects.length > 0
    ? docsProjects
    : allProjects.map((project) => ({
        id: project.id,
        name: project.name,
        space_id: project.space_id ?? null,
        color: project.color ?? null,
      }));
}
