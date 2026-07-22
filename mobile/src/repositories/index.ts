/**
 * Repository barrel.
 *
 * Each Repository is the single source of truth for one entity family and
 * hides the Local-First policy (SQLite cache + REST API + Sync Engine).
 */

export { tasksRepo, OfflineWriteError } from "./tasks";
export { projectsRepo } from "./projects";
export { occurrencesRepo } from "./occurrences";
export {
  conversationsRepo,
  flushPendingConversation,
  flushPendingConversations,
  getPromotedConversationSessionId,
} from "./conversations";
export {
  timeEntriesRepo,
  buildTimeReportFromEntries,
  calculateTimeEntryDuration,
} from "./timeEntries";
export { enqueueOutbox, listOutboxConflicts } from "./outbox";
export * from "./records";
export * from "./docs";
export type { SyncAction, OutboxEnqueue } from "./types";
