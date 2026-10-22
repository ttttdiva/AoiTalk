# EmbeddingGemma 2 fresh canonical proof

本CLIは外部で停止した全canonical/index writerの下で、現在の正本からtransfer用のprivate証拠を生成する。停止・転送・承認・runtime切替・live ACKは行わない。停止の実行と継続はoperatorの責任。

```
python scripts/prove_embeddinggemma2_canonical.py --candidate <candidate絶対パス> --manifest <manifest絶対パス> --generation <generation> --source-binding <source DB SHA256> --code-sha <clean checkout完全SHA> --freeze-id <実際の停止作業ID> --output <既存private-directory内の新規proof.json> --confirm-all-writers-stopped
```

出力は`verify_embeddinggemma2_cutover.py --canonical-proof`へ渡す。既存proofを上書きしない。candidate・manifest・shadow・runtime store・workspaces・登録storage root・ローカルKnowledge原本rootの内部へ出力できない。既存のdescriptor-pinned file I/Oでsymlink/junctionと親directoryの置換を拒否する。POSIX新規file modeは0600。Windowsでは既存private directoryのACLをoperatorが用意する。

## 正規照合

- `RuntimeCandidate`の全domain列挙、Docs/Knowledge own-record builder、memory/mediaの現在合法reader集合、`_input_digest`を再利用する
- encoder、QdrantClient、MigrationLedgerを生成せず、全候補pointをread-only SQLiteで読む。SQLは既存read-only transaction契約でflush/commit/DMLを拒否する
- 別々のrollback-only transactionで2回、全source/status/reason/registration hash/unit/input digestを生成し一致を要求する
- current Unitと候補物理pointのlocator、payload、sparse値、memory/lexical復号textを直接照合し、transferの全source/unit/point bijectionとdense checkpointを再検証する
- 削除済みsourceは候補のIDを墓標の照会キーとしてだけ使い、状態やhashはfresh SQL/readerから再計算する。Docs/Knowledgeのzero-unit tombstoneも対象
- 登録metadataは明示的なSQL列projectionを`aoitalk-canonical-registration-v1`でhash化。UUIDは文字列、datetimeはUTC ISO、JSON objectはkey順固定、listは順序保持、nullは保持。不在原本のbyte hashを捏造しない
- 全sourceを現在のglobal authority graphにもbindする。無関係なACL変更でも旧gap manifestは無効となる保守的仕様。SQLの登録文字列・パス・本文をstdoutへ出さない

## mediaの一時物とvideo

原本・既存候補・共有cacheは変更しない。mediaのnative再準備とmanaged assetの正規materializeはrepo `.local`内の独立scratchを使い、成功・失敗・取消後にcleanupする。動画は古い共有clipをfresh証拠に流用せず再抽出する。

video clipのinput digestはMatroska全byteを含む。旧v2生成は同一原本でもcontainer byteが非決定的だったため、正規MediaPreparationServiceをbitexact指定へ修正し、cache keyをv3-bitexactへ変更した。旧cacheや原本は削除しない。古いvideo candidateは同じcandidate migrationをresumeし、current input digest不一致のvideo unitだけを再構築する。Docs/Knowledge/memory/audio/image/PDFの入力規約とIDは変更しない。異なるffmpeg versionでbyteが違う場合もvalidatorを緩めず候補再構築が必要。

## gapの境界

failed理由はoriginal_missing/original_path_invalidだけがproofに残せる。storage障害、encode/decode障害、未知の失敗、残存derivativeは拒否する。proof生成は承認を生成しない。transfer時はその時点のexact manifestと受領済み承認receiptが別途必要で、件数による特例はない。

実source DB、実ユーザー素材、実Server、実停止、Windows descriptor経路、GPU/nativeモデルencode、本番切替はfixture試験で代替しない。
