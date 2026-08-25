import { db } from "@/db";
import { contentDeletionEvents } from "@/db/schema";

type ContentDeletionDb =
  | typeof db
  | Parameters<Parameters<typeof db.transaction>[0]>[0];

export type ContentDeletionAction =
  | "deleted"
  | "restored"
  | "purged"
  | "permanent_deleted";

export type ContentDeletionEventInput = {
  batchId: string;
  entityType: string;
  entityId: string;
  rootEntityId?: string | null;
  projectId?: string | null;
  actorUserId?: string | null;
  action: ContentDeletionAction;
  displayName?: string | null;
  source?: string | null;
  eventAt?: Date;
  metadata?: Record<string, unknown>;
};

/**
 * Append one immutable deletion lifecycle event.
 *
 * The table intentionally has no foreign-key cascade: audit history must not
 * disappear merely because the source content is later purged.  Callers that
 * mutate a subtree should pass one batch id to every event in that mutation.
 */
export async function appendContentDeletionEvent(
  client: ContentDeletionDb,
  input: ContentDeletionEventInput,
): Promise<void> {
  const forbidden = ["body", "content", "description", "message", "bytes", "attachment"];
  const containsForbiddenKey = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(containsForbiddenKey);
    if (!value || typeof value !== "object") return false;
    return Object.entries(value).some(([key, nested]) =>
      forbidden.some((token) => key.toLowerCase().includes(token)) ||
      containsForbiddenKey(nested),
    );
  };
  if (containsForbiddenKey(input.metadata ?? {})) {
    throw new Error("deletion audit metadata must not contain content fields");
  }
  await client.insert(contentDeletionEvents).values({
    batchId: input.batchId,
    entityType: input.entityType,
    entityId: input.entityId,
    rootEntityId: input.rootEntityId ?? null,
    projectId: input.projectId ?? null,
    actorUserId: input.actorUserId ?? null,
    action: input.action,
    displayName: input.displayName ?? null,
    source: input.source ?? "unknown",
    eventAt: input.eventAt ?? new Date(),
    metadata: input.metadata ?? {},
  });
}

export function createDeletionBatchId(): string {
  return crypto.randomUUID();
}
