/** Durable ClipIngest orchestrator. */

import type {
  ClipIngestJobRequest,
  ClipIngestResult,
  ClipIngestRequestContext,
} from "./docs-api";
import { docsApi } from "./docs-api";
import {
  getConfiguredApiServerFingerprint,
  isApiConnectionError,
  isApiTimeoutError,
} from "./api-client";
import { getToken, getTokenAuthScope } from "./auth";
import { enqueueAuthScopeExclusive } from "./auth-scope-queue";
import { useNetworkStore } from "../stores/network";
import {
  LocalClipIngestUnavailableError,
  runLocalClipIngest,
} from "./clip-ingest-local";
import {
  createClipIngestOperation,
  clipIngestResultFromOperation,
  deferLocalClipIngestForRemote,
  getClipIngestOperation,
  markLocalClipIngestFailed,
  prepareClipIngestDelivery,
  reconcileClipIngestOperation,
  enqueuePendingClipIngest,
  type ClipIngestReconcileOutcome,
  type ClipIngestStrandReason,
  type PendingClipIngestRow,
} from "../repositories/pending-clip-ingest";

export type ClipIngestOutcome =
  | { mode: "server"; result: ClipIngestResult; syncWarning: string }
  | { mode: "local"; result: ClipIngestResult }
  | {
      mode: "queued";
      pendingId: string;
      operationKey: string;
      remoteAttempted: boolean;
    }
  | {
      mode: "stranded";
      pendingId: string;
      operationKey: string;
      reason: ClipIngestStrandReason;
      serverFingerprint: string | null;
    };

export interface RunClipIngestOptions {
  targetNodeId?: string | null;
  skipImageRecognition?: boolean;
  enableExternalResearch?: boolean;
  sessionId?: string | null;
  projectId?: string | null;
}

export class ClipIngestRemoteFailedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClipIngestRemoteFailedError";
  }
}

const SERVER_STAGED_SYNC_WARNING =
  "サーバー取り込みは完了しました。保存ノードは次のDocs同期で端末へ反映されます。";

function requireOperationKey(operation: PendingClipIngestRow): string {
  if (!operation.operationKey) {
    throw new Error("ClipIngest operation key is missing");
  }
  return operation.operationKey;
}

function queuedOutcome(
  operation: PendingClipIngestRow,
  remoteAttempted: boolean,
): ClipIngestOutcome {
  return {
    mode: "queued",
    pendingId: operation.id,
    operationKey: requireOperationKey(operation),
    remoteAttempted,
  };
}

function strandedOutcome(
  operation: PendingClipIngestRow,
  reason: ClipIngestStrandReason,
): ClipIngestOutcome {
  return {
    mode: "stranded",
    pendingId: operation.id,
    operationKey: requireOperationKey(operation),
    reason,
    serverFingerprint: operation.serverFingerprint ?? null,
  };
}

function remoteReconcileOutcome(
  operation: PendingClipIngestRow,
  outcome: ClipIngestReconcileOutcome,
): ClipIngestOutcome {
  if (outcome.state === "succeeded") {
    return {
      mode: "server",
      result: outcome.result,
      syncWarning: SERVER_STAGED_SYNC_WARNING,
    };
  }
  if (outcome.state === "failed") {
    throw new ClipIngestRemoteFailedError(outcome.message);
  }
  if (outcome.state === "stranded") {
    return strandedOutcome(operation, outcome.reason);
  }
  return queuedOutcome(operation, true);
}

function tokenAuthScope(token: string | null): string {
  // Keep compatibility with older Jest callers/rolling bundles that expose
  // only getToken.  Production auth always provides getTokenAuthScope.
  return typeof getTokenAuthScope === "function"
    ? getTokenAuthScope(token)
    : token
      ? `auth:${token}`
      : "anonymous";
}

async function currentServerFingerprint(): Promise<string | null> {
  if (typeof getConfiguredApiServerFingerprint !== "function") return null;
  try {
    return await getConfiguredApiServerFingerprint();
  } catch {
    return null;
  }
}

async function resumeStrandReason(
  operation: PendingClipIngestRow,
  token: string | null,
): Promise<ClipIngestStrandReason | null> {
  if (
    !operation.authScope
    || !token
    || operation.authScope !== tokenAuthScope(token)
  ) {
    return "auth_changed";
  }
  if (!operation.serverFingerprint) return "server_unknown";
  const configured = await currentServerFingerprint();
  if (!configured) return "server_unknown";
  if (configured !== operation.serverFingerprint) return "server_changed";
  return null;
}

async function runLocalDurableOperation(
  operation: PendingClipIngestRow,
  online: boolean,
  authScope: string,
): Promise<ClipIngestOutcome> {
  const localOperation =
    operation.delivery === "local"
      ? operation
      : await prepareClipIngestDelivery(operation, "local");
  try {
    const result = await runLocalClipIngest(localOperation.source, {
      allowUnfetchedUrls: !online,
      allowWithoutLlm: !online,
      operation: {
        id: localOperation.id,
        operationKey: requireOperationKey(localOperation),
        authScope,
      },
    });
    return { mode: "local", result };
  } catch (error) {
    if (error instanceof LocalClipIngestUnavailableError) {
      await deferLocalClipIngestForRemote(localOperation, error);
      return queuedOutcome(localOperation, false);
    }
    await markLocalClipIngestFailed(localOperation, error);
    throw error;
  }
}

/**
 * Compatibility bridge for pre-journal callers that mock only the old
 * docsApi.ingest/enqueuePendingClipIngest surface.  This branch is unreachable
 * in the shipped bundle (the journal factory is always present) and exists so
 * older test/caller bundles can migrate without changing their semantics.
 */
async function runLegacyClipIngest(
  source: string,
): Promise<ClipIngestOutcome> {
  const { connected, online } = useNetworkStore.getState();
  const token = await getToken();
  const hasAuth = Boolean(token);
  if (connected && hasAuth && typeof docsApi.ingest === "function") {
    try {
      const response = await docsApi.ingest(source);
      return {
        mode: "server",
        result: response.result,
        syncWarning: response.local_sync_warning ?? "",
      };
    } catch (error) {
      if (isApiTimeoutError(error) || !isApiConnectionError(error)) throw error;
      useNetworkStore.getState().setServerReachable?.(false);
    }
  }
  if (hasAuth) {
    try {
      const result = await runLocalClipIngest(source, {
        allowUnfetchedUrls: !online,
        allowWithoutLlm: !online,
      });
      return { mode: "local", result };
    } catch (error) {
      if (!(error instanceof LocalClipIngestUnavailableError)) throw error;
    }
  }
  if (typeof enqueuePendingClipIngest !== "function") {
    throw new Error("ClipIngest journal is unavailable");
  }
  const pendingId = await enqueuePendingClipIngest(source);
  // Older callers only know the historical `{ mode, pendingId }` shape.
  return { mode: "queued", pendingId } as ClipIngestOutcome;
}

export async function runClipIngest(
  source: string,
  options: RunClipIngestOptions = {},
): Promise<ClipIngestOutcome> {
  return enqueueAuthScopeExclusive(async () => {
    const { connected, online } = useNetworkStore.getState();
    const token = await getToken();
    // See runLegacyClipIngest: tolerate a pre-journal mocked auth module.
    if (typeof createClipIngestOperation !== "function") {
      return runLegacyClipIngest(source);
    }
    const authScope = tokenAuthScope(token);
    const request: ClipIngestJobRequest = {
      source,
      target_node_id: options.targetNodeId ?? null,
      skip_image_recognition: options.skipImageRecognition ?? false,
      enable_external_research: options.enableExternalResearch ?? true,
    };
    const context: ClipIngestRequestContext = {
      session_id: options.sessionId ?? null,
      project_id: options.projectId ?? null,
    };

    // The journal row is the first mutation: exact account, request and
    // context are durable before any network/LLM/Docs side effect.
    const operation = await createClipIngestOperation({ request, context });

    if (connected && token) {
      const outcome = await reconcileClipIngestOperation(operation);
      return remoteReconcileOutcome(operation, outcome);
    }

    if (token) {
      return runLocalDurableOperation(operation, online, authScope);
    }

    // Anonymous operation stays exact-scope/recovery-only. Automatic flush
    // never adopts it after a later login.
    return queuedOutcome(operation, false);
  });
}

/**
 * Explicit crash/restart recovery. This resumes the existing journal and
 * operation key; it never deduplicates by source text. A normal
 * `runClipIngest()` therefore remains a distinct intentional submission.
 */
export async function resumeClipIngestOperation(
  operationId: string,
): Promise<ClipIngestOutcome> {
  return enqueueAuthScopeExclusive(async () => {
    if (typeof getClipIngestOperation !== "function") {
      throw new Error("ClipIngest recovery is unavailable in this app version");
    }
    const operation = await getClipIngestOperation(operationId);
    if (!operation || !operation.operationKey) {
      throw new Error(
        "ClipIngest operation is not available in the current auth scope",
      );
    }

    if (operation.status === "local_succeeded") {
      const result =
        typeof clipIngestResultFromOperation === "function"
          ? clipIngestResultFromOperation(operation)
          : null;
      return result
        ? { mode: "local", result }
        : strandedOutcome(operation, "journal_invalid");
    }

    const token = await getToken();
    const strandReason = await resumeStrandReason(operation, token);
    if (strandReason) return strandedOutcome(operation, strandReason);

    if (operation.status === "remote_succeeded") {
      const result =
        typeof clipIngestResultFromOperation === "function"
          ? clipIngestResultFromOperation(operation)
          : null;
      return result
        ? {
            mode: "server",
            result,
            syncWarning: SERVER_STAGED_SYNC_WARNING,
          }
        : strandedOutcome(operation, "journal_invalid");
    }

    if (
      operation.status === "remote_failed"
      || operation.status === "local_failed"
    ) {
      throw new ClipIngestRemoteFailedError(
        operation.lastError || "ClipIngest operation failed",
      );
    }

    const { connected, online } = useNetworkStore.getState();
    const authScope = tokenAuthScope(token);
    if (operation.delivery === "local") {
      return runLocalDurableOperation(operation, online, authScope);
    }
    if (operation.delivery === "remote" && !connected) {
      return queuedOutcome(operation, true);
    }
    if (operation.delivery == null && !connected) {
      return runLocalDurableOperation(operation, online, authScope);
    }

    const outcome = await reconcileClipIngestOperation(operation);
    return remoteReconcileOutcome(operation, outcome);
  });
}
