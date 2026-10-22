# EmbeddingGemma 2: 本番切替レビューと復旧の条件

この文書は切替の設計・受入条件であり、切替成功の記録ではない。
対象コードを最終統合後の完全SHAで固定し、下記の実測証跡が揃うまで稼働構成を変えない。
既存の手順・検証範囲は `docs/embeddinggemma2_search.md` を参照する。

## 1. 今回の設計判断

- 埋め込みは固定revisionのEmbeddingGemma 2、768次元、正規化済みvectorだけを使用する。
- Qdrantの役割と文字列・完全一致・スパース検索を維持する。今回の切替だけを理由に別DBや常設の二重検索を増やさない。
- owner別shadowは受入検証のための部分索引である。フォルダ結合、binding解除、最後のownerのstoreをそのまま本番指定する方法は採用しない。
- shadowと本番のDocs metadataは別物。shadowのREADYを本番のapplied revisionやoutbox ACKとしてコピーしない。
- 本番切替と旧方式の整理は別の検証段階にする。切替前に旧collectionを消さない。旧モデルへの恒久fallbackは作らない。

## 2. 完走直後に揃える証跡

| 確認 | 必須条件 |
| --- | --- |
| 実行終了 | 所有ジョブの終了コード、対象owner数、各domainの完了・除外・失敗を記録。途中の保存件数だけで完了としない |
| 分母 | 原本record、source、chunk/unit、重複除去済みIDの数を分ける。異なる単位を割って進捗率にしない |
| identity | model/revision/dimension/prefix/pooling/正規化のfingerprintが候補全collectionで一致 |
| vector | 保存後のID集合・dimension・finite・L2・input/content/lexical hashを照合 |
| 現在の権限 | User、Project、Space、Docs share、会話参加者、branch、削除・マスキング状態を原本から再確認 |
| 原本 | 原本hash不変、元会話・ファイル・PDFページ・時間区間を検索結果から取得できる |
| 空・除外 | 旧collection不存在は該当なし。原本不存在・不正参照・読取不可・明示破棄を区別。読取失敗を空集合成功に変えない |
| 更新差分 | shadow開始後の追加・変更・削除・権限失効を最終差分として処理できる |

fixtureのPASS数と実データの品質評価は別に報告する。未解消failedがあるdomainは切替完了扱いにしない。
正当な除外は理由と対象数を記録するが、全文・認証情報・個人識別子をGitへ保存しない。

## 3. shadowから共有runtimeへ移す前の必須設計

現在のCLIのshadowはownerごとに分離され、liveは停止済みruntimeを前提にする。
この差を埋める昇格工程は、以下を満たす実装と回帰検証が必要である。

1. 新しい共有候補storeを、稼働storeと別に用意する。元の7 owner storeを変更しない。
2. 再利用するpointはcanonical ID、fingerprint、現原本のinput hashで照合する。
3. 同じIDの候補が複数ある場合、黙って最後のpayload/vectorを採用しない。不一致は現在の原本から再構築するか、明示的に失敗させる。
4. あるownerの権限失効・excludedは、別の合法readerのglobal pointを削除する根拠にしない。マスキング等の全reader共通除外は別に扱う。
5. 一人の部分的なDocs可読集合を共有storeのstale削除集合へ流さない。
6. runtimeのcanonical reconcileと保存済みvector再検証を通し、本番metadataの整合を確立する。shadow ledgerの単純連結は行わない。
7. 最終差分処理の失敗・取消・再開を試験する。二重writerと未完の候補への切替を防ぐ。

この工程が未実装なら、その事実を残す。安全に再利用できない候補を無理に昇格せず、停止時間を実測した正規live再構築案と比較して判断する。

## 4. 停止・バックアップ・切替

- 現在の稼働SHA、設定、Qdrant mode/path、関係するDB index metadataと停止対象プロセスを読み取りで確定する。
- 所有者不明のprocessをkillしない。通常の停止手順で全reader/writerが閉じたことを確認する。lockの削除や稼働ファイルのhot copyはしない。
- Python local modeはserver snapshot APIを使えない。停止したstoreと対応metadataを整合したcold backupとして保全する。
- backupのchecksumだけで復旧可能とはしない。隔離した復元先でcollection schema、ID集合、payload/vector、代表検索を検証する。
- 実際に必要な停止時間は、差分処理・候補準備・起動・smokeの実測から算出する。未測定の短い停止時間を約束しない。
- 正式な設定・起動経路で新storeと固定モデルを使用する。初回起動のcold loadも別に測る。
- 変更後は日本語意味検索、型番/IP/filenameの完全一致、画像/PDF、audio/video区間、UI、AI原本取得、権限外拒否を再確認する。
- 同時更新中の検索、検索キャンセル、worker異常終了・再起動、通知欠落の自動再照合も切替後の確認対象にする。

## 5. 戻す条件と方法

新runtimeの起動不能、index identity不一致、許可情報の欠落、権限外の露出、原本取得の不整合は切替中止条件。
性能は既存の実利用条件と比較し、native encode・ACL SQL・Qdrant・原本取得を分けて判断する。

戻す際は新writerを停止し、保全したコード・設定・索引・対応metadataの整合した組を復元する。
原本DB全体を古いbackupへ戻して切替後のユーザー入力を消してはならない。
切替後に原本更新がある場合の差分再照合が可能なことを、切替前に確認する。
復旧手順は一時的な障害対応であり、旧モデルを恒久的な選択肢として製品へ戻すものではない。

## 6. 旧方式の整理と完了判定

- 最終差分で、旧denseモデルのコード・設定・依存・選択肢・fallbackが残っていないか確認する。別役割のrerankerを名称だけで誤削除しない。
- 削除対象は41所有と確認できた旧collectionだけ。原本・他project共有資産・共有モデルcacheを含めない。
- 旧索引の整理は新索引の全coverage・権限・品質・復旧検証後に行う。必要な個別承認や削除境界は守る。
- 全domainについて、実装、検証、Git公開、実データ移行、本番切替、旧方式整理を別々に記録する。
- 最後に旧runtimeへの参照がなく、新規追加・更新が新方式だけへ反映されることを確認する。

## 7. 待機中に進められる作業

移行完走を待つ間に、非機密の受入チェックリスト、代表queryの分類、移行失敗理由の分類、停止対象の確認、復旧試験の準備、最終公開SHAのレビューを終える。
これらは追加の本番変更を許可するものではなく、完走後に準備をやり直さないための並行作業である。

## 8. 現コードから見た共有候補の構築案

この節の共有candidate構築は `src/rag/runtime_candidate.py` と既存CLIの `--mode candidate` に実装した。
隔離した合成回帰の対象であり、実データ共有candidate・本番昇格・正規live metadata確立は未実施である。
使い方と境界は [共有candidateの操作](embeddinggemma2_search.md#独立した共有candidate) を参照する。

`scripts/migrate_embeddinggemma2.py` の `--live` も一人のactorを入口にする。
`docs_library_ids(..., shadow=False)` はそのactorが所有するLibraryだけを列挙するため、
ownerがNULL・非activeでも別の合法readerが読めるLibraryを含む共有runtimeの構築工程にはならない。
複数actorのlive実行でもownerのないLibraryは列挙されない。
不足分にshadowのactor別集合を流用すると、同じLibraryの部分的な集合や実行順がglobalのstale削除へ影響する。
現CLIの引数を付け替えるだけの切替は採用しない。

正規Docs runtimeは `DocsIndexService.reconcile_library` がLibrary内の全non-archived原本からown-record入力を組み立てる。
actor別shadowの検索可能集合とは分母が異なる。候補構築はこの正規のwriter集合を明示し、
検索のcurrent ACLとrender guardは引き続き結果候補と原本取得に適用する。
Libraryを全体列挙して一度だけcanonical reconcileする工程が必要である。

### 比較する経路

| 経路 | 追加実装と再利用 | 停止時間の扱い |
| --- | --- | --- |
| 新しい共有候補への検証済みvector再利用 | 全Libraryの正規入力・canonical unit IDを再構築し、各shadow storeから現input hash・fingerprint・vectorが一致するdenseだけを再利用。payloadとsparseは現原本から再生成し、不足・不一致は同じ固定モデルで再埋め込みする | 候補の大部分を稼働storeと分離して準備できる。停止中にも最終差分、backup/restore確認、正規metadata確立、起動・smokeが必要。現時点で所要時間は未測定 |
| 正規liveで全再構築 | 同じ共有inventoryを使い、旧vectorを再利用せず全current unitを固定モデルで再埋め込み。candidateでshadow providerを省略すれば隔離再構築できるが、正規live metadata確立は別工程 | 初回encodeと全upsert・保存後検証が停止時間に入る構成になる。actor別shadowの時間をこの工程の実測値として扱わない。停止時間が必要なら隔離した正規writer rehearsalを先に測る |

再利用案ではfolder・ledgerを連結しない。新candidateの役割・generation・source DB・destination・fingerprintを
独立してbindingし、入力storeは各actorの既存bindingを読み取りで検証する。
同一canonical IDの複数候補は全候補を比較し、同じinput hashでもvectorが不一致なら現原本から再埋め込みする。
raw title/filenameも照合し、同一record hashでも実際のtitle prefixが違うdenseを再利用しない。
candidate payloadの所有者、Project、Library、page/timeは現原本のcanonical builderで生成し、古いpayloadを採用しない。
candidateの保存済みprefixは、保存後検証した現在入力とdenseのdigestをunit checkpointに結び付ける。
未検証prefixや後からdenseが変わったprefixは同じ固定モデルで再構築する。
可読sourceの原本欠落・不正path・整合性失敗はformal excludedにせずfailedとする。
coverage未完了のcandidateはREADYとせず、実行・statusともCLIを失敗終了させる。
このgateは未解決の実原本を修復したという証拠ではなく、実データcoverageの照合は別の実環境工程に残る。

### 最小の実装範囲

1. `src/rag/runtime_candidate.py` 等の明示的なoperator用builderと、CLIの独立したcandidate mode。
   registered sourceの共有inventory、現在入力の組立て、read-only shadow provider、新destinationと再開用manifestを担当する。
   一人のshadow scopeを解除して流用するAPIや、本番設定を自動更新する処理は設けない。
2. canonical writerに限定したverified-vector providerと保存後検証。
   Docsでは `_node_units` / `_build_point` を再利用し、768d・finite・L2・unit ID集合・input version・fingerprint・
   record input hash・現sparseを照合する。会話では `migrate_memory_unit` / `_memory_embedding_hash` / `has_verified_document`、
   mediaとKnowledgeでは現source/unit builderと現input hashの検証を使う。
   これは固定モデルの計算結果の再利用であり、別モデルへのfallbackではない。
3. 正規reconcileの検証gateとmetadata確立の分離。
   Docsのhash/ID一致skipにはboundedな保存dense/payload/sparse検証gateを追加済み。
   新規upsertも保存後のdense/ID/payload/sparse検証に失敗したら例外とし、durable queueを保持する。
   切替時のoutbox ACKは現在のrequested revisionを取得し、
   正規reconcileと保存後検証が成功した範囲だけを進める。shadow READYをACKへ変換しない。

### 追加する受入回帰

- 同じDocを複数actorが読む場合、canonical IDを一つだけ保存する。actorの列挙順を変えても結果が変わらない。
- 同一IDのinput hash・vector・payloadが不一致、768d以外、非finite、L2不正、fingerprint不一致を再利用しない。
- NULL/非active ownerの読めるDocs、joined participant、権限失効、foreign Inbox、privacy maskingを現在の原本で確認する。
  一人のexcludedが別readerのpoint削除にならないこと、privacy共通除外は全candidateから消えることを確認する。
- shadow取得後・encode中・upsert後の原本更新を差分として扱い、未検証batchや旧requested revisionをACKしない。
- 失敗・取消・再開でbindingと検証済みbatchを保ち、別generation/DB/destination、同時writer、未完成候補の採用を拒否する。
- 複数Libraryのunionと正規writer集合、全unit ID/hash、原本hash不変を比較する。新candidate以外を削除しない。
- 元の7 store、原本DB、旧storeに変更がないことを検証し、候補でcurrent ACL付き検索・AI原本取得を再試験する。

実データの所要時間・失敗件数・品質とlatencyはprivateな検証記録に保存し、合成fixtureの結果と区別する。
canonical reconcile、最終差分、復旧確認の実測がない間は、どちらの経路にも短い停止時間を約束しない。

candidateの `all` は登録済みDocs/Knowledge/会話/mediaを対象とする。現移行では旧genericFilesが存在しないため、
legacy generic files経路の追加は行わずcandidateの `files` 指定を拒否する。
共有runtimeへのmetadata確立・store採用APIは追加していない。actor別live CLIでcandidateを採用する操作も拒否する。
