/**
 * In-memory guard for outbox resolution while an editor route is mounted.
 *
 * This module deliberately keeps only scope/table/entity keys and owner
 * probes. Draft contents, payloads, credentials, and persistence handles do
 * not cross this boundary.
 */

export type OutboxResolutionEditorTable = "tasks" | "knowledge_nodes";

export type OutboxResolutionEditorTarget = {
  tableName: string;
  entityId: string;
};

export type OutboxResolutionEditorIdleProbe = () => boolean | Promise<boolean>;

export type OutboxResolutionEditorRegistrationOptions = {
  idleProbe?: OutboxResolutionEditorIdleProbe;
};

type OutboxResolutionEditorListener = () => void;
type OutboxResolutionEditorOwner = {
  idleProbe?: OutboxResolutionEditorIdleProbe;
  released: boolean;
};

const registrations = new Map<string, Set<OutboxResolutionEditorOwner>>();
const listeners = new Set<OutboxResolutionEditorListener>();

let revision = 0;

function registrationKey(
  authScope: string,
  tableName: OutboxResolutionEditorTable,
  entityId: string,
): string {
  return `${authScope}\u0000${tableName}\u0000${entityId}`;
}

function normalizedRegistration(
  authScope: string,
  tableName: string,
  entityId: string,
): { authScope: string; tableName: OutboxResolutionEditorTable; entityId: string } | null {
  const normalizedScope = typeof authScope === "string" ? authScope.trim() : "";
  const normalizedEntityId = typeof entityId === "string" ? entityId.trim() : "";
  if (!normalizedScope || !normalizedEntityId) return null;
  if (tableName !== "tasks" && tableName !== "knowledge_nodes") return null;
  return {
    authScope: normalizedScope,
    tableName: tableName as OutboxResolutionEditorTable,
    entityId: normalizedEntityId,
  };
}

function notifyListeners(): void {
  // A listener is UI invalidation code. One faulty subscriber must not prevent
  // the remaining subscribers from observing the new revision.
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // Keep the guard mutation independent from consumer errors.
    }
  }
}

function bumpRevision(): void {
  revision += 1;
  notifyListeners();
}

function releaseOwner(
  key: string,
  owner: OutboxResolutionEditorOwner,
): void {
  if (owner.released) return;
  owner.released = true;
  const owners = registrations.get(key);
  if (!owners || !owners.delete(owner)) return;
  if (owners.size === 0) registrations.delete(key);
  bumpRevision();
}

function targetSuffix(
  tableName: OutboxResolutionEditorTable,
  entityId: string,
): string {
  return `\u0000${tableName}\u0000${entityId}`;
}

function matchingKeysForTargets(
  authScope: string | null,
  targets: ReadonlyArray<OutboxResolutionEditorTarget>,
): Set<string> {
  const keys = new Set<string>();
  for (const target of targets) {
    const normalized = normalizedRegistration(
      authScope ?? "metadata-only",
      target.tableName,
      target.entityId,
    );
    if (!normalized) continue;
    const suffix = targetSuffix(normalized.tableName, normalized.entityId);
    if (authScope === null) {
      for (const key of registrations.keys()) {
        if (key.endsWith(suffix)) keys.add(key);
      }
    } else {
      keys.add(registrationKey(
        normalized.authScope,
        normalized.tableName,
        normalized.entityId,
      ));
    }
  }
  return keys;
}

async function refreshOwnerKeys(keys: ReadonlySet<string>): Promise<void> {
  const owners: Array<{ key: string; owner: OutboxResolutionEditorOwner }> = [];
  for (const key of keys) {
    for (const owner of registrations.get(key) ?? []) {
      owners.push({ key, owner });
    }
  }

  for (const { key, owner } of owners) {
    if (owner.released || !owner.idleProbe) continue;
    let idle = false;
    try {
      idle = await owner.idleProbe();
    } catch {
      idle = false;
    }
    if (idle) releaseOwner(key, owner);
  }
}

/**
 * Mark one mounted editor as an owner of an outbox entity resolution guard.
 * The returned release function is idempotent and only releases its owner.
 */
export function registerOutboxResolutionEditor(
  authScope: string,
  tableName: OutboxResolutionEditorTable,
  entityId: string,
  options?: OutboxResolutionEditorRegistrationOptions | OutboxResolutionEditorIdleProbe,
): () => void {
  const normalized = normalizedRegistration(authScope, tableName, entityId);
  if (!normalized) return () => undefined;

  const key = registrationKey(
    normalized.authScope,
    normalized.tableName,
    normalized.entityId,
  );
  const idleProbe = typeof options === "function"
    ? options
    : typeof options?.idleProbe === "function" ? options.idleProbe : undefined;
  const owner: OutboxResolutionEditorOwner = {
    idleProbe,
    released: false,
  };
  const owners = registrations.get(key) ?? new Set<OutboxResolutionEditorOwner>();
  owners.add(owner);
  registrations.set(key, owners);
  bumpRevision();

  return () => {
    releaseOwner(key, owner);
  };
}

/** Return the revision used by UI consumers to invalidate stale previews. */
export function getOutboxResolutionEditorRevision(): number {
  return revision;
}

/** Subscribe to registration/refcount changes. */
export function subscribeOutboxResolutionEditors(
  callback: OutboxResolutionEditorListener,
): () => void {
  listeners.add(callback);
  let unsubscribed = false;
  return () => {
    if (unsubscribed) return;
    unsubscribed = true;
    listeners.delete(callback);
  };
}

/**
 * Check whether any mounted editor owns one of the requested targets for the
 * supplied auth scope. Empty/invalid targets fail closed at the caller by
 * returning false here because no concrete target can be matched.
 */
export function hasOutboxResolutionEditors(
  authScope: string,
  targets: ReadonlyArray<OutboxResolutionEditorTarget>,
): boolean {
  const normalizedScope = typeof authScope === "string" ? authScope.trim() : "";
  if (!normalizedScope || targets.length === 0) return false;

  return targets.some((target) => {
    const normalized = normalizedRegistration(
      normalizedScope,
      target.tableName,
      target.entityId,
    );
    if (!normalized) return false;
    return (registrations.get(
      registrationKey(
        normalized.authScope,
        normalized.tableName,
        normalized.entityId,
      ),
    )?.size ?? 0) > 0;
  });
}

/** Metadata-only target check used when the canonical auth scope is unknown. */
export function hasOutboxResolutionEditorsForTargets(
  targets: ReadonlyArray<OutboxResolutionEditorTarget>,
): boolean {
  return [...matchingKeysForTargets(null, targets)].some((key) =>
    (registrations.get(key)?.size ?? 0) > 0,
  );
}

/**
 * Re-evaluate only matching owners after an explicit compare/adopt attempt.
 * Mounted editors return false from their probe; cleanup-retained owners may
 * release only after their existing work is already idle.
 */
export async function refreshOutboxResolutionEditorGuards(
  authScope: string,
  targets: ReadonlyArray<OutboxResolutionEditorTarget>,
): Promise<void> {
  const normalizedScope = typeof authScope === "string" ? authScope.trim() : "";
  if (!normalizedScope || targets.length === 0) return;
  await refreshOwnerKeys(matchingKeysForTargets(normalizedScope, targets));
}

/** Re-evaluate all scopes for the supplied local target metadata only. */
export async function refreshOutboxResolutionEditorGuardsForTargets(
  targets: ReadonlyArray<OutboxResolutionEditorTarget>,
): Promise<void> {
  if (targets.length === 0) return;
  await refreshOwnerKeys(matchingKeysForTargets(null, targets));
}
