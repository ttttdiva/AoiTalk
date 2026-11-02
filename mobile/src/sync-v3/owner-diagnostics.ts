import { countChatComposerDraftsForOwner } from "../features/conversation/chat-composer-draft";
import { getConfiguredApiServerFingerprint } from "../lib/api-client";
import { getOutboxResolutionDraftCount, getOutboxResolutionIdentity } from "../repositories/outbox-resolution";
import type { ResolutionContext } from "../repositories/outbox-resolution-types";

export type ScopedDiagnosticCount = {
  status: "available" | "unknown";
  count: number | null;
};

export type OwnerSyncDiagnostics = {
  scopeStatus: "verified" | "unknown";
  chatComposerDrafts: ScopedDiagnosticCount;
  resolutionDrafts: ScopedDiagnosticCount;
  docsDirtyByTable: Record<
    "knowledge_nodes" | "knowledge_supertags" | "knowledge_node_supertags" | "knowledge_field_values",
    ScopedDiagnosticCount
  >;
  docsDirtyReason: "server_fingerprint_not_persisted_in_docs_membership" | null;
  v2FallbackHolds: ScopedDiagnosticCount;
  v2FallbackHoldReason: "server_fingerprint_not_persisted_in_v2_fallback_hold" | null;
};

const DIRTY_DOCS_TABLES = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_field_values",
] as const;

function unknownCount(): ScopedDiagnosticCount {
  return { status: "unknown", count: null };
}

function unknownOwnerDiagnostics(): OwnerSyncDiagnostics {
  return {
    scopeStatus: "unknown",
    chatComposerDrafts: unknownCount(),
    resolutionDrafts: unknownCount(),
    docsDirtyByTable: Object.fromEntries(DIRTY_DOCS_TABLES.map((table) => [table, unknownCount()])) as OwnerSyncDiagnostics["docsDirtyByTable"],
    docsDirtyReason: "server_fingerprint_not_persisted_in_docs_membership",
    v2FallbackHolds: unknownCount(),
    v2FallbackHoldReason: null,
  };
}

/** Read only counts tied to the current token and configured server. Private
 * draft values and resolution identifiers never leave their repositories. */
export async function getOwnerSyncDiagnostics(
  authScope: string,
  expectedIdentity: ResolutionContext,
): Promise<OwnerSyncDiagnostics> {
  if (!authScope || authScope === "anonymous" || !authScope.startsWith("auth:")
    || expectedIdentity.authScope !== authScope) {
    return unknownOwnerDiagnostics();
  }

  try {
    const identityBefore = await getOutboxResolutionIdentity();
    const serverBefore = await getConfiguredApiServerFingerprint();
    if (!identityBefore.ok || identityBefore.value.authScope !== authScope
      || identityBefore.value.serverFingerprint !== expectedIdentity.serverFingerprint
      || identityBefore.value.credentialIdentityRevision !== expectedIdentity.credentialIdentityRevision
      || identityBefore.value.serverFingerprint !== serverBefore) {
      return unknownOwnerDiagnostics();
    }
    const userId = authScope.slice("auth:".length);
    if (!userId) return unknownOwnerDiagnostics();

    const [chatComposerDrafts, resolutionDraftCount] = await Promise.all([
      countChatComposerDraftsForOwner({ authUser: userId, serverProfile: serverBefore }),
      getOutboxResolutionDraftCount(identityBefore.value),
    ]);
    const [identityAfter, serverAfter] = await Promise.all([
      getOutboxResolutionIdentity(),
      getConfiguredApiServerFingerprint(),
    ]);
    if (!identityAfter.ok || identityAfter.value.authScope !== authScope
      || identityAfter.value.serverFingerprint !== expectedIdentity.serverFingerprint
      || identityAfter.value.credentialIdentityRevision !== expectedIdentity.credentialIdentityRevision
      || serverAfter !== serverBefore) {
      return unknownOwnerDiagnostics();
    }

    return {
      scopeStatus: "verified",
      chatComposerDrafts,
      resolutionDrafts: resolutionDraftCount == null
        ? unknownCount()
        : { status: "available", count: resolutionDraftCount },
      // docs_scope_membership records authScope but no server fingerprint. An
      // authScope-only count cannot prove the requested server fence.
      docsDirtyByTable: Object.fromEntries(DIRTY_DOCS_TABLES.map((table) => [table, unknownCount()])) as OwnerSyncDiagnostics["docsDirtyByTable"],
      docsDirtyReason: "server_fingerprint_not_persisted_in_docs_membership",
      v2FallbackHolds: unknownCount(),
      v2FallbackHoldReason: "server_fingerprint_not_persisted_in_v2_fallback_hold",
    };
  } catch {
    return unknownOwnerDiagnostics();
  }
}
