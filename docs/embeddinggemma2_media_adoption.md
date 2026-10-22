# EmbeddingGemma 2 media live ledger 採用

このCLIは、停止中に転送・完全照合したmedia vectorを、既存の通常ledgerへ正規`sync_source`で採用する。runtimeの起動・設定切替は行わない。

## 必須条件

- `runtime_transfer` と fresh canonical proof producer を含む統合済みclean checkoutを同一SHAに固定する
- canonical source writerと全index writerの停止を、proof生成開始からこのCLI終了、別工程のactivationまで連続して維持する
- 候補全4collectionを実Serverへ転送し、同generation/source DB/model/input/code/endpointにbindしたverified receiptを用意する
- source status、reason、registration hashに完全一致する欠損manifestと明示受領approval receiptが必要。件数そのものに許可の意味はない
- `data/search/embeddinggemma2.sqlite` に既存の通常media ledgerが存在すること。candidate/shadow ledgerをこの場所へコピーしない

## 実行

`python scripts/adopt_embeddinggemma2_media.py --candidate <停止候補store絶対パス> --manifest <候補manifest> --generation <generation> --source-binding <DB binding SHA256> --target-endpoint <Qdrant URL> --transfer-receipt <verified receipt.sqlite> --canonical-proof <fresh-proof.json> --code-sha <clean checkout SHA> --confirm-all-writers-stopped`

欠損付きの場合は、`--approval-manifest <exact-private-gaps.json> --approval-receipt <received-approval.json>` を併用する。

任意ledger/ownerへの切替、skip/force、READYコピー、queue resetの引数はない。CLIは既定ledgerだけを更新する。

## 採用の意味

- 採用前後に正規fresh canonical builderを再実行し、入力proofと完全一致を要求する。source追加・削除や他domainの変化も停止条件になる。二重transaction検証を含み、停止時間は実測が必要
- verified receiptを変更せず、全4collectionの物理ID/payload/dense/sparse/checkpointを採用前後に再照合する
- mediaは全active Userを現在のACLで確認する。ownerがNULL・無効でも合法readerを考慮する
- 既存vectorが現在のchunk/content/lexical identityと一致する場合だけ再利用し、encoder初期化・encode・target create/upsert/deleteは行わない
- 動画のdecode中間物は隔離した一時scratchだけへ生成し、成功・失敗・取消時に削除する。共有media cacheは使わない
- 原本hash・metadata・現在readerを再確認した後、通常`sync_source`がREADYを書き込む
- 全readerの原本missing/path-invalidが承認済み理由と一致した場合は通常failedへ移す。古いlive READYを残して成功扱いにしない
- 権限失効・formal exclusion・登録削除は通常excludedへ移す。1人の権限失効を全体missingと見なさない
- EACCES/EIO/storage busy・不明な解決失敗は承認済みmissingへ変換せず、採用停止とする
- Docs requested/applied、Knowledge.vector_id、source登録、原本、queueは変更しない。暗号鍵も新規生成しない

## 中断・再開

失敗すると`media_adoption_complete=false`、activation未実施として終了する。ledgerには一部進捗が残り得るため、部分更新をruntime切替成功と扱わない。再実行でもreceipt・原本・sourceを再照合し、READYを盲信しない。

live ledgerにfresh proofへ含まれないmedia keyがある場合は停止する。候補の正規全coverage・削除済み登録の扱いを確認し、必要なproof/転送をやり直す。ledgerの手動リセットは行わない。

出力は集計とbinding digestのみ。private proof・manifest・receiptを共有ログやGitへ置かない。

ChatAttachment、TaskAttachment、GeneratedMediaはmissing/path-invalidを型付きで分類する。GeneratedMediaのroot offline、EACCES/EIOは欠損承認に混ぜない。Meeting/Docsの一部下位storage読取はgeneric failureを返すため、そのケースは許可済みreasonへ推測変換せず停止する。

## 検証境界

fixtureでは実SQLite ACL、実Qdrant 1.16.2 local engine、source bytes、動画decoderを検証する。Qdrant HTTP transportはdouble。実PostgreSQL、実Server、全本番media種別・登録、Windows/Gateway、activation・本番検索は別工程であり、このpatchのPASSとは区別する。
