import { getDb, schema } from "../db/client";
import {
  saveSelectedProjectId,
  saveSelectedSpaceId,
} from "../lib/auth";
import { clearCachedClipIngestTargets } from "../lib/clip-ingest-targets";

export async function clearLocalSyncCache(): Promise<void> {
  const db = getDb();

  // 認証切替では本体cacheを捨てても、未同期mutation（outbox）は捨てない。
  // outboxにはauth_scopeとreplay payloadが含まれるため、同一ユーザーへ戻った
  // ときにだけ再送できる。SQLiteの同期transactionでcache削除を原子的に行う。
  db.transaction((tx) => {
    tx.delete(schema.syncState).run();
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
  });

  // pending_clip_ingests はここでは消さない。未ログイン→ログインの遷移でも
  // 保留した入力を失わない契約のため（行ごとの auth_scope で送信先を絞る）。
  // clip_ingest_target_cache はスコープ別に持つが、認証遷移で無限に溜まらないよう
  // ここで全スコープ分を破棄する（次の同期で現スコープ分を取り直す）。
  clearCachedClipIngestTargets();

  await saveSelectedProjectId("");
  await saveSelectedSpaceId("");
}
