import { eq, isNull } from "drizzle-orm";
import { schema } from "../db/client";
import {
  assertActiveWriter,
  type SqliteHandle,
  type SqliteWriter,
} from "../db/sqlite-writer";
import type { Space } from "../types/api";

const ALWAYS_CURRENT = () => true;

/**
 * Upsert spaces inside an active coordinator write unit. Returns false (and
 * writes nothing) when `isCurrentGeneration` is false (the account/generation
 * changed). The whole body is synchronous, so the generation cannot change
 * part-way through and the surrounding transaction stays atomic.
 */
export function applyRemoteSpacesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  list: Space[],
  isCurrentGeneration: () => boolean = ALWAYS_CURRENT,
): boolean {
  assertActiveWriter(writer);
  if (!isCurrentGeneration()) return false;
  const now = new Date().toISOString();
  for (const space of list) {
    tx
      .insert(schema.spaces)
      .values({
        id: space.id,
        name: space.name,
        slug: space.slug ?? null,
        description: space.description ?? null,
        color: space.color ?? null,
        ownerId: space.owner_id ?? null,
        sortOrder: space.sort_order ?? null,
        createdAt: space.created_at ?? now,
        updatedAt: space.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.spaces.id,
        set: {
          name: space.name,
          slug: space.slug ?? null,
          description: space.description ?? null,
          color: space.color ?? null,
          ownerId: space.owner_id ?? null,
          sortOrder: space.sort_order ?? null,
          updatedAt: space.updated_at ?? now,
          deletedAt: null,
        },
      })
      .run();
  }
  return isCurrentGeneration();
}

/**
 * Tombstone every locally active space whose id is not in `keepIds`, inside an
 * active coordinator write unit. Returns false when the generation changed.
 */
export function applySpaceTombstonesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  keepIds: ReadonlySet<string>,
  isCurrentGeneration: () => boolean = ALWAYS_CURRENT,
): boolean {
  assertActiveWriter(writer);
  if (!isCurrentGeneration()) return false;
  const now = new Date().toISOString();
  const existing = tx
    .select()
    .from(schema.spaces)
    .where(isNull(schema.spaces.deletedAt))
    .all();
  for (const row of existing) {
    if (!keepIds.has(row.id)) {
      tx
        .update(schema.spaces)
        .set({ deletedAt: now, updatedAt: now })
        .where(eq(schema.spaces.id, row.id))
        .run();
    }
  }
  return isCurrentGeneration();
}
