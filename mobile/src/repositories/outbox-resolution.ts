import { and, eq, inArray, isNull, or } from 'drizzle-orm';

import { getDb, schema } from '../db/client';
import {
  getCredentialIdentityRevision,
  getConfiguredApiServerFingerprint,
  normalizeApiServerFingerprint,
} from '../lib/api-client';
import { getTokenAuthScope, getTokenSnapshot } from '../lib/auth';
import {
  fetchApiReadForAuthScopeAtServer,
  isApiHttpError,
} from '../lib/api-client';
import {
  getOutboxResolutionEditorRevision,
  hasOutboxResolutionEditors,
  hasOutboxResolutionEditorsForTargets,
} from '../lib/outbox-resolution-editor-guard';
import * as editorGuard from '../lib/outbox-resolution-editor-guard';
import { normalizeTaskStatus } from '../lib/task-status';
import { useSyncStatusStore } from '../stores/sync-status';
import {
  getOutboxResolutionReadSnapshot,
  type OutboxResolutionReadSnapshot,
} from './outbox';
import { createOutboxResolutionStore } from './outbox-resolution-store';
import type {
  OutboxResolutionStore,
  ResolutionContext,
  ResolutionDraftSceneRequest,
  ResolutionDraftSceneSnapshot,
  ResolutionEditorAncestor,
  ResolutionEditorTarget,
  ResolutionField,
  ResolutionGuardSnapshot,
  ResolutionHistoryItem,
  ResolutionInternalFullSnapshot,
  ResolutionJsonObject,
  ResolutionJsonValue,
  ResolutionLocalSnapshot,
  ResolutionOperationSnapshot,
  ResolutionPortableSnapshot,
  ResolutionPreview,
  ResolutionPreviewView,
  ResolutionRecordView,
  ResolutionResult,
  ResolutionResultReason,
  ResolutionServerSnapshot,
  ResolutionStoreDependencies,
  ResolutionTargetCapability,
  ResolutionTargetMetadata,
} from './outbox-resolution-types';
import { sameResolutionContext } from './outbox-resolution-types';

export type OutboxResolutionLiveFetch = <T>(
  authScope: string,
  serverFingerprint: string,
  path: string,
  timeout?: number,
) => Promise<T>;

export type OutboxResolutionLocalCapture = {
  local: ResolutionLocalSnapshot;
  related: ResolutionJsonValue;
  membership: ResolutionJsonValue;
  cache: ResolutionJsonValue;
  portableChildren?: ResolutionJsonValue;
  portableTarget?: ResolutionJsonValue;
  incomplete: string[];
  portableIncomplete?: string[];
  /** A missing local row can be a known tombstone/404 state, distinct from
   * an unreadable or incomplete local snapshot. */
  localMissingKnown?: boolean;
  scopeProven: boolean;
  acl: ResolutionGuardSnapshot['acl'];
  sceneRevision: string | null;
  uniqueEntity: boolean;
  editorOwned: boolean;
};

export type OutboxResolutionFacadeDependencies = {
  store?: OutboxResolutionStore;
  storeFactory?: (deps: ResolutionStoreDependencies) => OutboxResolutionStore;
  readOperation?: typeof getOutboxResolutionReadSnapshot;
  fetch?: OutboxResolutionLiveFetch;
  now?: () => string;
  readLocal?: (
    operation: ResolutionOperationSnapshot,
    context: ResolutionContext,
  ) => Promise<OutboxResolutionLocalCapture>;
  editorRevision?: () => number;
  hasEditors?: typeof hasOutboxResolutionEditors;
  hasEditorsForTargets?: typeof hasOutboxResolutionEditorsForTargets;
  syncState?: () => { status?: string; authScope?: string | null };
};

export type OutboxResolutionOpaqueTicket = object;
export type OutboxResolutionOpaqueConfirmation = object;

type ResolutionCapabilitySnapshot = {
  context: ResolutionContext;
  operation: ResolutionOperationSnapshot;
  local: ResolutionLocalSnapshot;
  related: ResolutionJsonValue;
  membership: ResolutionJsonValue;
  cache: ResolutionJsonValue;
  guard: ResolutionGuardSnapshot;
};

const pendingTargetTickets = new WeakMap<object, ResolutionCapabilitySnapshot>();
const activeTargetCapabilities = new WeakMap<object, ResolutionCapabilitySnapshot>();
const previewCapabilities = new WeakMap<object, object>();

const PRIVATE_KEY = /^(?:access[_-]?token|refresh[_-]?token|token|cookie|password|passwd|authorization|credential|secret|api[_-]?key)$/i;
const DOMAIN_CONTAINER_KEY = new Set([
  'task_metadata', 'taskMetadata', 'metadata', 'body_json', 'bodyJson',
  'body_text', 'bodyText', 'value_json', 'valueJson', 'display_props',
  'displayProps', 'query_json', 'queryJson', 'view_json', 'viewJson',
  'aliases', 'fields', 'fieldValues', 'children', 'content', 'payload',
]);
const DOCS_TABLE = 'knowledge_nodes';
const SAFE_DOCS_NODE_TYPES = new Set(['node', 'search', 'day', 'system']);

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function editorTargetsForScene(
  operation: ResolutionOperationSnapshot,
  local: ResolutionLocalSnapshot,
): ResolutionEditorTarget[] {
  const targets: ResolutionEditorTarget[] = [{
    tableName: operation.tableName,
    entityId: operation.entityId,
  }];
  if (operation.tableName !== DOCS_TABLE) return targets;
  const knownTargets = Array.isArray(local.editorTargets)
    ? local.editorTargets.filter((item): item is ResolutionEditorTarget => Boolean(
      item
      && typeof item.tableName === 'string'
      && item.tableName.length > 0
      && typeof item.entityId === 'string'
      && item.entityId.length > 0,
    ))
    : [];
  if (knownTargets.length > 0) {
    const seen = new Set<string>();
    return [...targets, ...knownTargets].filter((item) => {
      const key = `${item.tableName}:${item.entityId}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  const row = asRecord(local.row);
  const ids = [row?.parentId, row?.parent_id, row?.rootPageId, row?.root_page_id]
    .filter((id): id is string => typeof id === 'string' && id.length > 0);
  for (const entityId of [...new Set(ids)]) targets.push({
    tableName: DOCS_TABLE,
    entityId,
  });
  const seen = new Set<string>();
  return targets.filter((item) => {
    const key = `${item.tableName}:${item.entityId}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function privateJsonValue(
  value: unknown,
  seen = new Set<unknown>(),
  domainContainer = false,
): ResolutionJsonValue | null {
  if (value == null) return null;
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'object' || seen.has(value)) return null;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => privateJsonValue(item, seen, domainContainer));
  const result: ResolutionJsonObject = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (PRIVATE_KEY.test(key) && !domainContainer) continue;
    result[key] = privateJsonValue(item, seen, domainContainer || DOMAIN_CONTAINER_KEY.has(key));
  }
  return result;
}

function safeJsonValue(value: unknown, seen = new Set<unknown>()): ResolutionJsonValue | null {
  if (value == null) return null;
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'object' || seen.has(value)) return null;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => safeJsonValue(item, seen));
  const result: ResolutionJsonObject = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (PRIVATE_KEY.test(key)) continue;
    result[key] = safeJsonValue(item, seen);
  }
  return result;
}

/** Private capture clone. It retains user-domain opaque values nested in
 * content/metadata containers while dropping credential-like top-level data. */
function jsonValue(value: unknown): ResolutionJsonValue | null {
  return privateJsonValue(value);
}

function parseObject(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    try { return asRecord(JSON.parse(value)); } catch { return null; }
  }
  return asRecord(value);
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value as Record<string, unknown>).sort()
      .map((key) => `${JSON.stringify(key)}:${stable((value as Record<string, unknown>)[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function comparable(key: string, value: unknown): string {
  if (key === 'status' && typeof value === 'string') return normalizeTaskStatus(value);
  if ((key === 'assignee_ids' || key === 'tag_ids') && Array.isArray(value)) {
    return stable([...value].map(String).sort());
  }
  return stable(value);
}

function safeFieldValue(value: unknown): ResolutionJsonValue | null {
  return safeJsonValue(value);
}

function normalizeOperation(
  operation: OutboxResolutionReadSnapshot,
): ResolutionOperationSnapshot {
  return operation;
}

function serverUpdatedAt(value: Record<string, unknown>): string | null {
  return text(value.updated_at ?? value.updatedAt);
}

function serverId(value: Record<string, unknown>): string | null {
  return text(value.id);
}

function parsePayload(operation: ResolutionOperationSnapshot): Record<string, unknown> | null {
  return parseObject(operation.payload);
}

function hasUnexpectedCredentialKeys(
  value: unknown,
  domainContainer = false,
  seen = new Set<unknown>(),
): boolean {
  if (!value || typeof value !== 'object' || seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some((item) =>
    hasUnexpectedCredentialKeys(item, domainContainer, seen));
  return Object.entries(value as Record<string, unknown>).some(([key, item]) => {
    if (PRIVATE_KEY.test(key) && !domainContainer) return true;
    return hasUnexpectedCredentialKeys(
      item,
      domainContainer || DOMAIN_CONTAINER_KEY.has(key),
      seen,
    );
  });
}

function taskServerField(
  server: Record<string, unknown> | null,
  key: string,
): { known: boolean; value: unknown } {
  if (!server) return { known: false, value: null };
  const wireKey = key === 'task_metadata' && !Object.prototype.hasOwnProperty.call(server, key)
    ? 'metadata'
    : key;
  if (Object.prototype.hasOwnProperty.call(server, wireKey)) {
    if (key === 'assignee_ids' || key === 'tag_ids') {
      const direct = server[wireKey];
      return Array.isArray(direct) && direct.every(nonEmptyTaskId)
        ? { known: true, value: direct }
        : { known: false, value: null };
    }
    return { known: true, value: server[wireKey] };
  }
  if (key === 'assignee_ids' || key === 'tag_ids') {
    const aggregate = server[key === 'assignee_ids' ? 'assignees' : 'tags'];
    const completeKey = key === 'assignee_ids' ? 'assignees_complete' : 'tags_complete';
    const idKeys = key === 'assignee_ids' ? ['user_id'] : ['id'];
    if (Array.isArray(aggregate)
      && server[completeKey] !== false
      && aggregate.every((item) => idKeys.some((idKey) => nonEmptyTaskId(asRecord(item)?.[idKey])))) {
      return {
        known: true,
        value: aggregate.map((item) => {
          const record = asRecord(item)!;
          const idKey = idKeys.find((candidate) => typeof record[candidate] === 'string')!;
          return record[idKey];
        }),
      };
    }
  }
  return { known: false, value: null };
}

function nonEmptyTaskId(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim());
}

type TaskAggregateObservation = {
  present: boolean;
  complete: boolean;
  valid: boolean;
  ids: string[];
};

function taskAggregateObservation(
  server: Record<string, unknown>,
  key: 'assignees' | 'tags',
): TaskAggregateObservation {
  const present = Object.prototype.hasOwnProperty.call(server, key);
  if (!present) return { present: false, complete: false, valid: false, ids: [] };
  const completeKey = key === 'assignees' ? 'assignees_complete' : 'tags_complete';
  if (server[completeKey] === false || !Array.isArray(server[key])) {
    return { present: true, complete: false, valid: false, ids: [] };
  }
  const ids: string[] = [];
  for (const item of server[key]) {
    const record = asRecord(item);
    const id = key === 'assignees' ? record?.user_id : record?.id;
    if (!nonEmptyTaskId(id)) return { present: true, complete: true, valid: false, ids: [] };
    if (key === 'tags' && typeof record?.name !== 'string') {
      return { present: true, complete: true, valid: false, ids: [] };
    }
    ids.push(id);
  }
  return { present: true, complete: true, valid: true, ids };
}

function taskAggregatesComplete(
  payload: Record<string, unknown> | null,
  server: Record<string, unknown>,
): boolean {
  if (!payload) return true;
  const relationKeys: Array<['assignee_ids' | 'tag_ids', 'assignees' | 'tags']> = [
    ['assignee_ids', 'assignees'],
    ['tag_ids', 'tags'],
  ];
  for (const [directKey, aggregateKey] of relationKeys) {
    const payloadHasRelation = Object.prototype.hasOwnProperty.call(payload, directKey);
    const serverHasDirect = Object.prototype.hasOwnProperty.call(server, directKey);
    const serverHasAggregate = Object.prototype.hasOwnProperty.call(server, aggregateKey);
    // If neither the operation nor the response carries this relation, the
    // aggregate is genuinely unknown; scalar adoption may preserve cache.
    if (!payloadHasRelation && !serverHasDirect && !serverHasAggregate) continue;
    if (payloadHasRelation) {
      const device = payload[directKey];
      if (!Array.isArray(device) || !device.every(nonEmptyTaskId)) return false;
    }
    const aggregate = taskAggregateObservation(server, aggregateKey);
    if (!aggregate.present || !aggregate.complete || !aggregate.valid) return false;
    if (!serverHasDirect) continue;
    const direct = server[directKey];
    if (!Array.isArray(direct) || !direct.every(nonEmptyTaskId)) return false;
    if (stable([...direct].sort()) !== stable([...aggregate.ids].sort())) return false;
  }
  return true;
}

function fieldsForPayload(
  payload: Record<string, unknown> | null,
  server: Record<string, unknown> | null,
  kind: 'task' | 'docs',
): ResolutionField[] {
  if (!payload) return [];
  return Object.entries(payload)
    .filter(([key]) => !PRIVATE_KEY.test(key))
    .map(([key, device]) => {
      const current = kind === 'task'
        ? taskServerField(server, key)
        : server && Object.prototype.hasOwnProperty.call(server, key)
          ? { known: true, value: server[key] }
          : { known: false, value: null };
      const deviceValue = safeFieldValue(device);
      const serverValue = safeFieldValue(current.value);
      return {
        key,
        device: deviceValue,
        server: serverValue,
        serverKnown: current.known,
        comparison: !current.known
          ? 'unknown'
          : comparable(key, device) === comparable(key, current.value)
            ? 'same'
            : 'different',
      } satisfies ResolutionField;
    });
}

const TASK_SERVER_KEYS = new Set([
  'id', 'project_id', 'projectId', 'title', 'description', 'status', 'priority',
  'start_at', 'startAt', 'end_at', 'endAt', 'all_day', 'allDay',
  'reminder_offsets', 'reminderOffsets', 'notifications_enabled',
  'notificationsEnabled', 'auto_close_on_due', 'autoCloseOnDue', 'source',
  'created_by', 'createdBy', 'completed_at', 'completedAt', 'archived_at',
  'archivedAt', 'estimated_hours', 'estimatedHours', 'parent_task_id',
  'parentTaskId', 'task_metadata', 'taskMetadata', 'metadata', 'sort_order',
  'sortOrder', 'created_at', 'createdAt', 'updated_at', 'updatedAt',
  'deleted_at', 'deletedAt', 'assignees', 'tags', 'assignee_ids', 'tag_ids',
]);

const DOCS_SERVER_KEYS = new Set([
  'id', 'workspace_id', 'workspaceId', 'parent_id', 'parentId', 'root_page_id',
  'rootPageId', 'project_id', 'projectId', 'source', 'access', 'read_only',
  'readOnly', 'system_key', 'systemKey', 'title', 'aliases', 'description',
  'body_json', 'bodyJson', 'body_text', 'bodyText', 'node_type', 'nodeType',
  'display_props', 'displayProps', 'query_json', 'queryJson', 'view_json',
  'viewJson', 'day_date', 'dayDate', 'sort_order', 'sortOrder', 'created_by',
  'createdBy', 'updated_by', 'updatedBy', 'created_at', 'createdAt',
  'updated_at', 'updatedAt', 'archived_at', 'archivedAt',
]);

function canonicalServerValue(
  kind: 'task' | 'docs' | 'unsupported',
  value: Record<string, unknown>,
): ResolutionJsonValue | null {
  const allowed = kind === 'task' ? TASK_SERVER_KEYS : DOCS_SERVER_KEYS;
  const projected: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (allowed.has(key)) projected[key] = item;
  }
  return privateJsonValue(projected);
}

function validDocsNode(value: unknown, expectedId: string): value is Record<string, unknown> {
  const node = asRecord(value);
  if (!node || serverId(node) !== expectedId) return false;
  const nullableString = (key: string) => node[key] == null || typeof node[key] === 'string';
  const nullableRecord = (key: string) => node[key] == null || asRecord(node[key]) !== null;
  return typeof node.workspace_id === 'string'
    && nullableString('parent_id')
    && nullableString('root_page_id')
    && nullableString('project_id')
    && typeof node.title === 'string'
    && Array.isArray(node.aliases)
    && node.aliases.every((item) => typeof item === 'string')
    && nullableString('description')
    && nullableRecord('body_json')
    && nullableString('body_text')
    && typeof node.node_type === 'string'
    && SAFE_DOCS_NODE_TYPES.has(node.node_type)
    && nullableRecord('display_props')
    && nullableRecord('query_json')
    && nullableRecord('view_json')
    && nullableString('day_date')
    && (node.sort_order == null || (typeof node.sort_order === 'number' && Number.isFinite(node.sort_order)))
    && nullableString('created_by')
    && nullableString('updated_by')
    && nullableString('created_at')
    && nullableString('updated_at')
    && nullableString('archived_at');
}

function docsScope(value: Record<string, unknown>): string | null {
  const workspace = value.workspace_id ?? value.workspaceId;
  const project = value.project_id ?? value.projectId;
  if (typeof workspace !== 'string' || !workspace.trim()) return null;
  if (project !== null && project !== undefined && typeof project !== 'string') return null;
  return `${workspace}|project:${project == null ? '' : project}`;
}

function errorState(error: unknown): ResolutionServerSnapshot['state'] {
  if (isApiHttpError(error)) {
    if (error.status === 404) return 'not_found';
    if (error.status === 403) return 'forbidden';
  }
  return 'unavailable';
}

function reasonForServer(state: ResolutionServerSnapshot['state']): ResolutionResultReason | null {
  if (state === 'not_found') return 'server_not_found';
  if (state === 'forbidden') return 'server_forbidden';
  if (state === 'unavailable') return 'server_unavailable';
  if (state === 'invalid') return 'unsupported';
  if (state === 'unsupported') return 'unsupported';
  return null;
}

function sameJson(left: unknown, right: unknown): boolean {
  return stable(left) === stable(right);
}

function cloneSnapshot<T>(value: T): T {
  try {
    return JSON.parse(JSON.stringify(value)) as T;
  } catch {
    return value;
  }
}

function contextFor(
  auth: { token: string | null; revision: number },
  serverFingerprint: string,
  credentialIdentityRevision: string,
  requestedScope: string,
): ResolutionContext | null {
  if (!auth.token || getTokenAuthScope(auth.token) !== requestedScope) return null;
  const normalizedServer = normalizeApiServerFingerprint(serverFingerprint);
  if (!normalizedServer || !credentialIdentityRevision) return null;
  return {
    authScope: requestedScope,
    serverFingerprint: normalizedServer,
    credentialIdentityRevision,
  };
}

function safeServerLabel(serverFingerprint: string): string | null {
  try {
    const parsed = new URL(serverFingerprint);
    if (parsed.username || parsed.password || parsed.search || parsed.hash || !parsed.host) return null;
    const path = parsed.pathname.replace(/\/+$/, '');
    return `${parsed.host}${path && path !== '/' ? path : ''}`;
  } catch {
    return null;
  }
}

async function captureContext(authScope: string): Promise<ResolutionContext | null> {
  if (!authScope.trim() || authScope === 'anonymous') return null;
  const [auth, serverFingerprint, credentialIdentityRevision] = await Promise.all([
    getTokenSnapshot(),
    getConfiguredApiServerFingerprint(),
    getCredentialIdentityRevision(),
  ]);
  return contextFor(auth, serverFingerprint, credentialIdentityRevision, authScope);
}

async function captureContextSafe(authScope: string): Promise<ResolutionContext | null> {
  try {
    return await captureContext(authScope);
  } catch {
    return null;
  }
}

async function contextChanged(expected: ResolutionContext): Promise<boolean> {
  const current = await captureContextSafe(expected.authScope);
  return !current
    || current.authScope !== expected.authScope
    || current.serverFingerprint !== expected.serverFingerprint
    || current.credentialIdentityRevision !== expected.credentialIdentityRevision;
}

function syncStateDefault(): { status?: string; authScope?: string | null } {
  const state = useSyncStatusStore.getState();
  return { status: state.status, authScope: state.authScope };
}

type DocsScopeIdentity = { workspaceId: string; projectId: string | null };

function parseDocsScopeKey(value: string | null): DocsScopeIdentity | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  const separator = value.indexOf('|project:');
  if (separator <= 0) return null;
  const workspaceId = value.slice(0, separator).trim();
  const projectValue = value.slice(separator + '|project:'.length);
  if (!workspaceId) return null;
  return { workspaceId, projectId: projectValue || null };
}

function docsRowMatchesScope(
  row: Record<string, unknown> | null,
  scope: DocsScopeIdentity | null,
  expectedId?: string,
): boolean {
  if (!row || !scope) return false;
  if (expectedId && row.id !== expectedId) return false;
  const workspaceId = row.workspaceId ?? row.workspace_id;
  const projectId = row.projectId ?? row.project_id;
  return workspaceId === scope.workspaceId
    && (projectId ?? null) === scope.projectId;
}

function membershipMatchesScope(
  row: Record<string, unknown>,
  context: ResolutionContext,
  scope: DocsScopeIdentity | null,
): boolean {
  const state = row.state;
  const tableName = row.tableName ?? row.table_name;
  const authScope = row.authScope ?? row.auth_scope;
  const scopeKey = row.scopeKey ?? row.scope_key;
  const projectId = row.projectId ?? row.project_id;
  return authScope === context.authScope
    && tableName === DOCS_TABLE
    && typeof scopeKey === 'string'
    && scopeKey === `${scope?.workspaceId ?? ''}|project:${scope?.projectId ?? ''}`
    && (state === 'active' || state === 'readonly')
    && (projectId ?? null) === (scope?.projectId ?? null)
    && typeof (row.entityKey ?? row.entity_key) === 'string';
}

function nodeScopePredicate(scope: DocsScopeIdentity): ReturnType<typeof eq> | ReturnType<typeof and> {
  const projectPredicate = scope.projectId == null
    ? isNull(schema.knowledgeNodes.projectId)
    : eq(schema.knowledgeNodes.projectId, scope.projectId);
  return and(
    eq(schema.knowledgeNodes.workspaceId, scope.workspaceId),
    projectPredicate,
  );
}

function isDescendantOf(
  row: Record<string, unknown>,
  targetId: string,
  byId: Map<string, Record<string, unknown>>,
  targetRootId: string | null,
): boolean {
  if (row.id === targetId) return false;
  if (targetRootId && (row.rootPageId ?? row.root_page_id ?? null) !== targetRootId) return false;
  const seen = new Set<string>();
  let parentId = (row.parentId ?? row.parent_id ?? null) as string | null;
  while (parentId) {
    if (parentId === targetId) return true;
    if (seen.has(parentId)) return false;
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent) return false;
    parentId = (parent.parentId ?? parent.parent_id ?? null) as string | null;
  }
  return false;
}

function nullableText(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function editorAncestorMetadata(
  row: Record<string, unknown>,
): ResolutionEditorAncestor | null {
  const id = nullableText(row.id);
  if (!id) return null;
  return {
    id,
    workspaceId: nullableText(row.workspaceId ?? row.workspace_id),
    projectId: nullableText(row.projectId ?? row.project_id),
    parentId: nullableText(row.parentId ?? row.parent_id),
    rootPageId: nullableText(row.rootPageId ?? row.root_page_id),
  };
}

type DocsEditorAncestry = {
  editorTargets: ResolutionEditorTarget[];
  editorAncestors: ResolutionEditorAncestor[];
  complete: boolean;
};

/**
 * Resolve only the same-scope membership rows needed for editor ownership.
 * The returned metadata deliberately excludes all content/body columns.
 */
function docsEditorAncestry(
  target: Record<string, unknown> | null,
  scope: DocsScopeIdentity | null,
  byId: Map<string, Record<string, unknown>>,
  targetId: string,
): DocsEditorAncestry {
  const editorTargets: ResolutionEditorTarget[] = [{
    tableName: DOCS_TABLE,
    entityId: targetId,
  }];
  const editorAncestors: ResolutionEditorAncestor[] = [];
  const targetMetadata = target ? editorAncestorMetadata(target) : null;
  let complete = Boolean(
    target
    && scope
    && targetMetadata
    && targetMetadata.id === targetId
    && byId.has(targetId)
    && docsRowMatchesScope(target, scope, targetId),
  );
  if (!targetMetadata || !scope || !target) {
    return { editorTargets, editorAncestors, complete: false };
  }

  const rootPageId = targetMetadata.rootPageId;
  let parentId = targetMetadata.parentId;
  const seen = new Set<string>([targetId]);
  if (rootPageId === targetId && parentId) complete = false;
  if (!rootPageId && parentId) complete = false;

  while (parentId) {
    if (seen.has(parentId)) {
      complete = false;
      break;
    }
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent || !docsRowMatchesScope(parent, scope, parentId)) {
      complete = false;
      break;
    }
    const metadata = editorAncestorMetadata(parent);
    if (!metadata) {
      complete = false;
      break;
    }
    if (metadata.id === rootPageId) {
      // A canonical root may store NULL or its own id as root_page_id, but it
      // must not itself point at another parent.
      if (metadata.rootPageId !== null && metadata.rootPageId !== metadata.id) complete = false;
      if (metadata.parentId) complete = false;
    } else if (rootPageId && metadata.rootPageId !== rootPageId) {
      complete = false;
    }
    editorAncestors.push(metadata);
    editorTargets.push({ tableName: DOCS_TABLE, entityId: metadata.id });
    if (metadata.id === rootPageId && metadata.parentId) {
      // A root carrying a parent is noncanonical. Continue walking the full
      // chain for editor guards, but keep the scene held as incomplete.
      complete = false;
    }
    parentId = metadata.parentId;
  }

  if (rootPageId && !seen.has(rootPageId)) {
    const root = byId.get(rootPageId);
    const rootMetadata = root ? editorAncestorMetadata(root) : null;
    if (!root || !rootMetadata || !docsRowMatchesScope(root, scope, rootPageId)) {
      complete = false;
    } else {
      // The root is known but was not reached through parent links. Keep its
      // identity in the editor guard while holding the scene as incomplete.
      editorAncestors.push(rootMetadata);
      editorTargets.push({ tableName: DOCS_TABLE, entityId: rootMetadata.id });
      complete = false;
    }
  }

  return {
    editorTargets: [...new Map(
      editorTargets.map((item) => [`${item.tableName}:${item.entityId}`, item]),
    ).values()],
    editorAncestors: [...new Map(
      editorAncestors.map((item) => [item.id, item]),
    ).values()],
    complete,
  };
}

/**
 * SQLite variable limit is 999 on older engines and Android platforms.
 * Keep inArray chunk sizes safely below 999 to prevent `too many SQL variables`.
 */
const SQLITE_IN_ARRAY_CHUNK_SIZE = 200;
const SQLITE_OR_IN_ARRAY_CHUNK_SIZE = 100;

async function fetchKnowledgeNodesInChunks(
  db: ReturnType<typeof getDb>,
  ids: string[],
  scope: DocsScopeIdentity,
): Promise<(typeof schema.knowledgeNodes.$inferSelect)[]> {
  const results: (typeof schema.knowledgeNodes.$inferSelect)[] = [];
  for (let i = 0; i < ids.length; i += SQLITE_IN_ARRAY_CHUNK_SIZE) {
    const chunk = ids.slice(i, i + SQLITE_IN_ARRAY_CHUNK_SIZE);
    const rows = await db.select().from(schema.knowledgeNodes).where(and(
      inArray(schema.knowledgeNodes.id, chunk),
      nodeScopePredicate(scope),
    ));
    results.push(...rows);
  }
  return results;
}

async function fetchKnowledgeNodeSupertagsInChunks(
  db: ReturnType<typeof getDb>,
  nodeIds: string[],
): Promise<(typeof schema.knowledgeNodeSupertags.$inferSelect)[]> {
  const results: (typeof schema.knowledgeNodeSupertags.$inferSelect)[] = [];
  for (let i = 0; i < nodeIds.length; i += SQLITE_IN_ARRAY_CHUNK_SIZE) {
    const chunk = nodeIds.slice(i, i + SQLITE_IN_ARRAY_CHUNK_SIZE);
    const rows = await db.select().from(schema.knowledgeNodeSupertags).where(
      inArray(schema.knowledgeNodeSupertags.nodeId, chunk),
    );
    results.push(...rows);
  }
  return results;
}

async function fetchKnowledgeFieldValuesInChunks(
  db: ReturnType<typeof getDb>,
  nodeIds: string[],
): Promise<(typeof schema.knowledgeFieldValues.$inferSelect)[]> {
  const results: (typeof schema.knowledgeFieldValues.$inferSelect)[] = [];
  for (let i = 0; i < nodeIds.length; i += SQLITE_IN_ARRAY_CHUNK_SIZE) {
    const chunk = nodeIds.slice(i, i + SQLITE_IN_ARRAY_CHUNK_SIZE);
    const rows = await db.select().from(schema.knowledgeFieldValues).where(
      inArray(schema.knowledgeFieldValues.nodeId, chunk),
    );
    results.push(...rows);
  }
  return results;
}

async function fetchKnowledgePlacementsInChunks(
  db: ReturnType<typeof getDb>,
  nodeIds: string[],
): Promise<(typeof schema.knowledgeNodePlacements.$inferSelect)[]> {
  const byId = new Map<string, typeof schema.knowledgeNodePlacements.$inferSelect>();
  for (let i = 0; i < nodeIds.length; i += SQLITE_OR_IN_ARRAY_CHUNK_SIZE) {
    const chunk = nodeIds.slice(i, i + SQLITE_OR_IN_ARRAY_CHUNK_SIZE);
    const rows = await db.select().from(schema.knowledgeNodePlacements).where(or(
      inArray(schema.knowledgeNodePlacements.nodeId, chunk),
      inArray(schema.knowledgeNodePlacements.parentNodeId, chunk),
    ));
    for (const row of rows) {
      byId.set(row.id, row);
    }
  }
  return [...byId.values()];
}

async function fetchKnowledgeEdgesInChunks(
  db: ReturnType<typeof getDb>,
  nodeIds: string[],
): Promise<(typeof schema.knowledgeEdges.$inferSelect)[]> {
  const byId = new Map<string, typeof schema.knowledgeEdges.$inferSelect>();
  for (let i = 0; i < nodeIds.length; i += SQLITE_OR_IN_ARRAY_CHUNK_SIZE) {
    const chunk = nodeIds.slice(i, i + SQLITE_OR_IN_ARRAY_CHUNK_SIZE);
    const rows = await db.select().from(schema.knowledgeEdges).where(or(
      inArray(schema.knowledgeEdges.sourceNodeId, chunk),
      inArray(schema.knowledgeEdges.targetNodeId, chunk),
    ));
    for (const row of rows) {
      byId.set(row.id, row);
    }
  }
  return [...byId.values()];
}

async function captureLocalDefault(
  operation: ResolutionOperationSnapshot,
  context: ResolutionContext,
): Promise<OutboxResolutionLocalCapture> {
  const db = getDb();
  const candidateRows = await db.select({
    opId: schema.outbox.opId,
    docsScopeKey: schema.outbox.docsScopeKey,
  }).from(schema.outbox).where(and(
    eq(schema.outbox.authScope, context.authScope),
    eq(schema.outbox.tableName, operation.tableName),
    eq(schema.outbox.entityId, operation.entityId),
  ));
  const candidates = candidateRows.filter((row) => row.docsScopeKey === operation.docsScopeKey);
  const uniqueEntity = candidates.length === 1 && candidates[0].opId === operation.opId;
  const payload = parsePayload(operation);
  const credentialPayloadUnsafe = hasUnexpectedCredentialKeys(payload);

  if (operation.tableName === 'tasks') {
    const rows = await db.select().from(schema.tasks).where(eq(schema.tasks.id, operation.entityId));
    const row = rows[0] ?? null;
    const cacheRows = await db.select().from(schema.taskDetailCache)
      .where(eq(schema.taskDetailCache.cacheKey, `task:${operation.entityId}`));
    const cache = cacheRows[0]?.payloadJson ?? null;
    const parsedCache = parseObject(cache);
    const cacheAbsent = cacheRows.length === 0;
    const cacheCorrupt = !cacheAbsent && parsedCache === null;
    // A missing cache row is a known absence, not an unknown aggregate. The
    // explicit payload and fresh server GET still carry assignee/tag intent.
    // An existing malformed row remains incomplete and blocks adoption.
    const incomplete = [
      ...(cacheCorrupt ? ['task_detail_cache_unavailable'] : []),
      ...(credentialPayloadUnsafe ? ['credential_payload_unexpected'] : []),
    ];
    const related = jsonValue(parsedCache ?? {}) ?? {};
    const local: ResolutionLocalSnapshot = {
      row: jsonValue(row),
      revision: text(row?.updatedAt),
      related,
      relatedRevision: text(cacheRows[0]?.cachedAt),
      editorTargets: [{ tableName: operation.tableName, entityId: operation.entityId }],
      editorAncestryComplete: true,
    };
    const localMissingKnown = row == null;
    const portableTarget = row
      ? jsonValue(row)
      : jsonValue({ id: operation.entityId, ...(payload ?? {}) });
    return {
      local,
      related,
      membership: [],
      cache: related,
      // task_detail_cache is unscoped aggregate cache. Keep it private for
      // CAS only; export contains the target row and finite intent fields.
      portableChildren: undefined,
      portableTarget,
      incomplete,
      portableIncomplete: ['task_detail_cache_private'],
      scopeProven: true,
      acl: 'writable',
      sceneRevision: text(row?.updatedAt),
      uniqueEntity,
      editorOwned: false,
      localMissingKnown,
    };
  }

  if (operation.tableName === DOCS_TABLE) {
    const scope = parseDocsScopeKey(operation.docsScopeKey);
    const rows = scope
      ? await db.select().from(schema.knowledgeNodes).where(and(
          eq(schema.knowledgeNodes.id, operation.entityId),
          nodeScopePredicate(scope),
        ))
      : [];
    const row = rows[0] ?? null;
    const membershipRows = operation.docsScopeKey
      ? await db.select().from(schema.docsScopeMembership).where(and(
          eq(schema.docsScopeMembership.authScope, context.authScope),
          eq(schema.docsScopeMembership.scopeKey, operation.docsScopeKey),
          eq(schema.docsScopeMembership.tableName, DOCS_TABLE),
        ))
      : [];
    const scopedMembership = membershipRows.filter((item) =>
      scope && membershipMatchesScope(item as unknown as Record<string, unknown>, context, scope));
    const targetMembership = scopedMembership.filter((item) => item.entityKey === operation.entityId);
    const writable = targetMembership.some((item) =>
      (item.state === 'active' || item.state === 'readonly')
      && item.access !== 'read'
      && item.readOnly !== true,
    );
    const systemKey = typeof row?.systemKey === 'string' ? row.systemKey : '';
    const protectedDocs = row?.archivedAt != null
      || systemKey === 'project_information_root'
      || systemKey.startsWith('project_information:');
    const acl: ResolutionGuardSnapshot['acl'] = protectedDocs
      || row?.readOnly === true || row?.access === 'read'
      ? 'read_only'
      : writable ? 'writable' : 'unknown';
    const scopeEntityIds = [...new Set(scopedMembership.map((item) => item.entityKey))];
    const scopedRows = scope && scopeEntityIds.length
      ? await fetchKnowledgeNodesInChunks(db, scopeEntityIds, scope)
      : [];
    const byId = new Map(scopedRows.map((item) => [item.id, item as unknown as Record<string, unknown>]));
    const targetForTree = row as unknown as Record<string, unknown> | null;
    const editorAncestry = docsEditorAncestry(
      targetForTree,
      scope,
      byId,
      operation.entityId,
    );
    const descendantRows = targetForTree
      ? scopedRows.filter((item) => isDescendantOf(
          item as unknown as Record<string, unknown>,
          operation.entityId,
          byId,
          (targetForTree.rootPageId ?? null) as string | null,
        ))
      : [];
    const targetScopeValid = Boolean(targetForTree && scope && docsRowMatchesScope(targetForTree, scope, operation.entityId));
    const descendantIds = descendantRows.map((item) => item.id);
    // Include the selected target in the private subtree snapshot so the
    // foreground CAS can revalidate target + descendants together. Ancestors
    // and same-scope siblings remain outside this set.
    const subtreeRows = targetScopeValid ? [row!, ...descendantRows] : [];
    const contentIds = [operation.entityId, ...descendantIds];
    const [nodeSupertags, fieldValues, placements, edges] = await Promise.all([
      contentIds.length
        ? fetchKnowledgeNodeSupertagsInChunks(db, contentIds)
        : Promise.resolve([]),
      contentIds.length
        ? fetchKnowledgeFieldValuesInChunks(db, contentIds)
        : Promise.resolve([]),
      contentIds.length
        ? fetchKnowledgePlacementsInChunks(db, contentIds)
        : Promise.resolve([]),
      contentIds.length
        ? fetchKnowledgeEdgesInChunks(db, contentIds)
        : Promise.resolve([]),
    ]);
    const contentIdSet = new Set(contentIds);
    const safeFieldValues = fieldValues.filter((item) =>
      item.targetNodeId == null || contentIdSet.has(item.targetNodeId));
    const safePlacements = placements.filter((item) =>
      contentIdSet.has(item.nodeId) && contentIdSet.has(item.parentNodeId));
    const safeEdges = edges.filter((item) =>
      contentIdSet.has(item.sourceNodeId) && contentIdSet.has(item.targetNodeId));
    const related = jsonValue({
      nodes: subtreeRows,
      nodeSupertags,
      fieldValues: safeFieldValues,
      placements: safePlacements,
      edges: safeEdges,
    }) ?? {};
    const loadedMembershipIds = new Set(scopedRows.map((item) => item.id));
    const missingScopedRows = scopeEntityIds.some((id) => !loadedMembershipIds.has(id));
    const relationScopeIncomplete = safeFieldValues.length !== fieldValues.length
      || safePlacements.length !== placements.length
      || safeEdges.length !== edges.length;
    const scopeProven = Boolean(scope && targetScopeValid && targetMembership.length === 1);
    const relevantMembershipIds = new Set([operation.entityId, ...descendantIds]);
    const relevantMembership = scopedMembership.filter((item) => relevantMembershipIds.has(item.entityKey));
    const local: ResolutionLocalSnapshot = {
      row: targetScopeValid ? jsonValue(row) : null,
      revision: targetScopeValid ? text(row?.updatedAt) : null,
      related,
      relatedRevision: relevantMembership.map((item) => item.updatedAt).sort().at(-1) ?? null,
      editorTargets: editorAncestry.editorTargets,
      editorAncestors: editorAncestry.editorAncestors,
      editorAncestryComplete: editorAncestry.complete,
    };
    return {
      local,
      related,
      // Membership is target-only for the exact membership CAS. Descendant
      // scope is represented in the private related snapshot, never inferred
      // from a raw UUID or exported as sibling content.
      membership: jsonValue(targetMembership) ?? [],
      cache: jsonValue({ conflictPayload: targetScopeValid ? row?.conflictPayload : null }) ?? {},
      portableChildren: related,
      incomplete: [
        ...(!targetScopeValid || targetMembership.length !== 1 || missingScopedRows
          ? ['local_children_incomplete']
          : relationScopeIncomplete
            ? ['related_scope_incomplete']
            : credentialPayloadUnsafe
              ? ['credential_payload_unexpected']
              : []),
        ...(editorAncestry.complete ? [] : ['editor_ancestry_incomplete']),
      ],
      scopeProven,
      acl,
      sceneRevision: targetScopeValid ? text(row?.updatedAt) : null,
      uniqueEntity,
      editorOwned: false,
      localMissingKnown: false,
    };
  }

  return {
    local: { row: null },
    related: {},
    membership: [],
    cache: {},
    incomplete: ['unsupported_table'],
    scopeProven: false,
    acl: 'unknown',
    sceneRevision: null,
    uniqueEntity,
    editorOwned: false,
    localMissingKnown: false,
  };
}

function makeServerSnapshot(
  operation: ResolutionOperationSnapshot,
  kind: 'task' | 'docs' | 'unsupported',
  serverState: ResolutionServerSnapshot['state'],
  value: Record<string, unknown> | null,
  fields: ResolutionField[],
  checkedAt: string,
  structuralComplete: boolean,
  reason?: ResolutionResultReason | null,
  taskAggregatesComplete?: boolean,
): ResolutionServerSnapshot {
  return {
    state: serverState,
    updatedAt: value ? serverUpdatedAt(value) : null,
    value: value ? canonicalServerValue(kind, value) : null,
    fields,
    serverSource: kind === 'task' || kind === 'docs'
      ? serverState === 'present'
        ? 'live_current'
        : serverState === 'invalid' || serverState === 'unsupported'
          ? 'unsupported'
          : 'unavailable'
      : 'unsupported',
    liveReadAt: kind === 'task' || kind === 'docs' ? checkedAt : null,
    // The response's updated_at is a server version, not a capture timestamp.
    serverCapturedAt: null,
    serverComplete: structuralComplete,
    ...(taskAggregatesComplete === undefined ? {} : { taskAggregatesComplete }),
    ...(reason ? { _reason: reason } : {}),
  } as ResolutionServerSnapshot;
}

function structuralDocsMatchesLocal(
  local: ResolutionLocalSnapshot,
  server: Record<string, unknown>,
): boolean {
  const row = asRecord(local.row);
  if (!row) return false;
  const pairs: Array<[string, string]> = [
    ['parentId', 'parent_id'],
    ['rootPageId', 'root_page_id'],
    ['nodeType', 'node_type'],
    ['systemKey', 'system_key'],
  ];
  return pairs.every(([localKey, serverKey]) => {
    const localValue = row[localKey] ?? row[serverKey] ?? null;
    const serverValue = server[serverKey] ?? server[localKey] ?? null;
    return String(localValue ?? '') === String(serverValue ?? '');
  });
}

function viewReason(
  kind: 'task' | 'docs' | 'unsupported',
  local: OutboxResolutionLocalCapture,
  guard: ResolutionGuardSnapshot,
  server: ResolutionServerSnapshot,
  fields: ResolutionField[],
  structuralMatch: boolean,
): ResolutionResultReason | null {
  if (kind === 'unsupported') return 'unsupported';
  if (local.incomplete.includes('credential_payload_unexpected')) return 'unsupported';
  if (kind === 'docs' && local.local.editorAncestryComplete !== true) return 'not_adoptable';
  if (local.editorOwned) return 'editor_changed';
  if (!local.uniqueEntity) return 'duplicate_entity';
  if (guard.inflightSync) return 'sync_inflight';
  if (!local.scopeProven) return 'scope_changed';
  if (local.acl !== 'writable') return local.acl === 'read_only' ? 'acl_changed' : 'scope_changed';
  if (!local.local.row && !local.localMissingKnown) return 'local_snapshot_changed';
  if (server.state !== 'present') return reasonForServer(server.state);
  if (local.localMissingKnown) return 'local_snapshot_changed';
  if (!server.updatedAt) return 'server_version_missing';
  if (!server.serverComplete || !structuralMatch) return 'server_changed';
  if (kind === 'task' && server.taskAggregatesComplete === false) return 'not_adoptable';
  if (fields.some((field) => !field.serverKnown)) return 'not_adoptable';
  return null;
}

function buildPortable(
  local: OutboxResolutionLocalCapture,
  fields: ResolutionField[],
): ResolutionPortableSnapshot {
  return {
    target: local.portableTarget ?? local.local.row,
    ...(local.portableChildren == null ? {} : { children: local.portableChildren }),
    fields,
    incomplete: [...local.incomplete, ...(local.portableIncomplete ?? [])],
  };
}

function viewFor(
  operation: ResolutionOperationSnapshot,
  kind: 'task' | 'docs' | 'unsupported',
  checkedAt: string,
  local: OutboxResolutionLocalCapture,
  guard: ResolutionGuardSnapshot,
  server: ResolutionServerSnapshot,
  fields: ResolutionField[],
  structuralMatch: boolean,
  targetLabel: string,
): ResolutionPreviewView {
  const reason = viewReason(kind, local, guard, server, fields, structuralMatch);
  const canPrepare = kind !== 'unsupported'
    && (Boolean(local.local.row) || local.localMissingKnown === true)
    && local.uniqueEntity
    && local.scopeProven
    && !local.editorOwned
    && local.incomplete.length === 0
    && !(kind === 'docs' && local.local.editorAncestryComplete !== true)
    && !guard.inflightSync;
  const canAdopt = canPrepare
    && reason === null
    && local.localMissingKnown !== true
    && server.state === 'present'
    && server.serverSource === 'live_current'
    && server.serverComplete === true
    && server.taskAggregatesComplete !== false
    && fields.every((field) => field.serverKnown);
  return {
    opId: operation.opId,
    entityId: operation.entityId,
    tableName: operation.tableName,
    action: operation.action,
    docsScopeKey: operation.docsScopeKey,
    kind,
    operationOrigin: 'unrecorded',
    resolutionTargetLabel: targetLabel,
    checkedAt,
    serverState: server.state,
    serverSource: server.serverSource,
    liveReadAt: server.liveReadAt,
    serverCapturedAt: null,
    serverComplete: server.serverComplete,
    serverUpdatedAt: server.updatedAt,
    baseUpdatedAt: operation.baseUpdatedAt,
    hasBaseSnapshot: operation.basePayload != null,
    hasConflictSnapshot: operation.conflictPayload != null,
    fields,
    canPrepare,
    canAdopt,
    reason,
    localComplete: (Boolean(local.local.row) || local.localMissingKnown === true)
      && local.incomplete.length === 0,
  };
}

function fullCapture(
  context: ResolutionContext,
  operation: ResolutionOperationSnapshot,
  kind: 'task' | 'docs' | 'unsupported',
  local: OutboxResolutionLocalCapture,
  guard: ResolutionGuardSnapshot,
  server: ResolutionServerSnapshot,
  fields: ResolutionField[],
  capability: ResolutionTargetCapability,
): ResolutionInternalFullSnapshot {
  const target: ResolutionTargetMetadata = capability.metadata;
  return {
    context,
    operation,
    kind,
    docsScopeKey: operation.docsScopeKey,
    local: local.local,
    related: local.related,
    membership: local.membership,
    cache: local.cache,
    guard,
    server,
    portable: buildPortable(local, fields),
    target,
    capability,
  };
}

function unsupportedServer(): ResolutionServerSnapshot {
  return {
    state: 'unsupported',
    updatedAt: null,
    value: null,
    fields: [],
    serverSource: 'unsupported',
    liveReadAt: null,
    serverCapturedAt: null,
    serverComplete: false,
  };
}

async function liveServer(
  operation: ResolutionOperationSnapshot,
  context: ResolutionContext,
  kind: 'task' | 'docs',
  fetch: OutboxResolutionLiveFetch,
  checkedAt: string,
): Promise<{ server: ResolutionServerSnapshot; fields: ResolutionField[]; structuralMatch: boolean }> {
  try {
    const path = kind === 'task'
      ? `/api/tasks/${encodeURIComponent(operation.entityId)}`
      : `/api/docs/nodes/${encodeURIComponent(operation.entityId)}`;
    const response = await fetch<Record<string, unknown>>(
      context.authScope,
      context.serverFingerprint,
      path,
    );
    const raw = asRecord(response);
    const value = kind === 'docs' ? asRecord(raw?.node) : raw;
    if (!value || serverId(value) !== operation.entityId) {
      const server = makeServerSnapshot(operation, kind, 'invalid', null, [], checkedAt, false, 'unsupported');
      return { server, fields: [], structuralMatch: false };
    }
    if (kind === 'docs' && !validDocsNode(value, operation.entityId)) {
      const server = makeServerSnapshot(operation, kind, 'invalid', null, [], checkedAt, false, 'unsupported');
      return { server, fields: [], structuralMatch: false };
    }
    if (kind === 'docs' && docsScope(value) !== operation.docsScopeKey) {
      const server = makeServerSnapshot(operation, kind, 'invalid', null, [], checkedAt, false, 'scope_changed');
      return { server, fields: [], structuralMatch: false };
    }
    const payload = parsePayload(operation);
    const fields = fieldsForPayload(payload, value, kind);
    const aggregateReady = kind === 'task'
      ? taskAggregatesComplete(payload, value)
      : undefined;
    const server = makeServerSnapshot(
      operation,
      kind,
      'present',
      value,
      fields,
      checkedAt,
      kind === 'task' || validDocsNode(value, operation.entityId),
      null,
      aggregateReady,
    );
    return { server, fields, structuralMatch: true };
  } catch (error) {
    const state = errorState(error);
    const server = makeServerSnapshot(operation, kind, state, null, [], checkedAt, false, reasonForServer(state));
    return { server, fields: [], structuralMatch: false };
  }
}

function ensureResult<T>(value: unknown): ResolutionResult<T> {
  if (value && typeof value === 'object' && (value as { ok?: unknown }).ok === true) {
    return value as ResolutionResult<T>;
  }
  if (value && typeof value === 'object' && (value as { ok?: unknown }).ok === false
    && typeof (value as { reason?: unknown }).reason === 'string') {
    return value as ResolutionResult<T>;
  }
  return { ok: false, reason: 'journal_unavailable' };
}

function compareLiveToStored(
  stored: ResolutionPreviewView,
  current: ResolutionPreviewView,
): ResolutionResultReason | null {
  if (stored.serverState !== current.serverState) return 'server_changed';
  if (stored.serverUpdatedAt !== current.serverUpdatedAt) return 'server_changed';
  if (!sameJson(stored.fields, current.fields)) return 'server_changed';
  return null;
}

export function createOutboxResolutionFacade(
  dependencies: OutboxResolutionFacadeDependencies = {},
) {
  let store: OutboxResolutionStore;
  const readOperation = dependencies.readOperation ?? getOutboxResolutionReadSnapshot;
  const fetch = dependencies.fetch ?? fetchApiReadForAuthScopeAtServer;
  const now = dependencies.now ?? (() => new Date().toISOString());
  const editorRevision = dependencies.editorRevision ?? getOutboxResolutionEditorRevision;
  const legacyHasEditors = dependencies.hasEditors ?? hasOutboxResolutionEditors;
  const hasEditorsForTargets = dependencies.hasEditorsForTargets
    ?? (typeof hasOutboxResolutionEditorsForTargets === 'function'
      ? hasOutboxResolutionEditorsForTargets
      : (targets) => legacyHasEditors('', targets));
  const syncState = dependencies.syncState ?? syncStateDefault;
  const readLocal = dependencies.readLocal;

  // The storage contract pins history reads to the current server namespace.
  // Keep the call through a narrow adapter while older test doubles still
  // implement the two argument shape.
  const readStore = async (
    recordId: string,
    authScope: string,
    context: ResolutionContext,
  ) => store.read(recordId, authScope, context);
  const listStore = async (
    authScope: string,
    context: ResolutionContext,
  ) => store.list(authScope, context);
  const exportStore = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
    context: ResolutionContext,
  ) => store.export(recordId, authScope, expectedRevision, context);
  const storeResult = async <T>(operation: () => Promise<unknown>): Promise<ResolutionResult<T>> => {
    try {
      return ensureResult<T>(await operation());
    } catch {
      return { ok: false, reason: 'journal_unavailable' };
    }
  };
  const decorateRecord = (
    result: ResolutionResult<ResolutionRecordView>,
    context: ResolutionContext,
  ): ResolutionResult<ResolutionRecordView> => result.ok
    ? {
      ok: true,
      value: {
        ...result.value,
        view: {
          ...result.value.view,
          operationOrigin: 'unrecorded',
          resolutionTargetLabel: safeServerLabel(context.serverFingerprint) ?? 'unknown',
        },
      },
    }
    : result;
  const runtimeGuardReason = (
    preview: ResolutionPreview,
    authScope: string,
  ): ResolutionResultReason | null => {
    if (editorRevision() !== preview.capture.guard.editorRevision) return 'editor_changed';
    const currentSync = syncState();
    if (currentSync.status === 'syncing' && currentSync.authScope === authScope) {
      return 'sync_inflight';
    }
    if (hasEditorsForTargets(editorTargetsForScene(
      preview.capture.operation,
      preview.capture.local,
    ))) return 'editor_changed';
    return null;
  };

  const operationKind = (operation: ResolutionOperationSnapshot): 'task' | 'docs' | 'unsupported' =>
    operation.tableName === 'tasks'
      ? 'task'
      : operation.tableName === DOCS_TABLE
        ? 'docs'
        : 'unsupported';

  const captureTargetScene = async (
    opId: string,
    authScope: string,
  ): Promise<ResolutionResult<ResolutionCapabilitySnapshot>> => {
    let context: ResolutionContext | null;
    try {
      context = await captureContextSafe(authScope);
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    let operationRaw: OutboxResolutionReadSnapshot | null;
    try {
      operationRaw = await readOperation(opId, authScope);
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!operationRaw) return { ok: false, reason: 'record_not_found' };
    const operation = normalizeOperation(cloneSnapshot(operationRaw));
    if (operationKind(operation) === 'unsupported') {
      return { ok: false, reason: 'unsupported' };
    }
    const refreshGuardsForTargets = (editorGuard as unknown as {
      refreshOutboxResolutionEditorGuardsForTargets?: (
        targets: ReadonlyArray<{ tableName: string; entityId: string }>,
      ) => Promise<void>;
    }).refreshOutboxResolutionEditorGuardsForTargets;
    const refreshGuards = (editorGuard as unknown as {
      refreshOutboxResolutionEditorGuards?: (
        scope: string,
        targets: ReadonlyArray<{ tableName: string; entityId: string }>,
      ) => Promise<void>;
    }).refreshOutboxResolutionEditorGuards;
    let local: OutboxResolutionLocalCapture;
    try {
      local = cloneSnapshot(readLocal
        ? await readLocal(operation, context)
        : await captureLocalDefault(operation, context));
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (refreshGuardsForTargets || refreshGuards) {
      try {
        const targets = editorTargetsForScene(operation, local.local);
        if (refreshGuardsForTargets) await refreshGuardsForTargets(targets);
        else await refreshGuards!(authScope, targets);
      } catch {
        return { ok: false, reason: 'editor_changed' };
      }
      if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
      // Readonly guard refreshes may change the editor generation. Re-capture
      // the entire local scene after the same target set has settled.
      try {
        local = cloneSnapshot(readLocal
          ? await readLocal(operation, context)
          : await captureLocalDefault(operation, context));
      } catch {
        return { ok: false, reason: 'local_snapshot_unavailable' };
      }
      if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    }
    const editorRevision = editorRevisionDefault();
    const payload = parsePayload(operation);
    if (hasUnexpectedCredentialKeys(payload)
      && !local.incomplete.includes('credential_payload_unexpected')) {
      local.incomplete = [...local.incomplete, 'credential_payload_unexpected'];
    }
    if (operation.tableName === DOCS_TABLE) {
      const row = asRecord(local.local.row);
      const scope = parseDocsScopeKey(operation.docsScopeKey);
      const rowScopeValid = docsRowMatchesScope(row, scope, operation.entityId);
      const memberships = Array.isArray(local.membership)
        ? local.membership.map((item) => asRecord(item))
          .filter((item): item is Record<string, unknown> => item !== null)
        : [];
      const targetMembership = memberships.find((item) =>
        (item.entityKey ?? item.entity_key) === operation.entityId
        && (item.state === 'active' || item.state === 'readonly'));
      if (!rowScopeValid || !targetMembership) local.scopeProven = false;
      const systemKey = typeof row?.systemKey === 'string' ? row.systemKey : '';
      if (row?.archivedAt != null
        || systemKey === 'project_information_root'
        || systemKey.startsWith('project_information:')
        || row?.readOnly === true
        || row?.access === 'read'
        || targetMembership?.readOnly === true
        || targetMembership?.access === 'read') {
        local.acl = 'read_only';
      }
    }
    const sync = syncState();
    local.editorOwned = hasEditorsForTargets(editorTargetsForScene(operation, local.local));
    const guard: ResolutionGuardSnapshot = {
      editorRevision,
      sceneRevision: local.sceneRevision,
      inflightSync: sync.status === 'syncing' && sync.authScope === authScope,
      uniqueEntity: local.uniqueEntity,
      acl: local.acl,
      scopeProven: local.scopeProven,
      unsupportedRelatedValues: local.incomplete.length > 0,
    };
    return {
      ok: true,
      value: {
        context,
        operation,
        local: local.local,
        related: local.related,
        membership: local.membership,
        cache: local.cache,
        guard,
      },
    };
  };

  const editorRevisionDefault = () => editorRevision();

  const sameCapabilityScene = (
    expected: ResolutionCapabilitySnapshot,
    actual: ResolutionCapabilitySnapshot,
  ): boolean => sameResolutionContext(expected.context, actual.context)
    && sameJson(expected.operation, actual.operation)
    && sameJson(expected.local, actual.local)
    && sameJson(expected.related, actual.related)
    && sameJson(expected.membership, actual.membership)
    && sameJson(expected.cache, actual.cache)
    && sameJson(expected.guard, actual.guard);

  const activeCapability = (
    value: unknown,
  ): { token: object; snapshot: ResolutionCapabilitySnapshot } | null => {
    if (!value || typeof value !== 'object') return null;
    const snapshot = activeTargetCapabilities.get(value as object);
    return snapshot ? { token: value as object, snapshot } : null;
  };

  const validateActiveCapability = async (
    value: unknown,
    opId: string,
    authScope: string,
  ): Promise<ResolutionResult<{ token: object; snapshot: ResolutionCapabilitySnapshot }>> => {
    const capability = activeCapability(value);
    if (!capability) return { ok: false, reason: 'origin_confirmation_required' };
    if (capability.snapshot.context.authScope !== authScope
      || capability.snapshot.operation.opId !== opId) {
      return { ok: false, reason: 'origin_confirmation_required' };
    }
    const current = await captureTargetScene(opId, authScope);
    if (!current.ok) return current;
    if (!sameCapabilityScene(capability.snapshot, current.value)) {
      return { ok: false, reason: 'origin_confirmation_required' };
    }
    return { ok: true, value: capability };
  };

  const captureDraftScene = async (
    request: ResolutionDraftSceneRequest,
  ): Promise<ResolutionDraftSceneSnapshot> => {
    const context = await captureContextSafe(request.authScope);
    if (!context) throw new Error('context_changed');
    const currentRaw = await readOperation(request.operation.opId, request.authScope);
    if (await contextChanged(context)) throw new Error('context_changed');
    const operation = currentRaw
      ? normalizeOperation(cloneSnapshot(currentRaw))
      : request.operation;
    let local: OutboxResolutionLocalCapture;
    try {
      local = cloneSnapshot(readLocal
        ? await readLocal(operation, context)
        : await captureLocalDefault(operation, context));
    } catch {
      throw new Error('local_snapshot_unavailable');
    }
    if (await contextChanged(context)) throw new Error('context_changed');
    const editorRevision = editorRevisionDefault();
    const sync = syncState();
    local.editorOwned = hasEditorsForTargets(editorTargetsForScene(operation, local.local));
    return {
      context,
      operation: currentRaw ? operation : null,
      local: local.local,
      related: local.related,
      membership: local.membership,
      cache: local.cache,
      guard: {
        editorRevision,
        sceneRevision: local.sceneRevision,
        inflightSync: sync.status === 'syncing' && sync.authScope === request.authScope,
        uniqueEntity: local.uniqueEntity,
        acl: local.acl,
        scopeProven: local.scopeProven,
        unsupportedRelatedValues: local.incomplete.length > 0,
      },
    };
  };

  const productionStoreDeps: ResolutionStoreDependencies = {
    targetCapabilityRequired: true,
    revalidateTarget: async (capability: ResolutionTargetCapability) => {
      const seed = activeTargetCapabilities.get(capability.opaque);
      if (!seed
        || capability.metadata.authScope !== seed.context.authScope
        || capability.metadata.serverFingerprint !== seed.context.serverFingerprint
        || capability.metadata.tableName !== seed.operation.tableName
        || capability.metadata.entityId !== seed.operation.entityId
        || capability.metadata.opId !== seed.operation.opId) return false;
      const current = await captureContextSafe(seed.context.authScope);
      if (!current || !sameResolutionContext(current, seed.context)) return false;
      if (editorRevision() !== seed.guard.editorRevision) return false;
      const sync = syncState();
      if (sync.status === 'syncing' && sync.authScope === seed.context.authScope) return false;
      if (seed.operation.tableName === DOCS_TABLE
        && seed.local.editorAncestryComplete !== true) return false;
      return !hasEditorsForTargets(editorTargetsForScene(seed.operation, seed.local));
    },
    assertTargetInTransaction: (capability: ResolutionTargetCapability) => {
      const seed = activeTargetCapabilities.get(capability.opaque);
      if (!seed
        || capability.metadata.authScope !== seed.context.authScope
        || capability.metadata.serverFingerprint !== seed.context.serverFingerprint
        || capability.metadata.tableName !== seed.operation.tableName
        || capability.metadata.entityId !== seed.operation.entityId
        || capability.metadata.opId !== seed.operation.opId) return false;
      const sync = syncState();
      return editorRevision() === seed.guard.editorRevision
        && !(sync.status === 'syncing' && sync.authScope === seed.context.authScope)
        && !(seed.operation.tableName === DOCS_TABLE
          && seed.local.editorAncestryComplete !== true)
        && !hasEditorsForTargets(editorTargetsForScene(seed.operation, seed.local));
    },
    captureDraftScene,
    assertDraftSceneInTransaction: (scene: ResolutionDraftSceneSnapshot) =>
      editorRevision() === scene.guard.editorRevision
      && !scene.guard.inflightSync
      && !(scene.operation?.tableName === DOCS_TABLE
        && scene.local.editorAncestryComplete !== true)
      && (!scene.operation || !hasEditorsForTargets(editorTargetsForScene(
        scene.operation,
        scene.local,
      ))),
  };
  const storeFactory = dependencies.storeFactory ?? createOutboxResolutionStore;
  store = dependencies.store ?? storeFactory(productionStoreDeps);

  const requestTarget = async (
    opId: string,
    authScope: string,
  ): Promise<ResolutionResult<{
    confirmation: OutboxResolutionOpaqueTicket;
    targetLabel: string;
    operationOrigin: 'unrecorded';
    serverHost: string;
    serverPort: number;
  }>> => {
    try {
      if (!opId.trim() || !authScope.trim() || authScope === 'anonymous') {
        return { ok: false, reason: 'origin_confirmation_required' };
      }
      const captured = await captureTargetScene(opId, authScope);
      if (!captured.ok) return captured;
      const ticket = Object.freeze({});
      pendingTargetTickets.set(ticket, captured.value);
      const serverLabel = safeServerLabel(captured.value.context.serverFingerprint);
      if (!serverLabel) {
        pendingTargetTickets.delete(ticket);
        return { ok: false, reason: 'origin_confirmation_required' };
      }
      const label = `${captured.value.operation.tableName === DOCS_TABLE ? 'Docs' : 'Task'} @${serverLabel}`;
      const endpoint = new URL(captured.value.context.serverFingerprint);
      return {
        ok: true,
        value: {
          confirmation: ticket,
          targetLabel: label,
          operationOrigin: 'unrecorded',
          serverHost: endpoint.hostname,
          serverPort: Number(endpoint.port) || (endpoint.protocol === 'http:' ? 80 : 443),
        },
      };
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
  };

  const confirmTarget = async (
    pendingTicket: unknown,
  ): Promise<ResolutionResult<OutboxResolutionOpaqueConfirmation>> => {
    if (!pendingTicket || typeof pendingTicket !== 'object') {
      return { ok: false, reason: 'origin_confirmation_required' };
    }
    const expected = pendingTargetTickets.get(pendingTicket as object);
    if (!expected) return { ok: false, reason: 'origin_confirmation_required' };
    const current = await captureTargetScene(
      expected.operation.opId,
      expected.context.authScope,
    );
    if (!current.ok) return { ok: false, reason: 'origin_confirmation_required' };
    if (!sameCapabilityScene(expected, current.value)) {
      return { ok: false, reason: 'origin_confirmation_required' };
    }
    pendingTargetTickets.delete(pendingTicket as object);
    const active = Object.freeze({});
    activeTargetCapabilities.set(active, current.value);
    return { ok: true, value: active };
  };

  const inspect = async (
    opId: string,
    authScope: string,
    confirmedCapability?: OutboxResolutionOpaqueConfirmation,
  ): Promise<ResolutionResult<ResolutionPreview>> => {
    if (!opId.trim() || !authScope.trim() || authScope === 'anonymous') {
      return { ok: false, reason: 'invalid_input' };
    }
    const capability = await validateActiveCapability(confirmedCapability, opId, authScope);
    if (!capability.ok) return capability;
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const operationBeforeRaw = await readOperation(opId, authScope);
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!operationBeforeRaw) return { ok: false, reason: 'record_not_found' };
    const operationBefore = cloneSnapshot(operationBeforeRaw);
    const operation = normalizeOperation(operationBefore);
    const kind: 'task' | 'docs' | 'unsupported' = operation.tableName === 'tasks'
      ? 'task'
      : operation.tableName === DOCS_TABLE
        ? 'docs'
        : operation.tableName.startsWith('knowledge_') ? 'unsupported' : 'unsupported';
    const refreshGuardsForTargets = (editorGuard as unknown as {
      refreshOutboxResolutionEditorGuardsForTargets?: (
        targets: ReadonlyArray<{ tableName: string; entityId: string }>,
      ) => Promise<void>;
    }).refreshOutboxResolutionEditorGuardsForTargets;
    const refreshGuards = (editorGuard as unknown as {
      refreshOutboxResolutionEditorGuards?: (
        scope: string,
        targets: ReadonlyArray<{ tableName: string; entityId: string }>,
      ) => Promise<void>;
    }).refreshOutboxResolutionEditorGuards;
    let localBefore: OutboxResolutionLocalCapture;
    try {
      localBefore = cloneSnapshot(readLocal
        ? await readLocal(operation, context)
        : await captureLocalDefault(operation, context));
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if ((refreshGuardsForTargets || refreshGuards) && kind !== 'unsupported') {
      try {
        const metadataTargets = editorTargetsForScene(operation, localBefore.local);
        if (refreshGuardsForTargets) await refreshGuardsForTargets(metadataTargets);
        else await refreshGuards!(authScope, metadataTargets);
      } catch {
        return { ok: false, reason: 'editor_changed' };
      }
      if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
      // A readonly refresh may advance the editor revision. Discard the
      // preliminary scene and capture a fresh full snapshot after it settles.
      try {
        localBefore = cloneSnapshot(readLocal
          ? await readLocal(operation, context)
          : await captureLocalDefault(operation, context));
      } catch {
        return { ok: false, reason: 'local_snapshot_unavailable' };
      }
      if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    }
    const revisionBefore = editorRevision();
    const syncBefore = syncState();
    const editorOwned = hasEditorsForTargets(editorTargetsForScene(operation, localBefore.local));
    localBefore.editorOwned = editorOwned;
    const guardBefore: ResolutionGuardSnapshot = {
      editorRevision: revisionBefore,
      sceneRevision: localBefore.sceneRevision,
      inflightSync: syncBefore.status === 'syncing' && syncBefore.authScope === authScope,
      uniqueEntity: localBefore.uniqueEntity,
      acl: localBefore.acl,
      scopeProven: localBefore.scopeProven,
      unsupportedRelatedValues: localBefore.incomplete.length > 0,
    };
    const checkedAt = now();
    let serverResult: { server: ResolutionServerSnapshot; fields: ResolutionField[]; structuralMatch: boolean };
    if (kind === 'unsupported') {
      serverResult = { server: unsupportedServer(), fields: [], structuralMatch: false };
    } else {
      serverResult = await liveServer(operation, context, kind, fetch, checkedAt);
    }
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    const operationAfterRaw = await readOperation(opId, authScope);
    const revisionAfter = editorRevision();
    let localAfter: OutboxResolutionLocalCapture;
    try {
      localAfter = cloneSnapshot(readLocal
        ? await readLocal(operation, context)
        : await captureLocalDefault(operation, context));
    } catch {
      return { ok: false, reason: 'local_snapshot_unavailable' };
    }
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!operationAfterRaw) return { ok: false, reason: 'outbox_replaced' };
    const operationAfter = cloneSnapshot(operationAfterRaw);
    if (!sameJson(operationBefore, operationAfter)) return { ok: false, reason: 'local_snapshot_changed' };
    if (!sameJson(localBefore, localAfter)) return { ok: false, reason: 'local_snapshot_changed' };
    const currentSync = syncState();
    localAfter.editorOwned = hasEditorsForTargets(editorTargetsForScene(operation, localAfter.local));
    const guard: ResolutionGuardSnapshot = {
      ...guardBefore,
      editorRevision: revisionAfter,
      inflightSync: currentSync.status === 'syncing' && currentSync.authScope === authScope,
    };
    if (revisionBefore !== revisionAfter) return { ok: false, reason: 'editor_changed' };
    const structuralMatch = kind === 'docs'
      && serverResult.server.state === 'present'
      && asRecord(serverResult.server.value)
      ? structuralDocsMatchesLocal(localAfter.local, asRecord(serverResult.server.value)!)
      : kind === 'task';
    const view = viewFor(
      operation,
      kind,
      checkedAt,
      localAfter,
      guard,
      serverResult.server,
      serverResult.fields,
      structuralMatch,
      safeServerLabel(context.serverFingerprint) ?? 'unknown',
    );
    const capture = fullCapture(
      context,
      operation,
      kind,
      localAfter,
      guard,
      serverResult.server,
      serverResult.fields,
      {
        metadata: {
          operationOrigin: 'unrecorded',
          authScope: context.authScope,
          serverFingerprint: context.serverFingerprint,
          tableName: operation.tableName,
          entityId: operation.entityId,
          opId: operation.opId,
          intentKey: `${operation.tableName}:${operation.entityId}:${operation.opId}`,
        },
        opaque: capability.value.token,
      },
    );
    const preview = { capture, view };
    previewCapabilities.set(preview, capability.value.token);
    return { ok: true, value: preview };
  };

  const prepare = async (
    preview: ResolutionPreview,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    const context = preview?.capture?.context;
    if (!context) return { ok: false, reason: 'invalid_input' };
    const previewCapability = preview && previewCapabilities.get(preview);
    if (!previewCapability) return { ok: false, reason: 'origin_confirmation_required' };
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    const capabilityCheck = await validateActiveCapability(
      previewCapability,
      preview.capture.operation.opId,
      context.authScope,
    );
    if (!capabilityCheck.ok) return capabilityCheck;
    const originalGuardReason = runtimeGuardReason(preview, context.authScope);
    if (originalGuardReason) return { ok: false, reason: originalGuardReason };
    // Re-read the current server value before making the private journal
    // durable. This rejects a preview whose captured version/value was edited
    // or became stale while the confirmation surface was open.
    const fresh = await inspect(
      preview.capture.operation.opId,
      context.authScope,
      previewCapability,
    );
    if (!fresh.ok) return fresh;
    if (!sameJson(preview.capture.operation, fresh.value.capture.operation)) {
      return { ok: false, reason: 'outbox_replaced' };
    }
    if (!sameJson(preview.capture.local, fresh.value.capture.local)) {
      return { ok: false, reason: 'local_snapshot_changed' };
    }
    if (!sameJson(preview.capture.related, fresh.value.capture.related)) {
      return { ok: false, reason: 'related_snapshot_changed' };
    }
    if (!sameJson(preview.capture.membership, fresh.value.capture.membership)) {
      return { ok: false, reason: 'membership_changed' };
    }
    if (!sameJson(preview.capture.cache, fresh.value.capture.cache)) {
      return { ok: false, reason: 'cache_snapshot_changed' };
    }
    if (!sameJson(preview.capture.guard, fresh.value.capture.guard)) {
      return { ok: false, reason: 'editor_changed' };
    }
    const serverChanged = compareLiveToStored(preview.view, fresh.value.view);
    if (serverChanged) return { ok: false, reason: serverChanged };
    const guardReason = runtimeGuardReason(fresh.value, context.authScope);
    if (guardReason) return { ok: false, reason: guardReason };
    const result = await storeResult<ResolutionRecordView>(() => store.prepare(fresh.value));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return decorateRecord(result, context);
  };

  const adopt = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
    confirmedCapability?: OutboxResolutionOpaqueConfirmation,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const capabilitySeed = activeCapability(confirmedCapability);
    if (!capabilitySeed || capabilitySeed.snapshot.context.authScope !== authScope) {
      return { ok: false, reason: 'origin_confirmation_required' };
    }
    const storedResult = await storeResult<ResolutionRecordView>(() => readStore(recordId, authScope, context));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!storedResult.ok) return storedResult;
    if (storedResult.value.item.revision !== expectedRevision) return { ok: false, reason: 'revision_mismatch' };
    if (storedResult.value.item.state !== 'prepared') return { ok: false, reason: 'not_adoptable' };
    const capabilityCheck = await validateActiveCapability(
      confirmedCapability,
      storedResult.value.view.opId,
      authScope,
    );
    if (!capabilityCheck.ok) return capabilityCheck;
    const live = await inspect(
      storedResult.value.view.opId,
      authScope,
      confirmedCapability,
    );
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!live.ok) return live;
    const changed = compareLiveToStored(storedResult.value.view, live.value.view);
    if (changed) return { ok: false, reason: changed };
    if (!live.value.view.canAdopt) {
      return { ok: false, reason: (live.value.view.reason as ResolutionResultReason | null) ?? 'not_adoptable' };
    }
    const guardReason = runtimeGuardReason(live.value, authScope);
    if (guardReason) return { ok: false, reason: guardReason };
    const result = await storeResult<ResolutionRecordView>(() => store.adopt({
      recordId,
      authScope,
      expectedRevision,
      preview: live.value,
    }));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return decorateRecord(result, context);
  };

  const restore = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const storedResult = await storeResult<ResolutionRecordView>(() => readStore(recordId, authScope, context));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!storedResult.ok) return storedResult;
    const item = storedResult.value.item;
    if (item.revision !== expectedRevision) return { ok: false, reason: 'revision_mismatch' };
    const result = await storeResult<ResolutionRecordView>(() => store.restoreDraft({
      recordId,
      authScope,
      context,
      expectedRevision,
      changes: {},
      fields: storedResult.value.view.fields,
      // Restore is a private draft action. It must remain available after the
      // original operation is consumed or the server is offline.
      expectedOutboxAbsent: false,
    }));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return decorateRecord(result, context);
  };

  const saveDraft = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
    changes: Record<string, unknown>,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    if (!changes || typeof changes !== 'object' || Array.isArray(changes)) {
      return { ok: false, reason: 'invalid_input' };
    }
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const storedResult = await storeResult<ResolutionRecordView>(() => readStore(recordId, authScope, context));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!storedResult.ok) return storedResult;
    if (storedResult.value.item.revision !== expectedRevision) return { ok: false, reason: 'revision_mismatch' };
    const result = await storeResult<ResolutionRecordView>(() => store.saveDraft({
      recordId,
      authScope,
      context,
      expectedRevision,
      changes: jsonValue(changes) as ResolutionJsonObject,
      fields: storedResult.value.view.fields,
      expectedOutboxAbsent: false,
    }));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return decorateRecord(result, context);
  };

  const read = async (
    recordId: string,
    authScope: string,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const result = await storeResult<ResolutionRecordView>(() => readStore(recordId, authScope, context));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return decorateRecord(result, context);
  };

  const list = async (
    authScope: string,
  ): Promise<ResolutionResult<ResolutionHistoryItem[]>> => {
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const result = await storeResult<ResolutionHistoryItem[]>(() => listStore(authScope, context));
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    return result;
  };

  const exportRecord = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
  ): Promise<ResolutionResult<import('./outbox-resolution-types').ResolutionExport>> => {
    const context = await captureContextSafe(authScope);
    if (!context) return { ok: false, reason: 'auth_scope_changed' };
    const result = await storeResult<import('./outbox-resolution-types').ResolutionExport>(
      () => exportStore(recordId, authScope, expectedRevision, context),
    );
    if (await contextChanged(context)) return { ok: false, reason: 'context_changed' };
    if (!result.ok) return result;
    if (result.value.context.authScope !== context.authScope
      || normalizeApiServerFingerprint(result.value.context.serverFingerprint)
        !== context.serverFingerprint) {
      return { ok: false, reason: 'context_changed' };
    }
    return { ok: true, value: { ...result.value, context } };
  };

  const assertContext = async (context: ResolutionContext): Promise<void> => {
    if (!context || await contextChanged(context)) {
      throw new Error('context_changed');
    }
  };

  const getIdentity = async (): Promise<ResolutionResult<ResolutionContext>> => {
    try {
      const snapshot = await getTokenSnapshot();
      const authScope = snapshot.token ? getTokenAuthScope(snapshot.token) : 'anonymous';
      if (!snapshot.token || authScope === 'anonymous') {
        return { ok: false, reason: 'auth_scope_changed' };
      }
      const context = await captureContextSafe(authScope);
      return context
        ? { ok: true, value: context }
        : { ok: false, reason: 'auth_scope_changed' };
    } catch {
      return { ok: false, reason: 'auth_scope_changed' };
    }
  };

  return {
    getOutboxResolutionIdentity: getIdentity,
    requestOutboxResolutionTarget: requestTarget,
    confirmOutboxResolutionTarget: confirmTarget,
    inspectOutboxResolution: inspect,
    prepareOutboxResolution: prepare,
    adoptOutboxServer: adopt,
    restoreOutboxResolutionDraft: restore,
    saveOutboxResolutionDraft: saveDraft,
    readOutboxResolutionRecord: read,
    listOutboxResolutionHistory: list,
    getOutboxResolutionExport: exportRecord,
    assertOutboxResolutionContext: assertContext,
  };
}

const defaultFacade = createOutboxResolutionFacade();

export const getOutboxResolutionIdentity = defaultFacade.getOutboxResolutionIdentity;
export const requestOutboxResolutionTarget = defaultFacade.requestOutboxResolutionTarget;
export const confirmOutboxResolutionTarget = defaultFacade.confirmOutboxResolutionTarget;
export const inspectOutboxResolution = defaultFacade.inspectOutboxResolution;
export const prepareOutboxResolution = defaultFacade.prepareOutboxResolution;
export const adoptOutboxServer = defaultFacade.adoptOutboxServer;
export const restoreOutboxResolutionDraft = defaultFacade.restoreOutboxResolutionDraft;
export const saveOutboxResolutionDraft = defaultFacade.saveOutboxResolutionDraft;
export const readOutboxResolutionRecord = defaultFacade.readOutboxResolutionRecord;
export const listOutboxResolutionHistory = defaultFacade.listOutboxResolutionHistory;
export const getOutboxResolutionExport = defaultFacade.getOutboxResolutionExport;
export const assertOutboxResolutionContext = defaultFacade.assertOutboxResolutionContext;
