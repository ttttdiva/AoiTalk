import { sql } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { SYNC_V3_DEVICE_KEY_PREFIX } from "../sync/v3/state";
import {
  saveSelectedProjectId,
  saveSelectedSpaceId,
} from "../lib/auth";
import { clearCachedClipIngestTargets } from "../lib/clip-ingest-targets";
import { captureLegacy, markLegacyCaptureSourceClearedInWriter } from "../sync-v3/legacy-migration";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";

export async function clearLocalSyncCache(): Promise<void> {
  const db = getDb();

  // The four Docs dirty markers do not carry an account owner. Persist the
  // exact legacy snapshot before the normal account-cache cleanup can remove
  // those live rows. A failed capture aborts the transition fail-closed.
  await captureLegacy();

  // 認証切替では本体cacheを捨てても、未同期mutation（outbox）は捨てない。
  // outboxにはauth_scopeとreplay payloadが含まれるため、同一ユーザーへ戻った
  // ときにだけ再送できる。SQLiteの同期transactionでcache削除を原子的に行う。
  await runForegroundSqliteWrite((writer) => db.transaction((tx) => {
    markLegacyCaptureSourceClearedInWriter(writer, tx);
    // Sync v3 device identity is kept: /ops receipts MAC device_id and op_seq,
    // so a resend after re-login must reuse the same device and sequence.
    tx.delete(schema.syncState)
      .where(sql`substr(${schema.syncState.tableName}, 1, ${SYNC_V3_DEVICE_KEY_PREFIX.length}) <> ${SYNC_V3_DEVICE_KEY_PREFIX}`)
      .run();
    tx.delete(schema.syncV3Membership).run();
    tx.delete(schema.knowledgeFieldValues).run();
    tx.delete(schema.knowledgeNodeSupertags).run();
    tx.delete(schema.knowledgeSupertagFields).run();
    tx.delete(schema.knowledgeNodePlacements).run();
    tx.delete(schema.knowledgeEdges).run();
    tx.delete(schema.knowledgeFields).run();
    tx.delete(schema.knowledgeSupertags).run();
    tx.delete(schema.knowledgeNodes).run();
    tx.delete(schema.conversationMessages).run();
    tx.delete(schema.conversationSessions).run();
    tx.delete(schema.timeEntries).run();
    tx.delete(schema.taskOccurrences).run();
    tx.delete(schema.tasks).run();
    tx.delete(schema.projects).run();
    tx.delete(schema.spaces).run();
    tx.delete(schema.users).run();
    // These snapshots are keyed by entity id (not auth scope), so retaining
    // them across a user switch could surface another account's data.
    tx.delete(schema.taskDetailCache).run();
    tx.delete(schema.filerDirCache).run();
  }));

  // pending_clip_ingests はここでは消さない。未ログイン→ログインの遷移でも
  // 保留した入力を失わない契約のため（行ごとの auth_scope で送信先を絞る）。
  // clip_ingest_target_cache はスコープ別に持つが、認証遷移で無限に溜まらないよう
  // ここで全スコープ分を破棄する（次の同期で現スコープ分を取り直す）。
  clearCachedClipIngestTargets();

  await saveSelectedProjectId("");
  await saveSelectedSpaceId("");
}
