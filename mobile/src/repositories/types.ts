/**
 * Shared Repository types.
 *
 * A Repository is the single entry point each feature uses to talk to data.
 * It owns the Local-First read/write policy; callers never touch the REST
 * client or the SQLite layer directly.
 */

export type SyncAction = 'create' | 'update' | 'delete';

export interface OutboxEnqueue {
  table: string;
  action: SyncAction;
  entityId: string;
  payload: unknown;
  baseUpdatedAt?: string | null;
}
