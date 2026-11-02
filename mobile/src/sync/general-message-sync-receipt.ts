import * as Crypto from "expo-crypto";
import { getSqlite } from "../db/client";
import { GENERAL_MESSAGE_RECEIPT_REVISION_KEY } from "../db/general-message-receipt-revision";

export const GENERAL_MESSAGE_SYNC_FINGERPRINT_KEY = "__aoitalk_general_sync_fingerprint";
export const GENERAL_MESSAGE_SYNC_SCOPE_KEY = "__aoitalk_general_sync_scope";
export const GENERAL_MESSAGE_SYNC_PROJECTION_KEY = "__aoitalk_general_sync_projection";

const FINGERPRINT_PATTERN = /^[a-f0-9]{32}$/;
export const GENERAL_MESSAGE_SYNC_READ_BATCH_SIZE = 512;

export type GeneralMessageSyncReceiptProgress = {
  scannedRows: number;
  scopedRows: number;
  batchCount: number;
  timings: GeneralMessageSyncReceiptTimings;
};

export type GeneralMessageSyncReceiptTimings = {
  totalMs: number;
  sqliteReadMs: number;
  projectionMs: number;
  materialJoinMs: number;
  digestMs: number;
  stateReadMs: number;
  maxBatchReadMs: number;
  maxBatchProjectionMs: number;
};

export type LocalGeneralMessageSyncRow = {
  id: string;
  sessionId: string;
  role: string;
  metadata: unknown;
  createdAt: string | null;
  updatedAt: string | null;
  deletedAt: string | null;
  tokenCount: number | null;
  parentMessageId: string | null;
  branchIndex: number | null;
  isActiveBranch: boolean | null;
};

export type GeneralMessageSyncReceiptRefusalReason =
  | "pending_outbox"
  | "local_only_message"
  | "missing_fingerprint"
  | "projection_mismatch"
  | "local_state_changed";

export type GeneralMessageSyncReceiptBaseline = {
  digest: string;
  localRevision: string;
  scopedRows: number;
};

export type GeneralMessageSyncReceiptResult = {
  digest: string | null;
  refusalReason: GeneralMessageSyncReceiptRefusalReason | null;
  scannedRows: number;
  scopedRows: number;
  batchCount: number;
  pendingOutboxCount: number;
  localRevision: string | null;
  reusedSavedDigest: boolean;
  timings: GeneralMessageSyncReceiptTimings;
};

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]),
  );
}

function publicMessageMetadata(value: unknown): Record<string, unknown> {
  const metadata = value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
  delete metadata[GENERAL_MESSAGE_SYNC_FINGERPRINT_KEY];
  delete metadata[GENERAL_MESSAGE_SYNC_SCOPE_KEY];
  delete metadata[GENERAL_MESSAGE_SYNC_PROJECTION_KEY];
  delete metadata.__aoitalk_branch_count;
  return metadata;
}

export function generalMessageSyncProjection(message: {
  session_id: string;
  role: string;
  metadata?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
  token_count?: number | null;
  parent_message_id?: string | null;
  branch_index?: number | null;
  is_active_branch?: boolean | null;
  sender_type?: string | null;
  sender_id?: string | null;
  sender_display_name?: string | null;
}): string {
  const metadata = publicMessageMetadata(message.metadata);
  for (const field of ["sender_type", "sender_id", "sender_display_name"] as const) {
    const value = message[field];
    if (typeof value === "string" && value.trim()) metadata[field] = value.trim();
    else if (value === null) metadata[field] = null;
    else delete metadata[field];
  }
  return JSON.stringify(stableValue([
    message.session_id,
    message.role,
    message.created_at ?? null,
    message.updated_at ?? null,
    message.token_count ?? null,
    message.parent_message_id ?? null,
    message.branch_index ?? 0,
    message.is_active_branch ?? true,
    message.sender_type ?? null,
    message.sender_id ?? null,
    message.sender_display_name ?? null,
    metadata,
  ]));
}

/** Return null unless the local server-backed rows are a safe digest baseline. */
export async function getLocalGeneralMessageSyncDigest(
  authScope: string,
): Promise<string | null> {
  return (await getLocalGeneralMessageSyncDigestResult(authScope)).digest;
}

function readLocalMessageReceiptState(authScope: string): {
  localRevision: string | null;
  pendingOutboxCount: number;
} {
  const row = getSqlite().getFirstSync<{
    local_revision: unknown;
    pending_outbox_count: unknown;
  }>(`
    SELECT
      (SELECT cursor FROM sync_state WHERE table_name = ?) AS local_revision,
      (SELECT COUNT(*) FROM outbox
       WHERE table_name = 'conversation_messages'
         AND (auth_scope = ? OR auth_scope IS NULL)) AS pending_outbox_count
  `, [GENERAL_MESSAGE_RECEIPT_REVISION_KEY, authScope]);
  return {
    localRevision: typeof row?.local_revision === "string"
      && /^\d+$/.test(row.local_revision)
      ? row.local_revision
      : null,
    pendingOutboxCount: Number(row?.pending_outbox_count ?? 0),
  };
}

/** Return a bounded, data-free explanation when a local receipt cannot be used. */
export async function getLocalGeneralMessageSyncDigestResult(
  authScope: string,
  reusableBaseline?: GeneralMessageSyncReceiptBaseline | null,
  onProgress?: (progress: GeneralMessageSyncReceiptProgress) => void,
): Promise<GeneralMessageSyncReceiptResult> {
  const receiptStartedAt = Date.now();
  let sqliteReadMs = 0;
  let projectionMs = 0;
  let materialJoinMs = 0;
  let digestMs = 0;
  let stateReadMs = 0;
  let maxBatchReadMs = 0;
  let maxBatchProjectionMs = 0;
  const timingSnapshot = (): GeneralMessageSyncReceiptTimings => ({
    totalMs: Math.max(0, Date.now() - receiptStartedAt),
    sqliteReadMs,
    projectionMs,
    materialJoinMs,
    digestMs,
    stateReadMs,
    maxBatchReadMs,
    maxBatchProjectionMs,
  });
  const initialStateReadStartedAt = Date.now();
  const initialState = readLocalMessageReceiptState(authScope);
  stateReadMs += Math.max(0, Date.now() - initialStateReadStartedAt);
  const initialOutboxCount = initialState.pendingOutboxCount;
  if (initialOutboxCount > 0) {
    return {
      digest: null,
      refusalReason: "pending_outbox",
      scannedRows: 0,
      scopedRows: 0,
      batchCount: 0,
      pendingOutboxCount: initialOutboxCount,
      localRevision: initialState.localRevision,
      reusedSavedDigest: false,
      timings: timingSnapshot(),
    };
  }
  if (
    reusableBaseline
    && initialState.localRevision !== null
    && initialState.localRevision === reusableBaseline.localRevision
  ) {
    return {
      digest: reusableBaseline.digest,
      refusalReason: null,
      scannedRows: 0,
      scopedRows: reusableBaseline.scopedRows,
      batchCount: 0,
      pendingOutboxCount: 0,
      localRevision: initialState.localRevision,
      reusedSavedDigest: true,
      timings: timingSnapshot(),
    };
  }

  const batchReadStartedAt = Date.now();
  const rows = getSqlite().getAllSync<{
    id: string;
    fp: unknown;
    local_only: unknown;
  }>(`
    SELECT id,
           json_extract(message_metadata, '$.__aoitalk_general_sync_fingerprint') AS fp,
           json_extract(message_metadata, '$.local_only') AS local_only
      FROM conversation_messages
     WHERE deleted_at IS NULL
       AND json_extract(message_metadata, '$.__aoitalk_general_sync_scope') = ?
     ORDER BY id
  `, [authScope]);
  const batchReadMs = Math.max(0, Date.now() - batchReadStartedAt);
  sqliteReadMs = batchReadMs;
  maxBatchReadMs = batchReadMs;
  const batchProjectionStartedAt = Date.now();
  const scannedRows = rows.length;
  const scopedRows = rows.length;
  const batchCount = 1;
  let refusalReason: GeneralMessageSyncReceiptRefusalReason | null = null;
  const material: string[] = [];
  for (const row of rows) {
    if (row.local_only === 1 || row.local_only === true) {
      refusalReason = "local_only_message";
      break;
    }
    if (typeof row.fp !== "string" || !FINGERPRINT_PATTERN.test(row.fp)) {
      refusalReason = "missing_fingerprint";
      break;
    }
    material.push(`${row.id}:${row.fp}`);
  }
  projectionMs = Math.max(0, Date.now() - batchProjectionStartedAt);
  maxBatchProjectionMs = projectionMs;
  onProgress?.({ scannedRows, scopedRows, batchCount, timings: timingSnapshot() });
  if (refusalReason) {
    return {
      digest: null,
      refusalReason,
      scannedRows,
      scopedRows,
      batchCount,
      pendingOutboxCount: 0,
      localRevision: initialState.localRevision,
      reusedSavedDigest: false,
      timings: timingSnapshot(),
    };
  }

  // A foreground write may race the keyset scan. Fail closed if an outbox
  // entry appeared while reading; the next flight can establish a fresh set.
  const finalStateReadStartedAt = Date.now();
  const finalState = readLocalMessageReceiptState(authScope);
  stateReadMs += Math.max(0, Date.now() - finalStateReadStartedAt);
  const finalOutboxCount = finalState.pendingOutboxCount;
  if (finalOutboxCount > 0) {
    return {
      digest: null,
      refusalReason: "pending_outbox",
      scannedRows,
      scopedRows,
      batchCount,
      pendingOutboxCount: finalOutboxCount,
      localRevision: finalState.localRevision,
      reusedSavedDigest: false,
      timings: timingSnapshot(),
    };
  }
  if (
    initialState.localRevision === null
    || finalState.localRevision === null
    || initialState.localRevision !== finalState.localRevision
  ) {
    return {
      digest: null,
      refusalReason: "local_state_changed",
      scannedRows,
      scopedRows,
      batchCount,
      pendingOutboxCount: 0,
      localRevision: finalState.localRevision,
      reusedSavedDigest: false,
      timings: timingSnapshot(),
    };
  }
  const materialJoinStartedAt = Date.now();
  const digestMaterial = material.join("\n");
  materialJoinMs = Math.max(0, Date.now() - materialJoinStartedAt);
  const digestStartedAt = Date.now();
  const digest = await Crypto.digestStringAsync(
    Crypto.CryptoDigestAlgorithm.MD5,
    digestMaterial,
  );
  digestMs = Math.max(0, Date.now() - digestStartedAt);
  return {
    digest,
    refusalReason: null,
    scannedRows,
    scopedRows,
    batchCount,
    pendingOutboxCount: 0,
    localRevision: finalState.localRevision,
    reusedSavedDigest: false,
    timings: timingSnapshot(),
  };
}

export async function localGeneralMessageSyncDigest(
  authScope: string,
  rows: readonly LocalGeneralMessageSyncRow[],
  hasPendingOutbox: boolean,
): Promise<string | null> {
  if (hasPendingOutbox) return null;
  const result = localGeneralMessageSyncMaterial(
    authScope,
    [...rows].sort((left, right) => left.id.localeCompare(right.id)),
  );
  return result.refusalReason
    ? null
    : Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.MD5, result.material);
}

/** Convert one bounded keyset page into the server's sorted id:fingerprint material. */
function localGeneralMessageSyncMaterial(
  authScope: string,
  rows: readonly LocalGeneralMessageSyncRow[],
): {
  material: string;
  scopedRows: number;
  refusalReason: GeneralMessageSyncReceiptRefusalReason | null;
} {
  const active: string[] = [];
  let scopedRows = 0;
  for (const row of rows) {
    if (row.deletedAt !== null) continue;
    const metadata = (row.metadata && typeof row.metadata === "object"
      ? row.metadata
      : {}) as Record<string, unknown>;
    if (metadata[GENERAL_MESSAGE_SYNC_SCOPE_KEY] !== authScope) continue;
    scopedRows += 1;
    if (metadata.local_only === true) {
      return { material: "", scopedRows, refusalReason: "local_only_message" };
    }
    const fingerprint = metadata[GENERAL_MESSAGE_SYNC_FINGERPRINT_KEY];
    const projection = metadata[GENERAL_MESSAGE_SYNC_PROJECTION_KEY];
    if (typeof fingerprint !== "string" || !FINGERPRINT_PATTERN.test(fingerprint)) {
      return { material: "", scopedRows, refusalReason: "missing_fingerprint" };
    }
    const serverType = metadata.sender_type;
    const serverId = metadata.sender_id;
    const serverDisplayName = metadata.sender_display_name;
    const localProjection = generalMessageSyncProjection({
      session_id: row.sessionId,
      role: row.role,
      metadata,
      created_at: row.createdAt,
      updated_at: row.updatedAt,
      token_count: row.tokenCount,
      parent_message_id: row.parentMessageId,
      branch_index: row.branchIndex,
      is_active_branch: row.isActiveBranch,
      sender_type: typeof serverType === "string"
        ? serverType
        : serverType === null ? null : undefined,
      sender_id: typeof serverId === "string"
        ? serverId
        : serverId === null ? null : undefined,
      sender_display_name: typeof serverDisplayName === "string"
        ? serverDisplayName
        : serverDisplayName === null ? null : undefined,
    });
    if (projection !== localProjection) {
      return { material: "", scopedRows, refusalReason: "projection_mismatch" };
    }
    active.push(`${row.id}:${fingerprint}`);
  }
  return { material: active.join("\n"), scopedRows, refusalReason: null };
}
