import * as FileSystem from "expo-file-system/legacy";
import type {
  ResolutionContext,
  ResolutionExport,
  ResolutionRecordView,
  ResolutionResult,
} from "../repositories/outbox-resolution-types";
import {
  assertOutboxResolutionContext,
  getOutboxResolutionExport,
  readOutboxResolutionRecord,
} from "../repositories/outbox-resolution";
import {
  downloadFileToDevice,
  type FilesDownloadProgress,
} from "./files-download";

export type OutboxResolutionExportStatus = "saved" | "shared" | "cancelled";

export type OutboxResolutionExportResult =
  | { ok: true; value: { status: OutboxResolutionExportStatus } }
  | { ok: false; reason: string };

const SAFE_REASONS: ReadonlySet<string> = new Set([
  "invalid_input",
  "journal_unavailable",
  "record_not_found",
  "revision_mismatch",
  "context_changed",
  "auth_scope_changed",
  "server_changed",
  "server_unavailable",
  "server_not_found",
  "server_forbidden",
  "server_version_missing",
  "local_snapshot_changed",
  "related_snapshot_changed",
  "membership_changed",
  "cache_snapshot_changed",
  "editor_changed",
  "sync_inflight",
  "duplicate_entity",
  "acl_changed",
  "scope_changed",
  "unsupported",
  "not_prepareable",
  "not_adoptable",
  "outbox_replaced",
  "draft_missing",
  "transaction_failed",
  "export_unavailable",
  "export_completion_unknown",
]);

const SAFE_EXPORT_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json$/;

function safeReason(reason: unknown): string {
  return typeof reason === "string" && SAFE_REASONS.has(reason)
    ? reason
    : "export_unavailable";
}

function errorReason(error: unknown): string {
  if (typeof error === "string") return safeReason(error);
  if (typeof error === "object" && error !== null) {
    const candidate = error as { reason?: unknown; message?: unknown };
    const fromReason = safeReason(candidate.reason);
    if (fromReason !== "export_unavailable") return fromReason;
    // A facade context assertion may use a finite reason as its Error message.
    // Preserve that finite value while keeping native/raw exception text private.
    return safeReason(candidate.message);
  }
  return "export_unavailable";
}

function isValidInput(
  recordId: string,
  authScope: string,
  expectedRevision: number,
): boolean {
  return (
    typeof recordId === "string" &&
    recordId.trim().length > 0 &&
    typeof authScope === "string" &&
    authScope.trim().length > 0 &&
    Number.isInteger(expectedRevision) &&
    expectedRevision >= 0
  );
}

function isSafeFileName(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 100 &&
    value === value.trim() &&
    !value.includes("..") &&
    SAFE_EXPORT_FILE_NAME.test(value)
  );
}

function isContext(value: unknown): value is ResolutionContext {
  if (typeof value !== "object" || value === null) return false;
  const context = value as Partial<ResolutionContext>;
  return (
    typeof context.authScope === "string" &&
    context.authScope.length > 0 &&
    typeof context.serverFingerprint === "string" &&
    context.serverFingerprint.length > 0 &&
    typeof context.credentialIdentityRevision === "string" &&
    context.credentialIdentityRevision.length > 0
  );
}

function isExport(value: unknown): value is ResolutionExport {
  if (typeof value !== "object" || value === null) return false;
  const exportValue = value as Partial<ResolutionExport>;
  return (
    typeof exportValue.json === "string" &&
    isContext(exportValue.context) &&
    isSafeFileName(exportValue.fileName)
  );
}

function isResult(value: unknown): value is ResolutionResult<ResolutionExport> {
  if (typeof value !== "object" || value === null) return false;
  const result = value as { ok?: unknown; value?: unknown; reason?: unknown };
  if (result.ok === true) return isExport(result.value);
  if (result.ok === false) return typeof result.reason === "string";
  return false;
}

function isRecordResult(
  value: unknown,
): value is ResolutionResult<ResolutionRecordView> {
  if (typeof value !== "object" || value === null) return false;
  const result = value as {
    ok?: unknown;
    value?: unknown;
    reason?: unknown;
  };
  if (result.ok === false) return typeof result.reason === "string";
  if (result.ok !== true || typeof result.value !== "object" || result.value === null) {
    return false;
  }
  const record = result.value as { item?: unknown };
  if (typeof record.item !== "object" || record.item === null) return false;
  const item = record.item as { revision?: unknown };
  return typeof item.revision === "number" && Number.isInteger(item.revision);
}

async function assertExportCurrent(
  recordId: string,
  authScope: string,
  expectedRevision: number,
  context: ResolutionContext,
): Promise<void> {
  await assertOutboxResolutionContext(context);
  const current = await readOutboxResolutionRecord(recordId, authScope);
  await assertOutboxResolutionContext(context);
  if (!isRecordResult(current)) {
    throw new Error("export_unavailable");
  }
  if (!current.ok) {
    throw new Error(safeReason(current.reason));
  }
  if (current.value.item.revision !== expectedRevision) {
    throw new Error("revision_mismatch");
  }
}

function joinPrivateCacheUri(root: string, fileName: string): string {
  const normalizedRoot = root.endsWith("/") ? root : `${root}/`;
  const extensionIndex = fileName.lastIndexOf(".");
  const stem = fileName.slice(0, extensionIndex);
  const extension = fileName.slice(extensionIndex);
  const uniqueSuffix = `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  return `${normalizedRoot}${stem}-${uniqueSuffix}${extension}`;
}

type ExternalExportState = {
  started: boolean;
  finalCompleted: boolean;
  status: "saved" | "shared" | null;
};

function updateExternalExportState(
  state: ExternalExportState,
  event: FilesDownloadProgress,
): void {
  if (event.state === "started") {
    state.started = true;
    state.finalCompleted = false;
    state.status = null;
    return;
  }
  if (event.operation === "copy") {
    state.finalCompleted = true;
    state.status = "saved";
  } else if (event.operation === "share") {
    state.finalCompleted = true;
    state.status = "shared";
  }
}

/**
 * Export one already-prepared resolution record after an explicit user action.
 *
 * The facade owns the protected journal and safe JSON projection.  This layer
 * only writes that projection to app-private cache storage and delegates the
 * user-selected external save/share operation to the existing download path.
 */
export async function exportOutboxResolutionRecord(
  recordId: string,
  authScope: string,
  expectedRevision: number,
): Promise<OutboxResolutionExportResult> {
  if (!isValidInput(recordId, authScope, expectedRevision)) {
    return { ok: false, reason: "invalid_input" };
  }

  const externalState: ExternalExportState = {
    started: false,
    finalCompleted: false,
    status: null,
  };

  try {
    const exportResult = await getOutboxResolutionExport(
      recordId,
      authScope,
      expectedRevision,
    );
    if (!isResult(exportResult)) {
      return { ok: false, reason: "export_unavailable" };
    }
    if (!exportResult.ok) {
      return { ok: false, reason: safeReason(exportResult.reason) };
    }

    const payload = exportResult.value;
    const privateRoot = FileSystem.cacheDirectory || FileSystem.documentDirectory;
    if (!privateRoot) return { ok: false, reason: "export_unavailable" };

    // Validate the context and the mutable private journal revision before
    // creating even the private source file.  The journal read is local only;
    // it does not issue another live GET or regenerate the export JSON.
    await assertExportCurrent(
      recordId,
      authScope,
      expectedRevision,
      payload.context,
    );
    const sourceUri = joinPrivateCacheUri(privateRoot, payload.fileName);

    const result = await downloadFileToDevice(
      {
        name: payload.fileName,
        path: sourceUri,
        type: "file",
        source: "local",
        mimeType: "application/json",
      },
      async () => {
        // Resolve the source only after the user has selected a destination or
        // the share sheet has become available.  A cancelled picker therefore
        // performs no file read/write at all.
        await assertExportCurrent(
          recordId,
          authScope,
          expectedRevision,
          payload.context,
        );
        await FileSystem.writeAsStringAsync(sourceUri, payload.json, {
          encoding: FileSystem.EncodingType.UTF8,
        });
        await assertExportCurrent(
          recordId,
          authScope,
          expectedRevision,
          payload.context,
        );
        return sourceUri;
      },
      async () => {
        await assertExportCurrent(
          recordId,
          authScope,
          expectedRevision,
          payload.context,
        );
      },
      (event) => {
        updateExternalExportState(externalState, event);
      },
    );
    return { ok: true, value: { status: result.status } };
  } catch (error) {
    if (externalState.started) {
      if (externalState.finalCompleted && externalState.status) {
        return { ok: true, value: { status: externalState.status } };
      }
      return { ok: false, reason: "export_completion_unknown" };
    }
    return { ok: false, reason: errorReason(error) };
  }
}
