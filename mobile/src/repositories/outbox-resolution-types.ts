import type { PendingOutboxSnapshot } from './outbox';

/** JSON values retained by the private journal and safe view projections. */
export type ResolutionJsonPrimitive = string | number | boolean | null;
export type ResolutionJsonValue =
  | ResolutionJsonPrimitive
  | ResolutionJsonValue[]
  | { [key: string]: ResolutionJsonValue };

export type ResolutionJsonObject = {
  [key: string]: ResolutionJsonValue;
};

/**
 * A finite result used by the resolution facade.  Raw SQLite/network errors
 * stay private to the journal implementation and are never turned into a
 * user-visible string.
 */
export type ResolutionResultReason =
  | 'origin_confirmation_required'
  | 'invalid_input'
  | 'journal_unavailable'
  | 'record_not_found'
  | 'revision_mismatch'
  | 'context_changed'
  | 'auth_scope_changed'
  | 'server_changed'
  | 'server_unavailable'
  | 'server_not_found'
  | 'server_forbidden'
  | 'server_version_missing'
  | 'local_snapshot_changed'
  | 'local_snapshot_unavailable'
  | 'related_snapshot_changed'
  | 'membership_changed'
  | 'cache_snapshot_changed'
  | 'editor_changed'
  | 'sync_inflight'
  | 'duplicate_entity'
  | 'acl_changed'
  | 'scope_changed'
  | 'unsupported'
  | 'not_prepareable'
  | 'not_adoptable'
  | 'outbox_replaced'
  | 'draft_missing'
  | 'transaction_failed'
  | 'export_unavailable';

export type ResolutionResult<T> =
  | { ok: true; value: T }
  | { ok: false; reason: ResolutionResultReason };

/**
 * The only identity context that may cross the storage boundary.  It is
 * intentionally token-free: a token/cookie/password must never reach the
 * resolution journal or an export.
 */
export type ResolutionContext = {
  authScope: string;
  serverFingerprint: string;
  credentialIdentityRevision: string;
  /** Optional stable owner label supplied by the authenticated facade. */
  ownerId?: string | null;
};

/** Safe, serializable description of the explicit resolution target. The
 * opaque capability itself is process-local and is never journaled. */
export type ResolutionTargetMetadata = {
  operationOrigin: 'unrecorded';
  authScope: string;
  serverFingerprint: string;
  tableName: string;
  entityId: string;
  opId: string;
  intentKey: string;
};

export type ResolutionTargetCapability = {
  metadata: ResolutionTargetMetadata;
  /** WeakMap-backed facade ticket; storage only forwards it to validators. */
  opaque: object;
};

export type ResolutionKind = 'task' | 'docs' | 'unsupported';
export type ResolutionServerState =
  | 'present'
  | 'not_found'
  | 'forbidden'
  | 'unavailable'
  | 'invalid'
  | 'unsupported';
export type ResolutionComparison = 'same' | 'different' | 'unknown';
export type ResolutionLifecycleState = 'prepared' | 'adopted' | 'draft' | 'held';

/** A single finite field exposed to diagnostics, UI, and export. */
export type ResolutionField = {
  key: string;
  label?: string;
  device: ResolutionJsonValue | null;
  server: ResolutionJsonValue | null;
  serverKnown: boolean;
  comparison: ResolutionComparison;
};

export type ResolutionPreviewView = {
  opId: string;
  entityId: string;
  tableName: string;
  action: string;
  docsScopeKey: string | null;
  kind: ResolutionKind;
  operationOrigin?: 'unrecorded';
  resolutionTargetLabel?: string;
  checkedAt: string;
  serverState: ResolutionServerState;
  /** Provenance of the displayed server value. `live_get` is required for
   * adoption; cache/unknown values may still be prepared as held history. */
  serverSource?: 'live_get' | 'cache' | 'unknown' | string;
  liveReadAt?: string | null;
  serverCapturedAt?: string | null;
  serverComplete?: boolean;
  serverUpdatedAt: string | null;
  baseUpdatedAt: string | null;
  hasBaseSnapshot: boolean;
  hasConflictSnapshot: boolean;
  fields: ResolutionField[];
  canPrepare: boolean;
  canAdopt: boolean;
  reason: string | null;
  localComplete: boolean;
};

/** Full operation identity, including the createdAt field absent from the
 * historical outbox helper type. */
export type ResolutionOperationSnapshot = PendingOutboxSnapshot & {
  createdAt: number;
};

/** Metadata-only editor guard identities for one captured resolution scene. */
export type ResolutionEditorTarget = {
  tableName: string;
  entityId: string;
};

/** Metadata-only Docs ancestry identity.  Content/body fields are excluded. */
export type ResolutionEditorAncestor = {
  id: string;
  workspaceId: string | null;
  projectId: string | null;
  parentId: string | null;
  rootPageId: string | null;
};

/** Internal local row and related-row capture used for CAS checks. */
export type ResolutionLocalSnapshot = {
  row: ResolutionJsonValue | null;
  /** `false` distinguishes an unreadable/unknown local row from a proven
   * absence. Omitted keeps compatibility with older captures. */
  known?: boolean;
  revision?: string | null;
  related?: ResolutionJsonValue;
  relatedRevision?: string | null;
  /** Known cache absence is distinct from a failed/unknown cache read. */
  relatedKnown?: boolean;
  /** Known table/entity identities used only for editor ownership guards. */
  editorTargets?: ResolutionEditorTarget[];
  /** Same-scope Docs parent chain metadata; never user content. */
  editorAncestors?: ResolutionEditorAncestor[];
  /** False means ancestry could not be proven complete and adoption is held. */
  editorAncestryComplete?: boolean;
};

/** Server value captured by the facade after an explicit current GET. */
export type ResolutionServerSnapshot = {
  state: ResolutionServerState;
  updatedAt: string | null;
  value: ResolutionJsonValue | null;
  /** Canonical fields used by the safe view; raw value remains private. */
  fields?: ResolutionField[];
  serverSource?: 'live_get' | 'cache' | 'unknown' | string;
  liveReadAt?: string | null;
  serverCapturedAt?: string | null;
  serverComplete?: boolean;
  /** Task assignee/tag aggregates are complete enough for cache adoption. */
  taskAggregatesComplete?: boolean;
};

/** Durable membership/cache/guard observations required for a failed-closed
 * preparation/adoption.  These contain no credentials. */
export type ResolutionGuardSnapshot = {
  editorRevision: number;
  sceneRevision?: string | null;
  inflightSync: boolean;
  uniqueEntity: boolean;
  acl: 'writable' | 'read_only' | 'unknown';
  scopeProven: boolean;
  unsupportedRelatedValues?: boolean;
};

/** Portable user-content projection. Arbitrary raw cache values are excluded
 * by construction; unknown/omitted related values are marked incomplete. */
export type ResolutionPortableSnapshot = {
  target: ResolutionJsonValue | null;
  children?: ResolutionJsonValue;
  fields?: ResolutionField[];
  incomplete?: string[];
};

/** Private full capture persisted by the store. */
export type ResolutionInternalFullSnapshot = {
  context: ResolutionContext;
  operation: ResolutionOperationSnapshot;
  kind: ResolutionKind;
  docsScopeKey: string | null;
  local: ResolutionLocalSnapshot;
  related: ResolutionJsonValue;
  membership: ResolutionJsonValue;
  cache: ResolutionJsonValue;
  guard: ResolutionGuardSnapshot;
  server: ResolutionServerSnapshot;
  portable: ResolutionPortableSnapshot;
  /** Optional revision-2 capability boundary. Never persisted. */
  target?: ResolutionTargetMetadata;
  capability?: ResolutionTargetCapability;
};

export type ResolutionPreview = {
  capture: ResolutionInternalFullSnapshot;
  view: ResolutionPreviewView;
};

export type ResolutionDraft = {
  restoredAt: string;
  fields: ResolutionField[];
  changes: ResolutionJsonObject;
};

export type ResolutionHistoryItem = {
  id: string;
  revision: number;
  kind: ResolutionKind;
  entityId: string;
  opId: string;
  createdAt: number;
  state: ResolutionLifecycleState;
  hasDraft: boolean;
};

export type ResolutionRecordView = {
  item: ResolutionHistoryItem;
  view: ResolutionPreviewView;
  draft: ResolutionDraft | null;
};

export type ResolutionExport = {
  json: string;
  context: ResolutionContext;
  fileName: string;
};

export type ResolutionAdoptInput = {
  recordId: string;
  authScope: string;
  expectedRevision: number;
  /** Fresh facade capture from the explicit current GET. */
  preview: ResolutionPreview;
};

export type ResolutionDraftInput = {
  recordId: string;
  authScope: string;
  /** Fresh namespace captured by the facade before draft restore/save. */
  context?: ResolutionContext;
  expectedRevision: number;
  changes: ResolutionJsonObject;
  fields?: ResolutionField[];
  restoredAt?: string;
  /** Optional post-adoption expectation; absence is valid for a prepared row. */
  expectedOutboxAbsent?: boolean;
  preview?: ResolutionPreview;
};

export type ResolutionDraftSceneSnapshot = {
  context: ResolutionContext;
  operation: ResolutionOperationSnapshot | null;
  local: ResolutionLocalSnapshot;
  related: ResolutionJsonValue;
  membership: ResolutionJsonValue;
  cache: ResolutionJsonValue;
  guard: ResolutionGuardSnapshot;
};

export type ResolutionDraftSceneRequest = {
  recordId: string;
  authScope: string;
  context: ResolutionContext;
  operation: ResolutionOperationSnapshot;
};

export type ResolutionStoreDependencies = {
  now?: () => number;
  id?: () => string;
  ensureSchema?: () => void;
  sqlite?: ResolutionSqlite;
  /** Optional fresh auth/server/generation provider. It is awaited only while
   * the foreground write slot is held, immediately before the sync SQLite
   * transaction; no provider call is made inside the transaction itself. */
  currentContext?: () => ResolutionContext | Promise<ResolutionContext>;
  /** Optional facade-owned opaque capability checks. Async checks run while
   * the foreground write slot is held; sync checks run inside the SQLite CAS. */
  revalidateTarget?: (
    capability: ResolutionTargetCapability,
    stage: 'prepare' | 'adopt',
  ) => boolean | Promise<boolean>;
  assertTargetInTransaction?: (
    capability: ResolutionTargetCapability,
    stage: 'prepare' | 'adopt',
  ) => boolean;
  /** Production facade sets this once the opaque target registry is wired. */
  targetCapabilityRequired?: boolean;
  /** Fresh current scene capture used by restore/save. It performs no live
   * GET and must only read current local/outbox/editor state. */
  captureDraftScene?: (
    request: ResolutionDraftSceneRequest,
  ) => Promise<ResolutionDraftSceneSnapshot>;
  /** Optional synchronous final fence immediately before draft journal update. */
  assertDraftSceneInTransaction?: (
    scene: ResolutionDraftSceneSnapshot,
  ) => boolean;
};

/** Minimal synchronous SQLite surface used by DatabaseSync and Expo SQLite
 * adapters in focused tests. */
export type ResolutionSqlite = {
  execSync?: (sql: string) => unknown;
  runSync?: (sql: string, params?: readonly unknown[]) => { changes?: number } | unknown;
  getFirstSync?: <T = Record<string, unknown>>(
    sql: string,
    params?: readonly unknown[],
  ) => T | null;
  getAllSync?: <T = Record<string, unknown>>(
    sql: string,
    params?: readonly unknown[],
  ) => T[];
  withTransactionSync?: <T>(callback: () => T) => T;
};

export type OutboxResolutionStore = {
  prepare(
    preview: ResolutionPreview,
  ): Promise<ResolutionResult<ResolutionRecordView>>;
  adopt(
    input: ResolutionAdoptInput,
  ): Promise<ResolutionResult<ResolutionRecordView>>;
  restoreDraft(
    input: ResolutionDraftInput,
  ): Promise<ResolutionResult<ResolutionRecordView>>;
  saveDraft(
    input: ResolutionDraftInput,
  ): Promise<ResolutionResult<ResolutionRecordView>>;
  read(
    recordId: string,
    authScope: string,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionRecordView>>;
  list(
    authScope: string,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionHistoryItem[]>>;
  export(
    recordId: string,
    authScope: string,
    expectedRevision: number,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionExport>>;
};

/** Narrow helper used by the facade before/after every awaited live request. */
export function sameResolutionContext(
  left: ResolutionContext,
  right: ResolutionContext,
): boolean {
  return left.authScope === right.authScope
    && left.serverFingerprint === right.serverFingerprint
    && left.credentialIdentityRevision === right.credentialIdentityRevision;
}
