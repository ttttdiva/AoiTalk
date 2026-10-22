# Files索引の保守レポートと全空確認

対象は `workspace_file` の検索派生データと索引の管理記録。User Files の
`daily/` も通常の FilesRoot 配下であり、将来削除される前提で扱う。
reportはread-only。purgeはreportから作るpinned planを、ロック下で再検証して適用する
別手順で、既定は検証のみ（`--execute` なしでは削除しない）。VACUUM・モデルロードはない。

## 既存日時の確認結果

`MigrationLedger.embedding_sources` に `updated_at` はあるが、これは進捗書込でも
更新され、cleanup完了日時にはならない。`embedding_attempts` に日時はない。
`media_jobs` にterminal日時はなく、`due` はretry/lease期限である。
`media_reconcile_sources.state='removed'` にremoved日時はない。
cursorの`last_completed`もソース単位の削除証明ではない。
canonical `workspace_file_entries.deleted_at` だけは既存日時がある。

## 独立したread-only CLI

```sh
venv/bin/python scripts/report_media_index_maintenance.py \
  --ledger /path/to/explicit-ledger.sqlite \
  --queue /path/to/explicit-queue.sqlite \
  --evidence /path/to/metadata-and-counts.json --retention-days 30
```

既定の実データパスはなく、両SQLiteを `mode=ro` / `query_only` で読む。
存在しないDBを作成しない。WALのcommitted行も読む。
evidenceを省略した場合はunknownとして保護する。CLIはsnapshot作成のために
本番DBやQdrantへ接続しない。metadata/count snapshotは、適用担当が許可された
read-only取得で用意する。実ファイルのmissingと、canonical行の不存在を混同しない。
canonicalのmissingは取得エラー/権限不足ではなく、完全で正常なcanonical取得による
不存在の確認を意味する。

evidenceの最小例（日時はUTC Unix秒、パス/本文を含めない）:

```json
{
  "observed_at": 1792544400,
  "canonical": {
    "workspace_file:UUID": {"state": "deleted", "changed_at": 1789088400}
  },
  "vectors": {
    "workspace_file:UUID": {
      "remaining": 0,
      "all_collections": true,
      "identities": ["実際のembedding fingerprint"],
      "verified_at": 1792544399
    }
  }
}
```

vector evidenceはsource_keyで全対象collection（現行/旧identityを含む）をexact count
した合計。途中エラー・未確認collectionは `all_collections: false`、未知残数はnull。
fingerprintを推測しない。既定でsnapshot自体と各vector countの検証日時が
それぞれ1時間以内の場合だけを採用する。
時刻が未来、最新の管理記録変更より前のcount、identity不一致も保護する。

`candidate` は canonical deleted/missing、全collection残数0、cleanup検証の永続日時、
全管理記録のterminal/removed日時、保持期間がすべて揃ったソースだけ。
`pending/running/failed` は期限切れleaseでも保護する。failedはterminalでも再調査対象。
live unsupported/excluded、ready、actorだけの権限喪失、未割当/未知state、証跡不足も保護。
sourceがcandidateでもpurge許可にはならない。二つのSQLiteと外部snapshotは分散した
観測であり、将来purgeを実装するならwriter停止/lock下で再検証する必要がある。
shared reconcile cursorはsource削除で消せない制御記録として保護する。

`logical_rows` はテーブル行数。SQLiteのpage_count/freelist_countとDB/WAL/SHMの
実バイト数は別の指標であり、候補行数から回収可能ディスク量を算出しない。

## integration branchで接続済みの処理

対象branch `fix/media-index-text-batches-20261021`、確認HEAD
`918aff52b4348183f34d17e5657e0d04ae8e6ac4` の3 commitを
`fix/media-index-cleanup-integration-20261021` へcherry-pick済み。
mainへの統合や実データへの適用は行っていない。

1. `MigrationLedger.__init__` / `MediaIndexQueue.__init__` の既存schema作成後、
   同じSQLite transaction内で `prepare_lifecycle_schema(db)` を呼ぶ。
   import先は `src.services.media_index_maintenance`。
   補助テーブル+triggerなので既存6列の positional reconcile INSERTを変えない。
   旧行のfirst_tracked/terminal/removedはmigration時点から開始し、古いupdated_at等を
   遡及利用しない。再適用しても起点は変わらない。reportはこのmigrationを呼ばない。
   再open時は既存triggerを確認し、台帳全行のbackfillを繰り返さない。
2. `_withdraw_source` のexact count 0検証後、`ledger.mark(..., 'excluded')` の**後**、
   同じsource lockを保持したまま `with ledger.connect() as db:` 内で
   `record_cleanup_verified(db, ref.key, EMBEDDING_FINGERPRINT, remaining=remaining.count)`。
   一般のunsupported/excluded経路では呼ばない。markを先に行う理由は、source更新triggerが
   既存のcleanup証跡を無効化するため。クラッシュ時は未検証として保護される。
   cleanup証跡はそのidentityの検証、別途report snapshotで旧collectionも確認する。
   Filesのdeleted_atありは `canonical_source_deleted` としてlive excludedから区別する。
   これによりexcluded mark直後・検証記録前に中断しても、retryでexact countを再確認する。
3. 削除cleanupのqueue priorityを通常textより先にする。期限切れlease優先は同一priority内
   の既存担当差分を維持し、running leaseを横取りしない。
   deleted canonical metadataを基準に専用priorityを設定し、単なるunsupportedと区別する。
   priorityは-10で、未完了cleanupを後着の通常通知で降格させない。
4. scannerの通知はcanonical commit後、上限100件ずつthreadでenqueueする。
   各pageは1transactionで、page間はevent loopへ制御を戻す。
   `enqueue_many` は入力を101件までしか消費せず、100件超は書込前に拒否する。
   reconciliationは既存のbounded page/cursorを維持し、removed通知もcleanup priorityで送る。
   再通知はgeneration/tokenによる既存CASを維持し、missed notificationはcanonical
   reconciliationで回収する。新しい全体queue/frameworkは作らない。

30日経過だけでcanonical tombstoneを削除する既存の自動purge経路は撤去した。
workerが長期停止していてもretry/reconciliation用のsource ID・actor・path・deleted日時を
保持する。現行identityのcleanup済みでも旧collection全体の証明にはならないため、
今回のruntimeはtombstoneを一律保持する。将来の安全なpurgeは別途全collectionの検証・
writerとの排他・再確認を必要とする。モデル停止中でも手掛かりが先に消えない。

旧collectionのvectorをこのhookが削除するわけではない。reportには引き続き明示的な
全collection count snapshotが必要で、現在のcleanup日時だけからall-collections=0とは
推定しない。管理記録・canonical tombstoneの回収は上記pinned purgeの明示実行のみで、自動削除や物理領域回収はない。
scanner自体の全走査やnotice listは依然inventory全体に比例し、boundedなのはenqueueの
入力・transactionと既存reconciliation pageである。activeなrunning leaseは横取りしない。

## 全空root確認の適用

全空検知は別commit。Alembic `20261021_0001` 適用後、同じFilesRootの正常なnonempty
full scanでbaselineを確立する。以前の全live pathを証明できない差替えrootではbaselineを
更新しない。root path + filesystem/directory identity + 有効scope設定の一致、完全走査、
エラーなしを確認し、空観測をDBへ保存してscan interval後の再確認で削除を検出する。
再起動でも空証跡は残る。hint scanは確認に使わない。

unknown/零identity、permission error、root差替え、走査中のidentity変化、scope変更、
legacyで健康なbaselineがない全空は削除しない。FilesRootは `_users/_projects` のみで、
追加StorageRoot registryのidentityや権限と混同しない。OSのfile-id再利用/偽装やscan直後の
並行ファイル変更まで絶対保証するものではない。

空のhint scanは次のfull scanを待つ保守的な挙動になる。

## pinned purge（削除済みFilesの管理記録とtombstoneの回収）

```sh
venv/bin/python scripts/report_media_index_maintenance.py --ledger L --queue Q   --evidence E.json --plan-out plan.json
venv/bin/python scripts/apply_media_index_purge.py --ledger L --queue Q --plan plan.json
venv/bin/python scripts/apply_media_index_purge.py --ledger L --queue Q --plan plan.json --execute
```

planはreportの`candidate`（全collection残数0・cleanup検証・terminal/removed・保持期間経過）
だけを最大100件、各管理記録の`row_key`/state/lifecycle `changed_at`（source generation）
付きで固定する。現行collectionだけの0（`all_collections: false`）や保持期限前はplanに入らない。
applyはplan生成から1時間以内のみ受け付け、sourceごとに次の順で処理する。

1. `sync_source` と同じsource FileLock（`source_file_lock_path`）を取得
2. ledger/queueの両SQLiteで `BEGIN IMMEDIATE`
3. planの行集合・state・`changed_at`・cleanup検証が一致することを再検証。新しいjob
   generation、別actorのattempt、再mark等があれば `skipped` にして何も消さない
4. canonical `deleted` は `status='deleted'` かつ plan時点以前の `deleted_at` のtombstoneだけを
   条件付きDELETEしてcommit（復元・再削除されていればskip）。`missing` はcanonical操作なし
5. 両SQLiteの対象行をDELETEしてcommit（lifecycle行はtriggerで同時に消える）

4と5の間で中断した場合、残る管理記録はcanonical `missing` として次回のreport/planで回収される。
VACUUM/checkpointは行わない。将来 `daily/` を削除した場合も、scannerのcleanup通知→
`_withdraw_source` のexact 0検証→全collection count snapshot→保持期間経過→pinned purge
の順で派生vector・管理記録・tombstoneが残らない。

## fixture検証

```sh
venv/bin/python -m pytest tests/test_media_index_maintenance.py \
  tests/test_media_index_cleanup_hooks.py tests/test_media_index_batches.py \
  tests/test_media_index_queue_leases.py tests/test_media_index_queue_retries.py \
  tests/test_media_index_cleanup_queue.py tests/test_workspace_file_cleanup_notifications.py \
  tests/test_workspace_file_tombstone_retention.py tests/test_workspace_files_index.py \
  tests/test_embeddinggemma_index_security.py tests/test_embeddinggemma_reconciliation.py \
  -k "not office_files_index_as_text_units" \
  --basetemp=.local/cleanup-integration-final -q --disable-warnings
```

143 passed / 1 deselected。除外した既存Office変換testは内部のMarkItDownがMagika
モデルをロードするため、今回のモデルロード禁止条件に従う。全追加fixture、root差替え
回帰、in-memory vector withdrawal、read-only CLI、SQLite migration/WAL検証は通過。
purgeはSQLite/aiosqlite fixtureでのみ実行。実DB・本番データには接続せず、実purge/VACUUM/実daily削除は未実施。
適用時はAlembic `20261021_0001` と健康な非空root baselineを先に確立する。
SQLite lifecycleはwriter constructorで初回準備され、旧行の保持起点はその適用時点となる。
