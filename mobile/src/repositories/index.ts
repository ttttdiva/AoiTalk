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
} from "./conversations";
export {
  timeEntriesRepo,
  buildTimeReportFromEntries,
  calculateTimeEntryDuration,
} from "./timeEntries";
export { enqueueOutbox } from "./outbox";
export * from "./records";
export type { SyncAction, OutboxEnqueue } from "./types";
