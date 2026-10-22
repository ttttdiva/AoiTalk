# 人格workspace frontendの結合・受入

2026-10-07。Operationsの既存画面を再利用し、bare `/operations` の入口を人格の制作workspaceにした。旧Overviewは `tab=overview` から引き続き利用できる。quota/settingsの専用UIと共有componentは変更していない。

## 実装した入口

- `tab=studio&persona=<id>`: 認証済みbrand portfolioのowner、公開人格ID、Project bindingに一致する正規 `/operations/media/personas` 一覧・詳細だけを使う。SNS事業本部は非公開。取得失敗時は他owner・別Project・先頭人格へ切り替えない。
- `stage=brief`: 本人が制作計画と保存済みbriefを明示選択する。本人入力は `input_kind: user_authored` で空白・改行を含む原文を保存し、研究採用の企画と区別する。人格revision・owner・Project・返却された原文と種別を照合する。
- `stage=generation`: 生成計画または `creation=existing` の既存素材登録を本人が選ぶ。生成runの選択変更で古い非同期結果を取り込まない。
- 既存素材はgenerated media、Files、既存GenerationOutputのID登録と本人ファイルのraw binary uploadを利用する。実bytesを1MiB単位でSHA256計算し、保存receiptとreadbackを照合する。画像25MiB・動画512MiB、MIME制約はbackend契約に従う。
- `stage=qa`: 選択した保存output ID/SHAを媒体別payloadと `generation_output_refs` に引き渡す。登録だけではQA・権利・公開承認を与えない。
- `stage=publish`: 保存variant・account・revision/hash・公開範囲・予定時刻・添付実体を確認する。本人の公開準備と公開ボタンからのみproviderのintent/approve/attempt APIへ進む。実成否は保存Action receiptを表示する。
- 社員は共通組織のまま人格assignmentと当該Projectの仕事・計画→子の制作/確認/返却→親の制作/確認/返却を可視化する。採用計画の割当、子の返却数、差戻し後のround、上司の返却先、計画採用の停止条件を表示する。workspace読込でユーザーTask/機械Jobを作らない。creator・人格・Projectを固定し、成果のID・hash・work/runを照合する。

## 公開・再開境界

ブラウザは参照ID/hash/同一操作keyだけを保存する。payloadやcredentialをlocalStorageに保存しない。Web Locksと同期guardで二重clickを止め、reloadで自動mutationしない。公開attemptの結果が未知なら新しい投稿を再送せず、同じActionのreceipt・provider状態を照合する。YouTubeの再開は保存checkpointをbackendが明示した同じuploadに限定する。

素材previewはsame-origin認証routeをID/SHAから組み立て、APIが返したURL/pathへ移動しない。packageがGenerationOutput/ArtifactVersionの結合不足またはarchiveサイズだけでpartialの場合も、凍結payload・source pin一致と正式asset routeの保存metadata/実体decodeを全添付分で確認する。manifestのverified状態を捏造しない。media専用proxyはrequest stream上限、response stream、Range/If-Matchと下流cancelを扱う。通常JSON proxyの制限は維持する。

公開payload照合はbackend `canonical_publication_payload` に合わせ、X/YouTubeの宣言済み任意項目のnull・空配列・X sensitive falseのHTTP DTO補完だけを同値化する。hash、本文、公開設定、予定時刻、非空値、素材IDと配列順は変更しない。

OAuth callbackはqueryを最初にhistoryから除去し、no-store/no-referrer/CSPを設定した同一originの認証POSTでrelayする。code/stateを別ページのreturn URLやログへコピーしない。実credentialの導入と本人による接続は利用者・運用の手順であり、source実装の受入と分ける。

## 検証

- 初回対象unit: 15 files / 63 tests PASS。追加契約と最終差分の対象unit: 13 files / 69 tests PASS。
- 永続 `frontend/e2e/operations-persona-workspace.spec.ts`: isolated Chromium 6 PASS。desktop/mobile、人格切替とkey操作、一覧503/no fallback、実PNG raw登録・二重click1POST・認証decode・reload noPOST・QA handoffに加え、本人brief原文保存→reload→Plan固定→reload後の保存Plan明示選択を含む。brief・Planとも同期二重clickは1POSTで、生成submitは0。
- 独立reviewの最新差分: 素材14、組織6、canonical owner/Project4の計24 PASS。実PNG/WebM再生、異bytes・scope/hash不一致・metadata/bytes503、同一操作の本人再開、preview再確認復帰を検証。追加でmalformed200、空組織、保存済み素材→QAの9チェック（scope4再検証を含む）もPASS。さらにDTO補完3媒体と本文・公開範囲・配列順の差替え拒否の6チェックもPASS。正常系Console/Network/pageerror 0。独立reviewの結果は [QA結果](qa/persona_frontend_2026_10_07.json) に保持する。
- Next production build、TypeScript、対象ESLint PASS。buildの既存tasks attachment/NFT警告は今回変更の経路外。
- 追加契約の独立Chromium: 公開operation 30、素材partial 12、社員計画10、本人brief/Plan12、入力種別境界8の計72 PASS。Plan連打とreload再照合の指摘は修正後に再検証済み。未知の入力種別は停止し、正規researchの旧Plan snapshotは互換表示できる。未解決指摘なし。
- 実SNS投稿・account作成・OAuth grant・paid model呼出しは0。隔離fixtureのAction応答はUI検証用であり、実provider公開の成功証拠ではない。

## 追加契約への追従

本人入力は [制作入力契約](media_content_input_contract.md) に従う。既存researchのfinding/candidate必須条件、hash、ACLを保持し、本人入力の保存でfake finding/evidenceを作らない。原文・種別・作成者・immutable Persona revision・ContentItem version/hashはserver-owned Plan snapshotへ固定される。frontendはPlan作成時とreload後の既存Plan選択時に正規ContentItemと照合する。不一致時は本文・Run出力を表示せず、submitを停止する。Planの同期連打も1POSTに制限する。

素材候補のpartial headersは、exact GET `operations/media/asset-sources` に限って2項目をproxyで転送する。部分取得時は自然文の案内を1箇所に表示し、取得できた候補を使用できる。403はcached候補があっても登録を停止し、登録503はunknownの参照/keyを保持してreload後も自動再送しない。

公開operationはbackend分類に一致する。Shortsはvideo、X画像・動画添付とthread添付はimage、YouTube予約公開はscheduleを要求する。未対応のX予約投稿とYouTube字幕uploadは具体的な理由を表示して準備を停止する。

社員の計画と再作業は [社員委任契約](employee_delegation_workflow.md) のDTOを表示する。実managerが計画し子を生成したとUI fixtureで偽証しない。実DB migration・dispatcher・自動委任の受入とprovider接続は別担当の結合工程である。frontendの契約待ちは解消した。

## Bridge準備のみの経路

[Bridge準備契約](media_publication_adapters.md) と migration `20261007_0025` の正規DTOに接続した。公開パネルに本人が選ぶ「公式APIで公開」「Bridgeで準備のみ」を追加し、接続失敗時に経路を自動変更しない。Bridgeはmanual Actionを本人が承認し、指定した本人のPC・既存tabへ下書きを準備する。Xは本文・単一管理画像、YouTubeはprivate動画・空tags・子ども向けではない設定が初期対応範囲である。

人格・Project・account版/hash・接続snapshotのID/platform/実account・QA・管理素材ID/SHA/実bytesを照合し、PCとtabを自動割当しない。準備POSTは正規契約の6項目だけを送り、claimで増加したAction版を同じIDのGETで読戻して準備証跡と照合する。X本文またはYouTube canonical metadataのSHAも確認する。機密checkpointやsession情報を表示・localStorage保存しない。

準備進捗と証跡はPublicationReceiptから分け、「本人の最終操作待ち」「YouTube private」「公開結果は未確認」を表示する。最終Post/Saveは本人がSNS画面で操作する。準備POST後は再入力を停止し、reloadで自動mutationしない。結果不明時は同じAction・台帳のGETと本人のinspectだけで再照合する。元のpackage素材を取得できなくても、有効な保存証跡を表示しつつ唯一のalertでpreview取得を停止する。異account/PC/内容SHAやprovider Actionは利用しない。

正規metadataの `source_available:true` と `available:false` を区別する。現状のBridge準備は配布版・実SNS画面の受入待ちとして停止し、PCが接続済みでも利用可能と表示しない。実PC配布版・実SNS DOM・実PostgreSQLの結合受入は担当側の工程であり、隔離UI fixtureを実投稿成功として扱わない。

Bridge追加・既存人格UI回帰のunitは15 files / 62 tests PASS、TypeScript・対象ESLint・Next production build PASS。恒久 `operations-persona-bridge.spec.ts` 5件と既存workspace 7件を実Chromiumで確認した。既存保存先エラーE2EのURLを現行 `stage=generation&creation=existing&content=<id>` に修正し、503の安全な案内と403の停止も再確認した。独立reviewは初回30件・長名表示4件・最終差分18件の計52 PASS。実PNG/WebM previewと再生、desktop/mobile・長いPC名・二重click・unknown/reload無再送・foreign account/PC/SHA・権限拒否・遅延経路切替・既存provider入口・package503/差替えを検証し、未解決指摘は0件。正常系のConsole/Network/pageerrorは0。実SNS投稿・Attempt・OAuth grant・paid model呼出しは0。

## 社員組織setupの正規UI入口

`/operations?tab=agents&panel=organization` の「社員組織を設定」から、正規 `/api/agent-organization/setup` のemployees設定を入力し、「保存前の内容を確認」→作成・再利用対象、直属上司、未設定社員の明示Team/Profile、activate、変更する役職と変更しないProject/Persona権限を確認→本人checkbox→保存→GET読戻しへ進む。manager_key省略は既存上司を保持し、nullの明示だけで解除する。既存職務revisionは保全し、職務・モデルの変更は既存個別UIへ誘導する。新社員のslugとrevisionの冪等性は既存canonical serviceが担う。Web Locksと保存前pending再読で二重click/別tabを直列化し、不明結果・reloadの自動POSTは0にする。

正規owner-bound SNS portfolioの新brand Project/Personaと非公開本部の範囲を読取表示する。既存SNS recipeのkeyに対応する保存予定社員はその担当予定範囲も表示するが、setupにProject/Persona grantやcredentialを混ぜない。`ready`は組織・職務の構造の判定で、仕事開始やprovider準備を偽証しない。職務のworkspace_read、選択Projectだけのviewer/read、対象Persona担当、ProjectにSpaceがあれば当該Space所属が社員委任に必要である。既存の `panel=role` と `panel=assignments` へ具体的にリンクし、全Project・delete・manage_members/manage_settingsを自動付与しない。直属関係を使う最小委任では共有社員のProject管理権限は不要であり、別ブランド責任者から直属でない共有職へ委任する場合のscoped management ACLは別の明示設計になる。

対象unit 2 files / 30 tests、TypeScript・対象ESLint・production build PASS。独立実Chromiumは新規下書き、既存上司・ID/slug/name保全、明示上司変更、catalog403、unknown503→GET失敗→reloadの読戻しをdesktop/mobileで確認して10 PASS。追加で別tabのunknown再送停止とpreview後の名前変更停止をdesktop/mobileで確認し4 PASS、合計14 PASS。最後に権限上限previewと非配列入力停止の3件を確認し、独立確認は合計17 PASS。正常系Console/Network/pageerrorは0、意図した403/503以外の失敗は0。未解決source指摘は0。source開発から実社員・実grant・実モデル呼出し・外部操作は0であり、利用者の正規UI設定と実委任受入は別工程である。

## 正本標準組織previewのファイル読込と不明保存の復旧

標準rosterの正本は `scripts/setup_employee_organization.py` の `--crowdworks` / `--sns` である。CWは既存CW/Web/App3社員と追加4職、SNSは既存MediaDirectorと8brand担当・共有5職を生成する。既存slugも現在のID・名前を使い、既存revision・状態を省略して保持する。frontend独自presetは追加しない。明示Team/Profileを指定したpreviewをUTF-8ファイルへ出し、`/operations?tab=agents&panel=organization` の「正本scriptのpreview JSONを読み込む」→保存前preview→本人checkbox→保存へ進む。`--output` はrepo内 `.local` に限定し、`--apply` との併用とsymlinkによる範囲逸脱を拒否する。

previewは最初に既存DBのAlembic headを確認し、その成功後に保存済み設定を読取専用snapshotで取得する。`AOITALK_REQUIRE_DATABASE=true` を維持し、同期・非同期の両接続でtransactionをread-onlyにする。previewからDB初期化・migration・stamp・設定seed・設定保存は行わない。DB接続やmigration版の不足はその場で停止し、通常起動側の正規migration完了を必要とする。

既存venvを有効にしたrepo rootからの最短コマンドは次の2本である。山括弧は本人が既存の管理者・社員・職務カタログから選んだ参照で置き換える。

```text
python scripts/setup_employee_organization.py --crowdworks --actor-user-id <本人管理者ID> --worker-team-id <既存workerTeam> --worker-profile-id <既存workerProfile> --reviewer-team-id <既存reviewTeam> --reviewer-profile-id <既存reviewProfile> --output .local/cw-organization-preview.json
python scripts/setup_employee_organization.py --sns --actor-user-id <本人管理者ID> --director-agent-id <既存MediaDirectorID> --brand-team-id <既存brandTeam> --brand-profile-id <既存brandProfile> --worker-team-id <既存workerTeam> --worker-profile-id <既存workerProfile> --reviewer-team-id <既存reviewTeam> --reviewer-profile-id <既存reviewProfile> --output .local/sns-organization-preview.json
```

ファイル読込はPOSTしない。SNSの8bindingsは現在の本人portfolioのProject・Persona/revision・Program・ContentItemと全件照合し、失敗時に別owner/Projectへ切り替えない。CWはSNS portfolioの取得失敗から独立して確認できる。正本recipeの新revisionは権限上限が空であり、組織作成だけではworkspace_readや実行を準備済みとしない。直属上司の職務・有効化不足はPOST前に停止し、個別社員画面で準備する。

送信前にowner・正確なmanifest hash・各既存社員identity・状態・プロフィール・revisionの保全hashをv2ブラウザ記録へ保持する。入力本文は記録しない。結果不明時は元の同じpreviewを読み込み、「同じ設定の保存完了を照合して送信記録を解除」を本人が押す。GETだけで同owner・同hash・全行の明示変更と保全条件が一致する場合に解除する。プロフィールは役職・責務・Space・上司・autonomy・employment・company ceilingと既存作成時刻、既存revisionはID/version・本文・Team/Profile・capability/allowed subagent・wake/budget/concurrency・content hashを含む保存済み全項目を照合する。変更が許される更新時刻と動的route warningsは除外する。途中保存・別owner・異manifest・差分・壊れた記録は停止し、再POSTしない。旧v1記録はowner/hash/保全条件が無く、証明できないため解除しない。クライアントの保存前GETはserver CASではなく、同時更新を原子的に保護するとの保証はない。

## 保存済み企画を統括社員へ依頼する入口

`/operations?tab=studio&persona=<人格ID>&stage=brief&content=<保存済みbriefID>` の保存済み企画に「この企画を統括社員へ依頼」を追加した。既存Work画面と委託panelは表示専用であり、新規API手作業を利用者に要求しない。選択人格の有効なPersona担当・Project権限・職務を持つ統括、制作、独立reviewの3社員と仕事の種類を選び、1呼出／全体の出力token上限を確認し、preview→本人checkbox→明示依頼ボタンで正規 `/api/agent-delegations` を作成する。

同じ本人・Project・Persona revisionのbriefを保存版の本文・版・hash・入力種別と共にinstructionへ固定し、送信前に正規GETで再確認する。統括／制作／reviewの職務revisionも固定する。現在の人格だけを計画scopeとし、許可した2社員、最大2子・深さ1・各段階1試行・再作業0回に限定する。これは予算／権限の新しい別実装ではなく正規planning policyへの入力であり、serverが予算・職務・所属・直属管理を再検証する。briefの参照はinstructionの保存hashに含み、delegation契約に新しいContentItem外部キーを追加しない。

POSTの正本content hashを全instruction/planning入力から照合し、同じIDのGETでowner・Project/Persona・3社員revision・hash・全体出力上限を確認して保存結果を表示する。進捗リンクは同じ人格の「組織・計画→制作→確認→返却」へ移動する。同期二重click・別tab・reloadの再送は0。unknown/競合/読戻し不一致は送信記録を保持し、正規transactionが明確に拒否した401/403/404/422だけは記録を解除して条件修正後の新しい本人previewを許す。権限不足には安全なcanonical確認コードと最小必要範囲を表示し、role/grant/provider設定や公開を自動追加しない。

追加導線のunitは6 files / 50 tests、scriptのpure preview/export境界は3 tests、TypeScript・対象ESLint・最終production build PASS。恒久Chromium回帰は組織のdesktop/mobileファイル読込＋GET-only復旧2件と、社員依頼の正規hash/同ID読戻し・別ID拒否2件の計4 PASS。独立reviewは組織30件・社員依頼30件・CLI境界6件すべてPASS、未解決source指摘0。正常系のConsole/Network/pageerrorは0、意図した拒否・失敗以外のNetwork失敗は0。fixture結果を実manager生成・実公開成功とは扱わず、実DB変更・grant・credential・SNS操作・paid model呼出しは0。

共有社員がMediaDirector直属で、brand担当が統括する場合、正規 `can_manage_project_delegation` は統括社員の選択brand Projectに限った `manage_members:true` と、それを許容する既存の組織・社員・Spaceの権限上限を要求する。制作／review社員の管理権限は不要である。各参加社員には同じProjectの `read:true`、Persona担当の `capability_ceiling:["workspace_read"]`、職務の `workspace_read` を確認する。ProjectにSpaceがあれば当該Spaceの有効所属も必要で、全Project・delete・manage_settingsを付与しない。

## 正規CLIの実PostgreSQL受入

readiness修正 `5871812df` を隔離したPostgreSQL 16で確認した。fixture準備だけで正規migrationと本人管理者・既存CW/Web/App/MediaDirector4社員・保存済みTeam/Profile・正本v6 portfolioを作成し、その後のbaselineを取得した。正規CLIを `AOITALK_REQUIRE_DATABASE=true` とSELECT専用DBロール（`default_transaction_read_only=on`）で直接実行し、CW previewは7職・既存3社員保全、SNS previewは14職・既存MediaDirector保全・8個の異なる本人Project/Persona bindingをUTF-8 JSONへ出力した。preview前後のpublic全268テーブルの件数・全行内容hashは一致した。

旧Alembic版では非ゼロ終了・出力なし・DB差分なし。global設定行がない正常DBでは既存社員だけのmanifest previewが成功し、設定行は追加されずDB差分もなかった。対象unitは5 PASS、独立source reviewは指摘0でread-only両engine・head不一致・apply境界の3件もPASS。隔離DBのfixture準備とsource受入を区別し、利用者DB変更・実社員委任・公開・credential導入・paid model呼出しは行っていない。実dispatcher/provider運用の受入はこのCLI確認に含まない。

## 組織設定・社員依頼の正規本人照合

実ログイン後も存在しない `/api/auth/session` が404となり、組織取込と社員依頼が送信前に停止していた問題を `86604ea44` で修正した。両導線の共通owner取得は、既存Next.js `/api/auth/status` が `getSession()` でDB検証した本人IDを使う。操作ごとにcookieを再照合し、未認証・ID不足・blank ID・パスワード再設定要求は停止する。FastAPIのboolean-only statusやcached ownerへfallbackしない。正規auth routeの返却項目・認証・権限は変更していない。

対象unitは2 files / 16 tests、TypeScript・対象ESLint・production build PASS。恒久E2Eの認証fixtureも実在するstatus DTOへ変更し、存在しないsession URLへのrequestを検出する。独立Chromiumはdesktop/mobileの両UIで24件PASS。正常系の二重clickは1POST・receipt読戻し後のreloadは追加POST0、owner変更・未認証・reset要求・boolean-only status・blank IDはPOST0。存在しないsession URL・FastAPI statusへのfallback request、Console/pageerror、mobile横overflowは0。実PG・本物login/cookie・正規HTTPによる両導線保存の再受入は別の保持中QA環境で再確認待ちであり、fixtureの成功を本物保存の成功とは扱わない。
