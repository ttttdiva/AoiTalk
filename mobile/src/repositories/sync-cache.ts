import { getDb, schema } from "../db/client";
import {
  saveSelectedProjectId,
  saveSelectedSpaceId,
} from "../lib/auth";

export async function clearLocalSyncCache(): Promise<void> {
  const db = getDb();

  await db.delete(schema.conversationMessages);
  await db.delete(schema.conversationSessions);
  await db.delete(schema.timeEntries);
  await db.delete(schema.taskOccurrences);
  await db.delete(schema.tasks);
  await db.delete(schema.projects);
  await db.delete(schema.spaces);
  await db.delete(schema.users);
  await db.delete(schema.outbox);
  await db.delete(schema.syncState);

  await saveSelectedProjectId("");
  await saveSelectedSpaceId("");
}
