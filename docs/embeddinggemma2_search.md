# EmbeddingGemma 2 共通検索基盤

文章、会話、Docs、Knowledge、コード添付、画像、PDF、音声、動画のdense encodingは `src/rag/embedding.py` の専用workerに集約する。固定モデルは `google/embeddinggemma-2`、revisionは `914f7f89142e33e77833254d9c9b90c3cef7303b`、768次元、L2正規化。モデル選択肢と旧モデルfallbackは設けない。Qdrant、Docs/Knowledgeの文字列・完全一致検索、識別子を保つhash sparseは継続する。Rerankerはdense encoderとは独立した既存機能である。

仕様の正本は [公式モデル](https://huggingface.co/google/embeddinggemma-2)。documentは `title: {title} | text: {content}`、text queryは `SearchQuery`、コードquery比較用には `CodeRetrieval` を使う。画像・音声・動画にtext prefixは付けない。BF16またはFP32を使用し、FP16は使わない。

## 経路と原本

| 原本 | dense経路 | 候補・取得時の権限/出典 |
| --- | --- | --- |
| ConversationMessage | CrossSessionMemory → 共通worker | 現SQLの所有者/joined参加者、character、Project、Space、現branch、削除状態。取得時も再確認し、現会話本文を読む |
| Docs node/長文span | DocsIndexService → 共通worker | 既存Docs SQL ACLとscopeを候補前・結果に適用、node/title/pageへの出典 |
| Knowledge chunk/GROWI本文 | KnowledgeIndexService → 共通worker | 現source権限とProject refs、既存原文/URL。GROWIのremote添付はlocal原本がないためnative media対象外 |
| 汎用RAG files | DocumentIndexer → 共通worker | 移行は旧collectionのsource_file metadataと現在認可されるKnowledgeSource rootに限定。原本不在、権限/登録不明、原本metadataのないmanual入力は明示除外 |
| generated media/chat/task/Docs/Knowledge添付 | MediaIndexService → 共通worker | SearchSourceResolverが現User、Project/Space、所有者/参加者/Docs/source ACLと保管namespaceを照合。元会話/message、原本URL、PDF page、timeを返す |
| 正式GenerationOutput成果物 | MediaIndexService → 共通worker | 既存MediaAssetServiceの認証preview readerでimmutable revision/SHA/owner/projectを検証。未登録remote outputは `generation_output_not_registered_as_managed_asset` として明示除外 |
| Meeting成功済みaudio | native対象外 | 既存契約の成功commit→staging audio削除を維持。理由 `meeting_audio_discarded_after_success`。既存ASR・議事録・memo Docs本文を検索する |
| 保持中Meeting audio、通常audio/video添付 | native media＋既存ASR/OCRテキスト | 現actor/Project/Spaceとimmutable upload hash/size/nameを確認。保持期限・明示cleanup後の不存在は理由付き除外 |

Task本体の意味検索は、既存の `search_task_candidates` tool内部で専用Task Qdrant派生索引と固定EmbeddingGemma 2 workerを使う。新しい検索lane/API/OpenAPI/DB schemaは追加せず、Task title/descriptionのsemantic順位と既存のlexical・ID一致候補を統合する。検索前に現在のreadable Project範囲をSQLで再同期し、削除・archive・purge済みpointをその範囲だけwithdrawする。検索後もfresh SQL ACLと現在のTask content hashを再確認し、権限失効や原文更新を結果として返さない。Task索引のpayloadへ本文を保存しない。

このTask本体経路は、既存の `task_attachment` を `MediaIndexService` で検索するmedia索引とは別である。Taskの `knowledge_node_id` がDocsへ結合している場合も、Docs nodeの既存索引更新とTask本体索引更新は別の派生pointとして扱う。semantic capabilityがlocalでない場合、またはEnterpriseで明示opt-inされていない場合は、従来のlexical Task検索契約を維持する。native worker、Qdrant、同期範囲、current ACLの失敗は空結果・重複なし成功へ変換しない。

画像/スクショはRGB、PDFは各page image＋抽出text。音声はmono16kHzの30秒以下の時間区間に分け、末尾までsample数を検証する。動画は32秒以下の区間を1fps、最大32frameで取り込み、音声trackも別audio区間として同じvector空間へ入れる。時間軸offsetを保持し、duration・frame/sample coverageが不一致ならreadyにしない。OCR/ASRは引用・lexical用に保持する。

AI tool `media_search` は自然言語または権限のある既存画像/PDF sourceのqueryを受け取り、`media_read(unit_id)` が原本のpage/time区間を再取得する。任意URL/pathは受け付けない。任意の `question` に対する解析は既存MediaRecognitionServiceとOutboundPrivacyGatewayを通す。原本の権限失効、削除、hash変更後は検索結果・取得を拒否する。原本は新索引が書き換えない。

既存 `/chat` のCtrl+F会話検索ダイアログに「画像・資料」tabを追加した。`ConversationSearchDialog` → `MediaSearchPanel` → `media-search-api.ts` → 既存Python proxy → `src/api/media_search_routes.py` → 同じMediaIndexService/SearchSourceResolverという経路で、別の検索DBや原本storeは作らない。自然言語検索、結果の「この画像で検索」、PDF page/audio-video time、資料確認、原本・元会話のリンクを利用できる。認証済み `POST /api/search/media`、`GET /api/search/media/progress`、`GET /api/search/media/{unit_id}`、`/{unit_id}/preview`、`/{unit_id}/content` はactive Userと現在Project/Spaceを確認する。contentはdescriptorを固定してSHAと現ACLを再検証し、Range対応、no-storeで配信する。Web/Mobileの共有OpenAPIと生成型にも反映する。

利用者向けprogressは全体ledger集計を流用せず、actorのqueue/再照合sourceを現在scopeのbulk ACLで絞り、安全な件数と失敗分類だけを返す。外国source key、原本path、生例外、共有workerのlast_errorは返さない。閉じる操作、query/scope変更、権限失効で古い結果・原本linkを残さない。

media候補ACLは全8種別ごとに重複sourceを除き、1000 ID以下のSQL batchで現DBのactor、source、Project、Space、linked rowを照合する。Project権限は既存の共通normalizer、Knowledgeは既存source grant、Docsは同libraryの祖先shareと特殊hub/identity規則を適用する。foreign Personal Docsの祖先はDB内で0〜512hopに限定し、全library本文を取得しない。永続ACL cacheは作らず、候補確認からは許可されたlocator keyだけを返す。

ready inventoryは最大100000 sourceで、超過は明示エラーとなる。media検索は最大100結果、dense/sparse prefetchと結果overfetchはそれぞれ最大400 unit。同一sourceの結果確認はrequest内で一度行い、原本取得は全inventory走査ではなく対象sourceの現ACLとready/fingerprint/hashを照合する。decode終了後にも権限とhashを取り直す。local MatchAnyはrequest専用のhash membershipを使い、ID listの線形照合を避ける。Qdrant、Knowledge、Docs、mediaのembedded I/Oは共通thread lockと実threadの完了drainで調整し、別event loopからの検索・更新にも対応する。

Docs/Knowledgeの個別原本readerは、source一件のfresh SQL ACLをpath取得前に追加し、既存point policyも維持する。長寿命SessionのDocs Library owner、identity Project、hub、祖先edgeの古いORM cacheで失効を隠さない。Spaceは既存policy直前に現rowをrefreshする。remote GROWIの権限は確認したうえで、local原本がないという明示除外理由を保持する。

Docs添付・会議音声の検索readerは既存storage rootだけを使い、rootが欠落していても作成しない。ClipIngestStorage/MeetingAudioStorageのwriter既定値は維持する。欠落・不正・workspace外・link/reparseの原本pathを拒否する規則を緩めず、原本の移動やDB内pathの修正をshadow検証で行わない。

## 専用workerの準備

hostのgTTS/Irodori依存を維持するため、ST 6.1 / Transformers 5.19は専用venvへ通常のpip resolverで導入する。workerはprivate stdin/stdoutで常駐し、公開portを作らない。request ID、identity、timeout、取消drain、異常終了後の再起動を持ち、認可済みのmedia bytesだけを受け取る。

```powershell
venv\Scripts\python.exe scripts\setup_embeddinggemma2_worker.py --cuda --host-check
# 共有保存先と既存snapshot・空き容量を確認してから、明示許可されたDLを行う
venv\Scripts\python.exe scripts\setup_embeddinggemma2_worker.py --verify-only --download --cache-dir F:\AI\models\Cold_F\llm\huggingface
venv\Scripts\python.exe scripts\setup_embeddinggemma2_worker.py --verify-only --load-check --cache-dir F:\AI\models\Cold_F\llm\huggingface
```

既定workerは `cache/embeddinggemma2/runtime/venv`。別配置は `AOITALK_EMBEDDING_WORKER_PYTHON`、共有model cacheは `AOITALK_EMBEDDING_CACHE` で指定する。モデル読込は既定local-only。workerのST/Transformers/PyTorch/vision/audio/AVはrequirementsで固定する。PyAV 19はtorchvision 0.25の `metadata_errors` 引数と互換性がないため、実video検証済み18.1.0を使う（[PyAV公式変更履歴](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.0)）。hostの既存pip check失敗をworkerの成功で隠さない。

## 本番既存経路の確定事項

今回追加した3ケース（通常アプリ作成、本番EG2保存pointの768次元・finite・L2・現行fingerprint/input hash、semantic検索から既存UIの表示・原本read・reloadまで）はPASSした。mainのdeviceはStrata/KawaiのGPU占有と共存する本番配置に合わせてCPUへ変更しただけで、model、revision、fingerprint、collectionの契約は変えない。

KnowledgeをOFFにする場合も、停止対象はKnowledge専用kindだけとする。native mediaの通常run/reconcileは継続し、新codec `c4fdfe969` をその経路で使用する。

## 再開可能な移行と切替

collectionは `__eg2_{fingerprint先頭16字}` を付ける。fingerprintはモデルrevision、768d、公式pooling、prefix、正規化、precision方針、media sampling/chunk条件を含む。別仕様・旧1024dとはcollectionを共用しない。汎用filesは `__files` を加えてKnowledgeのnamed dense/sparseと分ける。

```powershell
# 元DBをread-onlyにし、世代ごとの隔離index/checkpointで評価する
venv\Scripts\python.exe scripts\migrate_embeddinggemma2.py --mode shadow --generation review01 --user-id <認証済みUUID> --domain all --local-path .local\eg2-review01-qdrant --manifest .local\eg2-review01-migration.sqlite --media-manifest .local\eg2-review01-media.sqlite
venv\Scripts\python.exe scripts\migrate_embeddinggemma2.py --mode shadow --generation review01 --status --manifest .local\eg2-review01-migration.sqlite
```

既定はshadow modeで、generationと分離したlocal destinationを必須にする。元DBのschema初期化・migration・Config既定値/encryption writebackを行わず、既存RAG設定だけをprojectionで読む。PostgreSQL transactionはREAD ONLY、ORM flush/commitも拒否し、終了時rollbackする。Knowledge vector_id、Docs applied_revision/index_state/outboxなどのlive metadataは移行進捗に使わない。公式input/point builderを再利用して分離先に保存し、generation/actor/source DB/destination/fingerprintにbindingしたSQLiteでbatch進捗を記録する。再実行は同じbindingを必須とし、runtime ledgerの転用や旧storeと同じ・親子destinationを拒否する。statusのruntime queue照会もread-onlyで、queueを作成・upgradeしない。

全ownerのshadow評価では、ownerごとに別のdestinationとmanifestを指定する。progress manifestだけでなく、destination内の `.aoitalk-shadow-binding.sqlite` もgeneration/actor/source DB/destination/fingerprintにatomicに結び付ける。別manifest・別generationを指定しても、他ownerの同じstoreをQdrant/encoder起動前に拒否する。未結合の非空storeは自動採用せず、新しい空destinationを要求する。Docsの共有Libraryでは読めるnodeがownerごとに異なるため、この境界がないと後のactorのstale cleanupで先のactorのpointを削除し得る。Knowledge/Docs adapterもdestinationとactorを照合し、同actorの後続処理を狭いProject/Space scopeへ変更する再利用を拒否する。runtimeのcollection名とembedding fingerprintは変えない。

shadowのDocs対象は、空Libraryを含む所有Libraryと、canonical ACL/renderguardで現actorが読めるnodeを持つ共有Libraryである。Libraryを1000件以下のkeysetで列挙し、foreign Libraryは可読nodeの存在だけを本文取得・encoder起動前に照会する。NULL/inactive ownerでも正式共有の可読nodeがあれば対象にする。live modeの所有Library限定は維持するため、owner別shadowを単一runtimeへ昇格する手順は別途レビューが必要であり、shadow storeの連結やbinding解除で代用しない。

### 独立した共有candidate

`r01` candidateは、通常の本番既存EG2経路や通常の新規取り込みに必須のgateではなく、全candidate転送を行う場合だけ使う。全転送時はfresh proof、freeze、gap approvalをそろえる。復旧待ちのfailed 259業務原本と正常に残るdataは運用上分離し、READYを採用証明として偽装・流用しない。

`--mode candidate` は本番採用前のoperator用builder。既存DB設定をread-only projectionで読み、
新しいlocal Qdrantと一つの独立manifestだけを更新する。設定変更、service操作、live metadataのACKは行わない。

```powershell
venv\Scripts\python.exe scripts\migrate_embeddinggemma2.py --mode candidate --generation shared-review01 --domain all --local-path .local\eg2-shared-candidate --manifest .local\eg2-shared-candidate.sqlite --shadow-manifest .local\owner01-migration.sqlite --shadow-manifest .local\owner01-media.sqlite
venv\Scripts\python.exe scripts\migrate_embeddinggemma2.py --mode candidate --generation shared-review01 --status --manifest .local\eg2-shared-candidate.sqlite
```

`--shadow-manifest` はownerごとのmigration/media manifestを必要な数だけ追加する。省略すれば全current入力を
同じ固定モデルで再構築する。`--user-id` と `--media-manifest` はcandidateでは指定しない。
`all` は登録済みDocs・Knowledge・会話・mediaを構築する。現移行の旧genericFilesなしという前提に合わせ、
legacy generic filesはcandidate inventoryへ含めず、`--domain files` / `--files-collection` は明示的に拒否する。
legacy generic filesが再発見された場合は、共有candidateへ追加する前に別途設計レビューが必要になる。

Docsは全Libraryのnon-archived原本を一度ずつ正規own-record builderへ渡す。NULL/inactive ownerや
actorの部分共有・失効はこのwriter集合を狭めない。検索時の現SQL ACL/render guardは既存経路を使用する。
Knowledgeも正規source/chunk builderを使用する。会話・mediaは全active Userの現在可読集合のunionを用い、
一人の失効を全reader共通の除外と扱わない。privacy_masking.source=true、現branch外、削除、
全合法readerの不存在はcandidate側の派生pointを取り下げる。原入力・添付をencoderへ送らない。
privacy、canonical削除、ACL失効、契約上のnative対象外は理由別のexcludedにする。
可読sourceの原本欠落・不正path・取得時の整合性失敗はfailedとしてcoverageを保留する。
failedが残る実行とstatusは `candidate_ready=false` を返し、CLIは終了コード1にする。
原本を修復した後の再実行で現在入力を検証できるまでREADYにしない。

入力shadowのactor/generation/source DB/destination/fingerprint bindingを読み取りで確認し、
停止済みlocal storeの通常Qdrant lockを保持して必要なpointだけをread-only SQLiteから取得する。
稼働中storeのlock回避、hot copy、folder/ledger連結は行わない。Docsのcheckpointとrecord/span ID集合、
各domainのcanonical ID・現input/content/lexical hash・fingerprint・768d・finite・L2を照合する。
raw title/filenameも完全一致を要求する。Docsのrecord hashが同じでも、encoderのtitle prefixが異なれば再encodeする。
同じIDのshadow候補が不一致なら列挙順に依存せず固定モデルで再encodeする。
payloadとsparseは再利用時にも必ず現在の原本から作り直す。

各batchで原本をencode前・保存前・保存後に再照合し、保存pointのID集合、dense、payload、sparseも検証する。
Docs/Knowledgeは1000原本以下のSQL pageと32unit以下のencode/retrieveで進め、全sourceのunit集合を再照合してから
candidate内のstaleを削除する。失敗・取消ではcandidate READYを残さない。
保存後検証と原本再照合が成功したunitだけ、現在のモデル入力と保存denseのdigestを独立manifestへ記録する。
再開時はこのcheckpoint・現在入力・実際の保存denseの一致を要求し、未checkpoint・digest不一致のprefixは再encodeする。
digestはcandidate内の検証記録であり、runtime/shadowのhash体系やモデルidentityは変更しない。
candidateの役割/generation/source DB/destination/manifest/fingerprint/domain/入力shadow集合を独立bindingし、
同時writerや既存非candidate store、actor shadow/live modeによる採用を拒否する。
READYはその実行でのsource別検証結果であり、live applied_revision/outbox ACKや本番採用の証明ではない。
最終差分と正規metadata確立、本番切替は親の別レビュー工程である。

`tests/test_embeddinggemma_runtime_candidate.py` は使い捨てPostgreSQL/local Qdrantと合成encoderで、
7shadowの順序独立・不変、重複不一致、unit欠落、owner不在/無効、共有失効、変更/削除、privacy本文・添付除外、
取消/再開、保存ACK後・checkpoint後のdense破損、raw title境界、可読原本欠落と修復、
現ACL検索・原本取得、CLI all/coverage失敗終了、原本DB/旧store不変を検証する。
実データcoverage・品質・性能・本番切替の成功を示す試験ではない。

会話unitもupsert ACKだけでreadyにしない。新規・再利用の両方で保存vectorのdimension/finite/L2/fingerprint/input hashを検証し、元会話の現ACL・本文・character・Projectから取り直したinput hashと一致した場合にだけreadyにする。encode中の本文更新・branch失効・actor失効はreadyにせず、再実行の対象にする。

mediaも原本の現ACL/SHAを取り直した後、保存pointを64件以下ずつ最終検証する。ID集合、768d・finite・L2、fingerprint、原本hash、lexical hash、source keyが一致しないpointはreadyにせず、同じgenerationの次回実行で派生pointを修復する。全vectorを一度にメモリへ読み込まない。

`--local-path` だけでは元DB書込みを隔離できない。live metadataを更新する経路は `--mode live --confirm-runtime-stopped` とし、全index writerを停止済みとする運用確認を明示的に要求する。このCLIはservice停止・切替を実行しない。shadowからliveへの昇格は切替レビュー後の別操作である。旧local Qdrantが現runtimeによりexclusive lock中ならfiles inventoryは `legacy_source_metadata_unavailable` で失敗を残す。hotなstorage file copyやlock回避は行わず、通常の管理手順でreaderを閉じるか、認可されたmetadata提供経路を準備する必要がある。

### 既存本番Docsの再索引入口

`venv\Scripts\python.exe scripts\reindex_docs_qdrant.py` は、既存schemaを前提に保存済みglobal RAG projectionをread-onlyで復号・検証してからendpointとmodel identityを確定する。`get_rag_config()` の初回fallback、schema migration、app-configのdefault/encryption writebackは行わず、`--device` と `--local-path` の既存override semanticsは維持する。原本DBはread-onlyで参照し、変更対象はcanonical Docsの派生point reconcileだけで、queue ACKやlive metadataは更新しない。初期化・reconcileの失敗は成功扱いにせず、DB session、Qdrant client、embedding workerを終了処理してfail-closedにする。

移行は現actorが読める原本だけを対象にする。所有者ごとのinventoryと除外理由を照合する。SQLite ledgerはrunning/ready/interrupted/failed、完了unit数、hash、identity、actor別除外理由を保存する。各batch/unitをupsertし、再実行はhash/fingerprint/有効なvectorが一致する完了分を再利用する。Knowledgeは `min(indexing batch, embedding batch,32)`、mediaは1page/30秒/32frame以下。取消時は実行中のIPC/I/O/decoderをdrainしてからlockを解放する。staleな新方式の派生unitだけを、全unit検証後に置換する。

新規書込みはcommit後のdurable queueへ通知する。DocsのNext.js添付は既存DB trigger→DocsIndexWorker経由、生成物・task/chat添付・Knowledgeは各既存書込経路から通知する。queueは世代、所有token、lease更新、retryを持ち、未完了unitを再開する。

正式素材登録はSessionのflush/commitを監視し、Operations writerを変更せずキューへ通知する。取得はdescriptorを固定する正式materialize readerで検証済みbytesを `.local/eg2-source-*` の一時snapshotへコピーし、その寿命内だけ解析する。原本pathnameの再openによる差替え競合を避ける。SAVEPOINT commitは外側commitまで保留し、rollbackは該当transactionの通知だけを破棄する。

PostgreSQL commitとSQLite enqueue間の通知欠落は、独立MediaIndexWorker再照合taskで自動回復する。会話messageを含む8種の登録済みsource metadataだけをkeysetで100行以下ずつ、2秒間隔で巡回し、全文・原本bytes・任意diskは走査しない。同じSQLite queueにmetadata hash、actor、kind別cursor/cycle、進捗、安全な失敗分類を保存し、変更jobとcursorを一つのtransactionでcommitする。途中停止/書込失敗なら同じpageから再開、DB失敗は30秒後に再試行する。完了したkindは60秒後に新しいcycleを開始し、source削除/attachment減少/owner移管も検知する。変更のないsourceを毎cycle再encodeしない。長いaudio/video索引job中も独立taskが巡回し続ける。更新の到達時間はsource総数と巡回速度に依存する。完全DB outboxとはせず、CLI inventory再実行だけに頼らない。

`--domain all` はfiles/media/knowledge/docs/memoryを実行する。filesは旧generic collectionのsource_file metadataだけを200point以下ずつ読み、現在認可されたKnowledgeSource root/policyに含まれる原本をdescriptor固定の一時snapshotから再抽出する。任意directoryを探索しない。旧custom collectionは `--files-collection <正確な名前>` を追加指定できる。新規768d/identity/hash/vectorを保存・再検証してからreadyとし、再実行は検証済みchunkを再利用する。原本不在・権限不明・旧indexのみのmanual入力はexcluded、抽出/encode/保存失敗はfailedとする。旧vector/textをfallbackや再索引原文に使わず、旧collectionを消さない。

本番切替は次を親レビューで確認してから行う。

1. 新索引に対する全owner/source inventory、失敗・除外理由、元の現行検索とのRecall/順位/識別子比較。
2. 現user/Space/Projectの候補・結果・AI原本取得に対する権限失効/削除検証。
3. 長音声/動画・PDF全pageのcoverage、原本hash不変、取得page/time/会話の対応。
4. cold/warm、Qdrant＋lexical、原本取得、同時索引更新、実利用件数/ACLでのp50/p95とmemory/disk。
5. service切替の承認と運用時刻。切替を未検証のまま実施しない。

旧dense実装（BGE-M3/MiniLM）のimport・model選択・fallbackとFlagEmbedding依存は0で、未使用model filesも撤去済み。独立したrerankerとlexical/hash sparseは維持する。旧collectionの物理削除は実施しない。切替検証後に、正確なcollection名、所有project、point数、原本との独立性、backup/restore手順を確認し、個別承認を得てから対象のみ削除する。他project共有資産・原本・共有model cacheは削除しない。旧方式への恒久runtime fallbackはない。

削除レビューには旧collectionの所有範囲、vector schema/identity、point数、現inventoryとの対応、全sourceの新768d coverage、failed/excluded理由、現ACLと日本語品質の比較、rollbackに必要な旧metadataを揃える。serverでは[公式snapshot作成API](https://api.qdrant.tech/api-reference/snapshots/create-snapshot)でbackupを取得し、checksumを保存する。[公式upload復元API](https://api.qdrant.tech/api-reference/snapshots/recover-from-uploaded-snapshot)は既存targetを上書きし得るため、復元検証は新しい隔離collectionに限定し、point ID・payload・vector・件数を比較する。

Python local modeのsnapshot APIは[公式QdrantClient 1.16.2の実装](https://raw.githubusercontent.com/qdrant/qdrant-client/v1.16.2/qdrant_client/local/qdrant_local.py)で未対応。実環境のlocal storeにはserver snapshot手順を流用しない。全reader/writerを通常の停止手順で閉じ、quiescentなstorageと対応metadataをcold backupする運用手順をレビューし、checksum照合と分離cloneからの読込・検索・復旧を検証した後にだけ個別削除を検討する。本作業では本番停止・copy・restore・削除を実行していない。

`scripts/verify_qdrant_snapshot_restore.py` は、production endpointへfallbackしない固定loopback allowlist（source `56333`/`56337`、target `56335`/`56339`/`56341`）を使う。既定は別serverのcross-server restoreで、同一server fixtureは `--same-server` を明示した場合だけ許可する。repo-local ext4 image上のQdrant 1.16.3 source `56337`→target `56339`で、64件の合成1024d pointについてsnapshot download/SHA256、new collectionへのunmodified upload、schema/ID/payload/vector/count/代表検索の一致を実測しPASSした。証跡は `.local/search-memory-snapshot-check/qdrant-synthetic-ext4-final.json` / `.md` に保存している。

既存のconversation memory snapshotはreceipt SHA256（`7cbd0750...ca22f0`）を照合し、archiveを変更せずに隔離targetへrestoreした。source `56339`（Qdrant 1.16.3）→target `56341`（1.17.1）とtarget 1.17.1単体の両方で、`0/wal/first-index` が17-byte all-zeroのため `qdrant_snapshot_wal_error` になりFAILした。証跡は `.local/search-memory-snapshot-check/qdrant-business-ext4-final.json`、`qdrant-business-1163-to-171.json`、`qdrant-business-171-same-server.json` に分けて保存している。snapshot生成元の破損を補修・捏造せず、production endpoint `16333`、原本259件、既存collectionには触れていない。

current EmbeddingGemma 2のCPU worker（`cache/embeddinggemma2/runtime/venv`、cache `F:\\AI\\models\\Cold_F\\llm\\huggingface`、fingerprint `5fdc0b596b0b9ec7`、768d）から合成text 3点を実際にencodeし、専用ext4 source `56337` でseedした。unmodified snapshotをext4 target `56339`へrestoreし、schema/IDs/payloads/vectors/count/代表検索が一致するPASSを `.local/search-memory-snapshot-check/qdrant-native-eg2-ext4-final.json` に保存した。このPASSは現行EmbeddingGemma identityとQdrant server snapshot経路の証明であり、legacy business snapshotの原本復旧を意味しない。

## 2026-10-07の検証範囲

公式固定revisionをCUDA/BF16で実行した小規模synthetic corpusは9source/11vector、13query×3回。日本語、コード、型番/IP/filename/識別子、text→画像/PDF/audio/video、image→imageのRecall@3/MRRはいずれも1.0。30秒超音声の末尾と32秒超動画の末尾、PDF page1、元ファイルの取得も成功した。コードqueryはSearchQuery/CodeRetrievalともrank1で、既定SearchQueryを維持する。

| 実測 | p50 | p95 |
| --- | ---: | ---: |
| warm encode | 88.9ms | 162.1ms |
| Qdrant＋literal | 1.47ms | 1.95ms |
| 原本unit取得 | 0.67ms | 262.9ms |
| 同時50回索引更新下encode | 180.1ms | 249.4ms |

cold load 16.95秒、初回encode 7.78秒。worker peak RSS 4.93GB、CUDA peak 2.77GB。隔離workerの通常resolver install、pip check、ST/processor/vision importは成功。Gateway hostでは既存 `descript-audiotools` / protobuf conflictが残っている。

実環境のread-only metadata調査ではlocal Qdrantの旧1024dが145,857点と65点。原本/payloadは取得していない。小fixtureは本番semantic品質・14.6万点のSQL ACL latency・実データ切替を証明しない。追加capacityの反復native vector索引は容量評価に限る。Knowledgeのcanonical DB rows/既存source snapshotは全source収集のため、host memoryの完全stream化は未実装。実データのread-only shadowは別のprivate派生保存先で段階検証する。本番切替・restart・旧索引削除は未実施で、完了扱いしない。

追加Python local capacityは145,922点でPASS。upsert 890.84秒（163.8点/秒）、dense＋sparse＋actor filterの30検索はp50 3.114秒/p95 3.392秒、host RSS 1.49GB、index disk 1.21GB。これは本番metadata調査で確認した `QdrantClient(path=...)` と同じ実行方式で、HNSW server性能ではない。local client内部はmemory上の全vectorに対するflat/NumPy検索で、アプリが14.6万vectorを取得してPythonで再rankする経路は設けていない。反復native vectorとsynthetic payloadの容量試験であり、実データ意味品質やSQL候補ACL処理時間を含まない。

同Qdrantの独立server実行方式との比較は、127.0.0.1:56333の使い捨てDocker Qdrant 1.16.3で行った。本番のlocal設定・index・serviceを置換していない。random seed固定のsynthetic 768d unit vector 145,922点、14,593 source、7,296 allowed source。通常HNSW、keyword payload indexあり、量子化なし。upsert 73.11秒（1996点/秒）、HNSW build 89.64秒。named dense＋sparseのindexed vector countは291,844。各30検索で、結果top10、RRF prefetchは各40に限定した。

| 独立serverのHTTP query（encode/SQL/原本取得を除く） | p50 | p95 |
| --- | ---: | ---: |
| dense、filterなし | 5.03ms | 8.58ms |
| dense、actor filter | 4.42ms | 5.86ms |
| dense、allowed source ID＋identity filter | 13.27ms | 16.30ms |
| sparse、filterなし | 1.55ms | 2.34ms |
| sparse、actor filter | 1.64ms | 2.48ms |
| sparse、allowed source ID＋identity filter | 6.34ms | 8.02ms |
| RRF、allowed source ID＋identity filter | 22.13ms | 28.58ms |

server試験のclient RSSは185.9MB、server allocatorのresident metricは182.1MB（process peak RSSではない）、storageは601.8MB。localとserverではvectorの内容も異なるため、単純な速度比や本番の推定値にしない。

実PostgreSQL 16のsynthetic rowsで、同じ候補ACL codeだけを分離した測定も行った。media 14,593 source（7,296 allowed）の旧逐次resolverは36,482 SQL、3観測でp50 16.96秒/p95 19.20秒。bulk版は16 SQL、30観測でp50 470.2ms/p95 512.1ms。同8種別100→500候補の回帰は17→17 SQL、foreign Personal Docsの500 library/祖先shareは5→5 SQL。1001 sourceと重複を渡す場合もactor1＋source2batchの3 SQL、INは1000以下だった。実DB測定はPersonal generated mediaで、Space・複雑なDocs ancestryのlatencyは含まない。それらのpolicyは別SQLite実SQL回帰で確認する。

DocsIndexと同じDocsGraphServiceの候補relationは、145,922 node（Personal/許可Project/拒否Project各約1/3）から97,282 eligible IDを返した。30観測で1〜2 SQL、p50 2.957秒/p95 5.054秒。ProjectのJSON permissionを含むSQLとUUID列の取得時間であり、dense/sparse検索・worker encodeを含まない。このSQL時間と本番local Qdrant時間は切替レビューの性能課題として残る。この測定は後述のSpace境界追加前のrelationであり、現在の厳密な検索ACLの性能保証には使わない。

このDocs測定の初期観測は独立Qdrant serverのHNSW構築と時間が重なり、CPU競合の影響を含む可能性がある。単独実行時の性能と同一視しない。

会話検索は全許可message IDの256件分割を廃止し、正規SQL ACLで得た全許可IDへのvector queryを1回行う。current sessionは候補SQLから除外する。返す候補は最大400、通常top5では20件に限定し、同じ正規SQL ACLと新しいDB Sessionで本文を一括取得する。joined参加者の退会、User失効、Project/Space失効、削除、非active branchは現DBで拒否し、index payload本文は結果に使わない。local MatchAnyはhash membershipで照合する。

`scripts/evaluate_conversation_search_capacity.py` の実PostgreSQL＋Qdrant local/in-memory fixtureは許可100/500/145922 messageを各30回測定し、全規模でSQL4回/vector1回/結果5件/原本候補20件だった。p95は8.07ms/18.40ms/3.562秒（14.6万件p50 3.006秒）。vector/queryは合成でnative encode、disk永続化負荷、production意味品質は含まない。約570回の逐次vector queryは解消したが、大規模local modeの待ち時間は切替時の性能課題に残す。canonical SQL/SQLite queueで通知欠落・restart・enqueue失敗・削除・owner移管・長い索引job中の回復・readonly status 13件、files原本/保存Qdrant/再利用/除外/lock拒否/元store不在4件、会話bulk freshness/順位/他user/退会/branch/失効grant/規模増大8件もPASS。

既存 `/chat` UIは実装担当とは別のAIが実Chromiumで1440px・390pxを確認した。隔離PGの合成Userとsource API response fixtureを使い、自然言語検索、意味検索準備中の文字列検索表示、画像query、PDF page、audio time、資料確認、認証原本link、元会話link、権限失効404と再試行、遅い旧応答の取消、隣接会話tab、閉じて再開、横overflowなしを確認した。両幅で13項目、console/page/requestの予期しないerrorは0。実データの本番UI測定ではない。REST対象26件、React対象14件、frontend lint/typecheck、production build、共有Mobile Product Contractの13capability/33route照合もPASS。

readonly shadow単独回帰は実SQLite/PostgreSQL＋保存Qdrantで28件PASS、canonical Docs ACLとencode中外部更新を扱うPostgreSQL専用ケースのSQLite側5件は意図的skip。Knowledge/Docsの第2batch失敗・取消→保存済みprefix再利用、元vector_id/Docs revision/旧1024d point ID・payload・vector保全、raw DML/ORM flush/commit拒否、default commandの初期化・Config書込み禁止、正本AADで暗号化した既存RAG設定の読取とDB値不変、会話upsert ACK後の保存hash破損・encode中の本文/branch/actor変更拒否を確認した。encoderは合成768d fixtureで、モデル品質の証拠は別のnative評価に限定する。

owner分離追加前の関連17 test file一括回帰は288件PASS、上記SQLite専用5件SKIP、20.69秒。shadow/REST/再照合/files/会話ACLとscope/正式素材/原本race/Meeting/全種別bulk ACL/media/security/embedded concurrency/Knowledge contextとlifecycleを含む293ケースであり、重複する過去runのPASS数を合算していない。

owner分離の追加回帰では、隔離PostgreSQLの実際のDocs共有SQLでactor Aが5node、actor Bが2nodeを読めることを確認した。修正前はBの別generation/manifestで同destinationを使うとAの5pointが2pointへ減った。修正後はBを索引処理前に拒否し、Aのpoint ID・payload・vector全5件を保全する。全5domainのCLI入口、Knowledge/Docs直接adapter、同actorの狭いProject/Space、別owner/generation/source DB、2owner同時claim、既存未結合storeと空・欠落table/rowのmarkerを検証した。owner/shadow/filesの3file一括回帰は68件PASS、6件のSQLite限定SKIP、11.51秒。PostgreSQL側の対応ケースはPASS。encoderは合成768dであり、モデル品質評価とは別の結果である。

共有Docsの対象列挙を含むshadow4file回帰は73件PASS、11件のSQLite限定SKIP、13.27秒。PostgreSQLでは5件の追加ケース（正式共有、NULL/inactive owner、空Library、不可読Libraryでencoderを起動しないこと、失効後の候補除外と原DB metadata不変）を確認した。media/security/RESTの関連3file回帰は54件PASS、5.66秒。保存後のzero vector・原本hash・lexical hash・source key変更はreadyにせず、原本を変えずに再開修復できる。これらの合成回帰を実データのsemantic品質証明には使わない。

原本storageのreadonly化はSQLite ACLと実ファイルを含む関連4fileで137件PASS、0SKIP、9.27秒。明示/env/relative root欠落時のno-mkdir、file/link拒否、writerのstage/promote、既存日本語filenameとunder-root legacy absolute path、Docs original読取、meeting原音の保持契約を確認した。不正・traversal・workspace外の原本を採用して成功扱いにしない。

実常駐workerの33件batch、強制終了からの復旧12.92秒、取消drain131.7ms、timeoutからの再起動もPASS。warm通常latencyにcold load/異常復旧時間を混ぜない。初回単体・回帰363件PASS（cached workerによるDocs専用評価1件はopt-in未実施）。追加bulk ACL49件、embedded concurrency11件、result重複・原本非corpus走査/上限2件もPASS。bulk接続・現権限・原本raceの関連最終回帰は140件PASS。実PostgreSQL検索/ACL/索引queue148件PASS、最新Docs I/Oの再検証72件PASS。SQLite側の意図的skip8件、無関係な既存task設定table不足のfixture1件は除外しており、その初回失敗を成功に数えない。

再現: `scripts/evaluate_embeddinggemma2.py` と `scripts/verify_embeddinggemma2_worker.py`。後者は実workerの33件batch、強制終了、取消、timeout、復旧と、指定件数の独立Python local Qdrant capacityを検証する。`scripts/evaluate_qdrant_capacity.py` は127.0.0.1:56333の独立server、`scripts/evaluate_search_acl_capacity.py` は専用DB `aoitalk_test_*`、127.0.0.1:55432だけを使用する。random fixture collection/schemaはfinallyで片付ける。検証JSONは `artifacts/verification/embeddinggemma2-*.json` に保存する。


## Docs検索の現在Project・Space境界

候補SQL、shadow原本投影、保存済みprefixの再利用、worker encode直前・直後、ready前、結果の再確認には、同じ現在Project/Space readフィルタを適用する。Spaceは共有SpaceAccessPolicyと同じ境界で、InboxはSpace ownerだけ、通常Spaceはowner/exact admin/同Spaceのlive Project owner-or-read-memberを許可する。不存在Space、失効したProject、厳密decodeでreadがboolean trueにならないmembershipは拒否する。Projectのlegacy大文字ADMINとSpaceのexact adminを混同しない。point原本readerは現在Spaceをpopulate_existingで刷新する。

Docs編集用のowner-private stale identity整理例外は維持するが、dense検索・shadow候補へ流さない。Libraryの以前のreadyは現在ACLの証拠にならず、再開時は同じrelationと本文hashを再確認する。現在不可読になったforeign Libraryはready集合からexcludedへ移す。owner Library内の失効pointは、現在の許可集合と原本hashを検証してから、そのowner専用shadow保存先の派生point/unitsだけを除去する。原本・他owner・別generation・旧runtime storeは対象外である。

追加の実PostgreSQL CTE回帰では通常Space/Inbox/不存在Space、大小文字admin、同Spaceの別Project membership、strict permissions、Space slug変更、owner cleanup、候補LIMITとpoint readの一致を検証する。実Qdrantの追加回帰では、以前readyだったforeign Libraryの失効、保存済みpointの再開除外、encoder実行中のbody/share/actor変更をupsert前に拒否することを検証する。合成encoderを使うため、これらをモデルの日本語品質証明としない。実データ結果・所有者ID・本文・queryはprivate scratchだけに保持し、repositoryへ含めない。

現在の厳密なDocs検索ACLを145,922 synthetic nodeで再測定した。検証engineを本体 `DatabaseManager` と同じ `jit=off` に合わせ、97,282 eligible ID、30観測、1〜2 SQL、p50 710.0ms/p95 872.7msだった。以前のcapacity engineはJITが有効で、巨大なACL式の反復コンパイル時間を含んでいたため、その値を本体SQL latencyとして扱わない。`--phase docs` でDocs候補だけを再測定できる。fixtureのProjectはSpace未所属で、現在のSpace SQLも通るが、Space membershipやInboxが多数ある場合の性能は未測定。native encode、Qdrant、fresh結果/原本取得を除く測定であり、Qdrant localの大規模検索時間は別の性能課題として残る。

## 共有会話とマスキング対象の索引境界

会話のruntime・移行候補は現在のownerまたはjoined user participantを共通SQLで確認する。索引時も現在のactive User、Project/Space、branch、削除状態を原本readerで再確認し、encode直後と保存vector検証後にも本文hashを照合する。payloadとinput hashのauthorは正本ConversationSession.user_idに固定し、索引担当が参加者へ替わっても同じglobal pointを再生成しない。過去のraw contentを受け取る索引入口は撤去し、Manager、worker、CLIは同じcurrent原本writerを使う。

公式server marker `privacy_masking.source` がJSON boolean trueのmessageは、本文・添付の候補、結果、AI原本取得から除外する。文字列trueや数値1は同markerへcoerceしない。既に新方式の派生pointがあれば、原本文を取得せず現metadataだけを照合し、対象pointのdeleteとcount zeroを検証してからexcludedにする。元message・添付・旧indexは変更しない。shadowでは以前READYのmessageもactor/generation-bound manifest内で1000件以下のfresh ACL batchにより再確認し、失効時にexcludedへ変更する。privacy以外のactor失効を理由にglobal pointを削除せず、他の合法reader用の派生pointを保全する。

会話metadataの再照合はownerを優先し、現在activeで合法なjoined参加者へ索引担当を引き継ぐ。参加者やProject/Space権限の変更をversionへ反映する。会話は専用memory collectionへdispatchし、media collectionに混入しない。会話searchの設定はKnowledge searchから独立し、無効なkindはclaimしない。ACL SQL回数は100行以下のpageとdistinct候補actor数に依存し、参加者fanoutが大きい場合の負荷は別途評価が必要。

追加範囲は重複除外で244件PASS、6件のSQLite専用skip。実PostgreSQL/SQLite/Qdrantで、inactive・missing・Project失効ownerと合法参加者、Inbox拒否、真bool marker、旧派生とREADYの撤去、encode中marker変更、actor失効後のREADY除外と他reader用point保全、100/500件のmetadata page、失敗・取消・restart・verified再利用、専用collectionを確認した。encoderは合成fixtureでありnative意味品質の証明ではない。

## User Files / Project Filesの意味検索（`workspace_file`）

`workspaces/_users/user_<uuid>` と `workspaces/_projects/project_<uuid>` の通常ファイルを、上記media索引の source kind `workspace_file` として扱う。埋め込み・検証・ledger・ACL・原本取得は既存の `MediaIndexService` / `SearchSourceResolver` をそのまま使い、別の検索DBや原本storeは作らない。追加storage root（NAS等）、`.trash`、symlink/junctionは対象外。

- 原本行は `workspace_file_entries`（alembic `20261012_0001`）。行IDが source id で、`src/services/workspace_file_index.py` のスキャナが (size, mtime_ns) で変更を検出し、変更時だけsha256を再計算する。消滅行と新規行のhash一致（24時間以内）で移動・改名を検出し同じIDを保つため、ベクトルは再利用されpayloadの名前・場所だけ更新される（`lexical_hash` はtitleを含めない）。内容変更は同IDの再索引、消失は `deleted` を経て派生pointをwithdrawする。ルートが列挙できない・全件消失のfull scanは削除扱いにしない。
- AoiTalkの画面・AIツール（upload/save/rename/move/copy/delete/restore/展開/圧縮）は `file_explorer_service` から該当パスのdirty hintを出し、その部分だけ再スキャンする。外部変更は `scan_interval_seconds` ごとのfull scanで拾う。
- 対象形式: テキスト/コード（UTF-8→CP932で復号、4000字単位）、docx/xlsx/pptx（既存 `read_file` と同じmarkitdown変換）、PDF（page）、画像、音声・動画（区間）、msg/eml。それ以外は `unsupported`。
- 認可: User Filesは本人のみ（adminも他人のUser Filesは検索しない）。Project Filesは現在のProject read権限とsearch scope。resolve時に行の状態・size/mtime、索引時にsha256を再照合し、食い違えば結果・`media_read`・`/content` を拒否する。
- 設定は `app_config_settings` の `search.files_index`（既定値は `default_files_index_settings()`、既存DBには欠落キーとして補完）。`exclude_patterns` は `name/` がディレクトリ、`*.ext` がファイル名、`/` を含むものが相対パスに一致する。`include_patterns` で再対象化でき、`overrides.users|projects.<id>` で所有者別に追加できる。
- 既定除外の根拠（2026-10の実データ）: adminのUser Files約9,200件の大半は `daily/20261002/dot-handoff-results` 等のQA証跡（png/json/log）とapk/zip/build cacheだった。一方 `daily/` には FW入力xlsx・ttl・macro出力もあるため `daily/` 自体は除外しない。FWD Projectの `temp/` は申請xlsx/msg/ttlを含むため `tmp`/`temp` は除外しない。`*.log` はUser Filesだけ既定除外（daily のbuild/test log）で、Projectの `temp/*/log/*.log`（FW設定ダンプ）は対象に残す。アーカイブ・apk・実行形式・DB・lock・cache/build/dist/node_modules/venv・隠しファイルは除外し、理由は `excluded_reason`（`pattern:<glob>`, `hidden`, `size_limit` 等）で可視化する。
- 負荷: Files jobは他kindより低優先（priority 10）、巨大・音声/動画は更に後（20）。`max_concurrency` は追加のFiles専用laneの数で、推論自体は単一IPC workerで直列化され、hash・decode・Office変換が並行する。失敗は `max_failed_attempts` 回で `failed` に止め、内容変更または `POST /api/search/files/rescan {"retry_failed": true}` で再試行する。
- API: `GET /api/search/files/status`、`GET /api/search/files/entries`、`GET|PUT /api/search/files/settings`、`POST /api/search/files/rescan`。`media_search` の `targets` に `files` を含めるとFiles laneを検索し、結果は `files_location`（scope/project_id/path）を持つ。同じsha256の原本が他kind（チャット添付など）にもあればFilesを主結果にし、他は `other_locations` に並べる。Files索引が未完了なら `files_index`/`files_fallback_note` を返し、Agentは `search_files`/`bm25_search` の文字列検索を併用する。
- 移行/凍結用の `runtime_candidate`/`runtime_media_adoption` は `workspace_file` を含めない。Filesの派生点はスキャナと通常workerから再生成できる。
