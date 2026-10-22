# EmbeddingGemma 2 候補転送の operator gate

この手順は停止済み候補の転送・完全照合までを担当する。サービス停止・起動、runtime 設定変更、本番切替、Docs requested/applied の ACK、Knowledge.vector_id 更新、media live ledger 採用、原本・登録・旧 collection の削除を実行しない。

## 前提と未完了の境界

- 全 canonical source writer と全 index writer を停止し、その状態を fresh canonical 再照合の開始から転送完了まで維持する
- `migrate_embeddinggemma2.py --mode candidate --domain all` の候補について、canonical 全 source・全 unit・削除済み unit を停止下で再照合する。既存の candidate 実行だけでは下記 proof ファイルは自動生成されない
- 外部の停止・fresh canonical 検証担当が作成した証拠ファイルを入力する。転送器が「DB を fresh に読んだ」「全 writer を停止した」と自己申告する API はない。外部証拠の真偽をこの転送器が独立に証明するものではない
- 元候補と入力 manifest は現位置のまま使う。元候補を QdrantClient で開かない。通常 `.lock` と `.candidate-writer.lock` を取得し、接続前に未 checkpoint の SQLite journal を拒否、`mode=ro&immutable=1` と安全な PointStruct unpickler で bounded 読取する
- ローカル Qdrant serialization は 1.16.2 のみ対応。別バージョンは失敗させる。元候補を生成した version も canonical proof の binding に記録する
- target は明示した Qdrant HTTP endpoint。source は同時に1プロセスしか開けず、同一 operator checkout 内では target endpoint 共通 lock も取得する。この lock は複数ホストの distributed lease ではないため、別ホストの writer 停止は上記 freeze 証拠で担保する
- source や target の数値・payload・ID を弱く照合する skip/force/tolerance 引数はない

実 Qdrant Server と実 source DB、実ユーザー承認済み一覧による統合実行は別工程で必要。ローカル実体 fixture の成功を実 Server PASS や切替済みと扱わない。

## 実行

`python scripts/verify_embeddinggemma2_cutover.py --candidate <停止済み候補store絶対パス> --manifest <候補manifest絶対パス> --generation <同じgeneration> --source-binding <source DB binding SHA256> --target-endpoint <明示Qdrant URL> --receipt <独立receipt.sqlite> --canonical-proof <private-proof.json> --code-sha <現在のclean checkout commit SHA> --confirm-all-writers-stopped`

欠損がある場合は `--approval-manifest <private-approved-gaps.json> --approval-receipt <private-received-approval.json>` の両方も必須。コマンドは source と script の未 commit 変更を拒否する。認証情報を URL へ埋め込まない。

元候補に含まれる4 collection の完全一致を要求する。設定で base 名を変更した場合のみ `--docs-collection` / `--knowledge-collection` を指定する。モデル、dimension、domain別 fingerprint、input version は変更できない。

## canonical proof の形式

`schema` は `aoitalk-canonical-cutover-proof-v1`。`binding` は `runtime_transfer.proof_binding(source, code_sha)` の値と完全一致する。

- generation / source_binding / code_sha / qdrant_local_version
- embedding_fingerprint / input_identity
- candidate_binding_digest / candidate_snapshot_digest

`all_writers_stopped`、`canonical_rechecked`、`deleted_units_reconciled` はすべて true、`freeze_id` は実際の停止・検証作業を特定する非空ID。

`sources` の各要素は `source_key`、`status`、`reason`、`registration_metadata_hash` の4項目だけを含む。status は ready / excluded / failed。metadata hash は現在の canonical 登録 metadata を正規化した SHA256 であり、存在しない原本 bytes の hash を捏造してはいけない。対象の canonical metadata の射影・正規化規約は外部証拠担当が同一に保つ。

`units` の各要素は `[collection_name, source_key, unit_id, input_digest]`。fresh canonical の全 unit と input digest を記録する。候補の全 candidate_units と一致し、物理的な全 point との bijection を確認する。ready source の completed/total、global candidate:all の completion checkpoint も一致させる。失敗 source に残った古い derivative があれば拒否する。

候補の snapshot digest は lock 下で元 manifest・marker・meta.json・4 collection の storage.sqlite を全byte hashしたもの。証拠作成後に候補が変わった場合、転送途中や再開も拒否する。

## 承認済み欠損の厳密照合

承認 manifest の `schema` は `aoitalk-approved-search-gaps-v1`、`binding` は上記と同じ、`preserve_originals_and_registrations` は true、`gaps` は failed source の4項目を source_key 順に並べた完全な一覧。

`runtime_transfer.approval_request(proof)` は申請内容の作成にすぎず、承認は生成しない。別途受領した approval receipt が必要。

- schema: `aoitalk-gap-approval-receipt-v1`
- manifest_checksum: `runtime_transfer.digest(manifest)`
- generation: 同じ generation
- approval_reference: 実際の明示的承認を特定する非空参照
- explicitly_received: true

許容できる reason は original_missing / original_path_invalid だけ。167+5や172という件数は実装の allow 条件ではない。同数でも別source、新reason、metadata変更、source DB・モデル・input identity・generation・manifest checksum変更は拒否する。encode failure や corrupt vector は許容しない。原本や登録は触らない。

修復により欠損が減っても旧 manifest を暗黙流用しない。通常の canonical ready 証拠を完成し、更新した exact manifest とその明示的承認 receipt を用意する。単に一覧から項目を消す操作を修復成功とみなさない。

## 転送・receipt と結果の意味

各 collection を128件以下で scroll/upsert/retrieve する。768次元・finite・L2、ID集合、総数、余分point、全payload、domain別 fingerprint、sparse indexとfloat32値、dense数値差を照合する。Docsはv4、Knowledgeはknowledge-context-v1を含むそれぞれのfingerprint、media/memoryは共通 EMBEDDING_FINGERPRINT を使用する。

local 1.16.2 は入力PointStructを永続化する一方で add/update/reload の retrieve は丸めが異なる。このため raw からその既知表現だけを再現し candidate checkpoint の dense SHA256 と完全一致することを要求する。一般的な「近ければ再開可」という fallback はない。target Cosine 再正規化の数値比較は固定1e-5。

receipt は source/target/generation/code/evidence/receipt path に bind し、unit毎に input digest・dense digest・全point digestを保持する。再開時に prefix 全体を source と target から再検証する。別targetや破損prefixを上書きして隠さない。upsert後checkpoint前の中断も保存pointの完全再検証後にだけ継続する。

- 通常成功: transfer_complete=true、full_coverage=true、candidate_ready=true
- 欠損承認付き成功: transfer_complete=true、full_coverage=false、candidate_ready=false、cutover_eligible_with_approved_gaps=true
- 失敗・取消: receipt state=incomplete、reportなし。切替資格を出さない

結果はその時点の停止下検証証拠で、activation token やその後の自動監視ではない。writer再開後に古いreceiptだけで再切替しない。CLI出力は集計とbinding digestのみ。private proof/manifest/receipt はsource識別子を含むので GitHub・共有ログへ置かない。
