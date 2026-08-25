/**
 * Shared Repository types.
 *
 * A Repository is the single entry point each feature uses to talk to data.
 * It owns the Local-First read/write policy; callers never touch the REST
 * client or the SQLite layer directly.
 */

export type SyncAction = 'create' | 'update' | 'delete' | 'restore' | 'reorder';

export interface OutboxEnqueue {
  table: string;
  action: SyncAction;
  entityId: string;
  payload: unknown;
  /** Override only when the caller already holds a verified auth scope. */
  authScope?: string | null;
  baseUpdatedAt?: string | null;
  basePayload?: unknown;
  /** Composite Docs membership key for ACL quarantine isolation. */
  docsScopeKey?: string | null;
}
