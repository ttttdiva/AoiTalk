# Daily Capture

Daily Capture は、分類先を決めずに本人の Personal Library の Today に原文を保存する入口。
ClipIngest の記事取込や Task Knowledge Capture とは別の機能とする。

## 保存と整理

- POST `/api/docs/daily-captures` は `day`（ISO日付）、`content`、UUIDの `idempotency_key` を受ける。原文と durable job を同じ transaction で保存し、モデルを待たず返す
- `docs_library_id` を渡す場合も本人の Personal Library に限る。閲覧中の Project/共有ノートへ暗黙に送らない
- 原文は既存 Day の編集可能な markdown typed child。CRLF/CR→LF以外は空白・改行を含め保持し、原文をjobに複製しない
- Web/Python の Day 作成は同じ library の `FOR NO KEY UPDATE` fence を使う。通常のnode作成のFK `KEY SHARE` と競合する `FOR UPDATE` を使わない
- 原文保存にモデル/dispatchの成功は不要。workerはserver lifecycleで起動し、永続jobを短いclaim transactionで取得する
- 保存直後の新規captureだけが自動対象。既存ノート群の一括再整理、原文の移動/置換/要約、重複記事生成をしない

## モデルの権限

- ClipIngestと同じ現在のモデルルーティングを使うが、取込・外部調査pipelineは呼ばない
- 同じLibraryのACL適用済み検索結果と既存の通常タグだけを候補として渡す。候補探索はbounded/best-effortで、全ノートの完全走査ではない
- toolなしplain-text生成の厳密JSON `tag_ids` / `link_ids` のみ。各最大5件、候補ID以外・未知のkeyは拒否する
- Task/Day等のsystem tagを除外し、既存タグの付与と`related_to` edgeの作成だけを行う。Taskや期限、外部通信/検索、外部操作を生成しない
- 付与前に原文の版、Docs edit lease、library/policy revision、現在のactor/ノードACLとタグ定義を再検証。DocsMutationServiceのordered row lockとtag writerを使う
- モデル停止/不正応答/原文変更/失効は `failed`。原文を維持し、ユーザーの再試行で現在の原文から新しく候補・計画を作る。古い計画の再適用はしない
- claimにはtokenと期限を付け、モデル生成timeoutはleaseより短くする。最終transactionでもtoken/期限を検証し、失効workerのpublishを防ぐ。中断したrunは期限後failedとして再試行可能

## 履歴とundo

- `docs_daily_captures.history_json` は暗号化した試行履歴と成功時のrelation変更記録。APIは原文をcanonical nodeからその都度読み、履歴のlink targetも現在のACLで再確認する
- GET一覧は日付指定、最大100 capture、読み取りのみ。空のTodayやLibraryを作らない。POST retry/undoとworkerの確定はjob row lockで直列化する
- undoは成功したrunが実際に追加したrelationだけを対象にする。既存タグ/リンク、原文、位置、ユーザー追記は変更しない
- タグはnode/tag/creator/created_at/updated_at、edgeは固有IDと全可変属性を照合する。同一タグを利用者が削除して付け直した場合や編集済みedgeは保持する
- 消失、変更、アクセス不可になったrelationは安全にスキップし、`undo_skipped_count` に記録。残りの無変更relationだけを戻せる
- 期限切れた試行、成功後undoを含む記録は永続化。原文・保存時のDay・Library・actorの物理削除ではFK cascadeで台帳も除去する（通常のarchiveでは除去しない）

## クライアント/検証

Canonical FastAPIはcreate/list/get/retry/undoの5 operation。Webは既存Python proxy経由、Native Mobile向けも同じOpenAPIと生成型を提供する。Mobileのoffline同期は既存Docs node/tag/edge契約のままで、capture受付自体はonline操作。

Focused `tests/test_daily_capture.py` はSQLiteで実際のgraph/tag/edge/journal保存・再読込・undoを実行し、PostgreSQL専用ACL SQL/advisory lockだけを明示的に差し替える。PostgreSQLの実競合・trigger検証とは区別する。実モデルの分類品質はmock生成のテストで保証しない。
