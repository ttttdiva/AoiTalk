# X / YouTube の公開adapterとUI契約

既存MediaOpsのContentVariantRevision、ExternalAction、human approval、ExternalActionAttempt、Credential Vaultを使う。公開intentのAPI正本は `POST /api/operations/media/actions` であり、別queueや `/publish-intents` APIは追加しない。adapter versionは3。

## 制作物のpreviewから実行まで

| 順序 | 正規API | UIで固定する値 |
|---|---|---|
| 1 | `GET /api/operations/media/platform-accounts/{id}` と `GET .../{id}/credential` | brand Project、Persona、実account、account revision、verified credential。pausedの内部枠を実accountへ勝手に割り当てない |
| 2 | `GET .../{id}/provider-readiness`、human接続後 `POST .../{id}/provider-readiness/refresh` | text/image/video/schedule別status、付与scope、account eligibility、adapter3。refreshはvaultから現権限を観測する |
| 3 | `POST /api/operations/media/actions` | `action_type:"media.publish_content"`、`execution_mode:"provider"`、一意のidempotency key、既存 `MediaActionCreateRequest` のItem/Variant/VariantRevision/PersonaRevision/account/accountRevision/connection/Project/payloadとhash。5つのprovider authority値をすべて省略したとき、backendが人のrefreshで作成済みの現在のcapability/credential pinを解決する。部分指定・古いpinを新しい値へ補正しない |
| 4 | `GET /api/operations/media/actions/{id}`、`GET /api/operations/actions/{id}/package?expected_action_version=...&expected_payload_hash=...` | concrete Persona/account/content/添付hash/公開範囲/予約日時をpreview。返却したpayloadを後から組み直さない |
| 5 | `POST /api/operations/media/actions/{id}/approve` | `expected_version`, `action_version`, `payload_hash`, `artifact_hashes`をpreviewから送る。本人のボタン操作のみ |
| 6 | `POST /api/operations/media/actions/{id}/attempts` | 最新 `expected_version`、`executor_type:"provider"`, `execution_mode:"provider"`、intentの同じ `execution_key`。完了後 `GET .../actions/{id}` でprovider receiptを表示 |

作成・承認・実行は既存生成型の `MediaActionCreateRequest` / `ActionDecisionRequest` / `AttemptCreateRequest` を使う。metadata/API adapter状態だけでは実公開済みと表示しない。confirmed provider receiptのremote status（YouTubeの実際のprivate/unlisted/public/scheduled）を表示する。YouTube未監査projectでpublic要求がprivateになった場合も、providerが返したprivateを保持する。

結合診断では `GET content-variants/{id}/readiness` がQA／rights記録後も200になることを確認する。readinessは既存ledgerの互換aliasを正規projectionへ変換する。provider intentの5つのauthority pinをすべて省略した場合、serverが人のreadiness refreshで確立済みのtupleを選択する。4つの公開snapshot／adapter pinがすべて指定されcredential digestだけ省略された場合も、serverのfreshな検証済みbindingからdigestだけを補完して全tupleを照合する。他の部分指定・古いpinは自動置換せず拒否する。package previewのqueryは**返却されたactionの `action_version` と `payload_hash`**を使い、ContentVariantRevisionのhashやactionの一般 `version` を渡さない。source revision／payloadの不一致による409は再previewを要求する。実HTTP境界の回帰は `test_http_proposal_resolves_server_pins_without_exposing_credentials` と `test_regular_http_minimal_revision_qa_rights_intent_package_acceptance` にある。通常HTTPでminimalなX post／threadを保存し、QA・rights記録後にresponseが補うnull／空配列を含むbodyを返しても、共通 `canonical_publication_payload` で同じ意味として照合する。比較だけを正規化し、stored revision／QA hashは変更しない。本文・公開設定・schedule・素材ID・配列順の差替えは拒否し、Pixivの未指定AI／R18 flagをfalseへ変換しない。

素材previewとの結合: package exporterはGenerationOutputをArtifactVersionへ推測で結合せず、`generation_output_artifact_binding_unavailable` のpartial manifestを維持する。最新UIはこの理由またはpackage容量上限によるpartialを限定して受け入れ、正式assetのowner／Project／persona／manifest SHA照合と、認証済み画像／動画の実体preview取得成功を公開ボタンの追加条件にする。provider内部readerは送信前に再検証する。packageのverifiedや公開済receiptを捏造しない。今回の通常HTTP flow受入は素材を持たないX post／threadの範囲で、画像／動画の製品UI受入は別途確認する。

提案・修正・承認・実行はpayload全体とQA/Rights済みContentVariantRevisionのcanonical完全一致を共通境界で強制する。本文・公開設定・thumbnail・同ownerの別素材も変更できない。PlatformAccount.persona_idは対象PersonaRevision.persona_idと一致必須で、legacy未結合は自動割当せず停止する。

Xは既存payloadのtext/hashtags/links/reply/media/threadを、YouTubeはtitle/description/tags/video/visibility/scheduled_at/audience/disclosure/thumbnailを使う。GenerationOutputの所有者・Project・既存SHA/MIME/サイズを照合し、assetは最大1 GiB、thumbnailはJPEG/PNG最大2 MiB。登録済み素材は既存 `MediaAssetService.materialize_for_provider` で内部snapshotを取得する。owner／Project、review済みmanifest SHA、immutable revision、MIME・サイズ・実bytesを照合し、登録済み素材の欠落・改変・scope不一致を公開URLへfallbackしない。readerが未登録を示す `None` を返す場合だけ公開HTTPSのGenerationOutput.deep_linkを使う。登録済み画像は25 MiB、動画は512 MiBまでで、pathはDTOへ出さない。公開DNSの全回答を接続時に検証し、numeric IPへTCP接続する専用transportを使い、元のHost/SNI/TLS hostname検証を維持する。proxy・redirect・認証header・圧縮応答を拒否する。取得rowを再読込し、期待SHAは可変rowでなくreview済みrevision manifestを正本とする。Studioの相対URL・認証必須素材は正式asset登録APIと、server設定で固定したStudio origin／既存credentialの内部readerを経由する。未登録のprivate／LAN URLは拒否する。素材登録はQA・rights・公開承認を付与しない。結合契約は [media_asset_contract.md](media_asset_contract.md) を参照。providerのouter `schedule` は空objectだけを許可し、予約日時はreview済みtyped payloadの `scheduled_at` に限る。X API予約投稿は未対応で、予約要求を即時投稿へ変換しない。YouTube Shortはvideo、Xのmedia付きpost/threadはmedia権限を必要とする。caption upload、edit/delete、実モデル生成はこのadapterの対象外で、未実装を成功扱いにしない。Xの `sensitive_content:true` と非空 `alt_text`（post／thread内post）はこの版では `x_api_sensitive_content_unsupported`／`x_api_alt_text_unsupported` としてreadiness・提案・承認・実行の共通境界で拒否する。既存承認済みintentとprovider内部entryでも送信前に停止する。false／nullのDTO既定値は拒否しない。asset-reference内の非空alt_textはID正規化前に拒否し、説明文を黙って削除しない。現公式 [create Post](https://docs.x.com/x-api/posts/create-post) と [media metadata](https://docs.x.com/x-api/media/create-media-metadata) のbodyを確認したが、この版にレビュー済み契約からmetadataへ写す実装はないため、推測したparameterを送らない。

## 不明結果と再開

`POST /api/operations/media/actions/{id}/provider-recovery` のoperationIdは `media_operations_recover_provider_action`。bodyは `{ "expected_version": 4, "resume": false }`。responseは既存 `ActionResponse`。`resume:false` は既知receipt/sessionの照会、`true` は同じ承認・同じsession・同じvideo SHAからYouTube upload/thumbnailを再開する。新intent/attemptは作らない。

Attemptには安全な `provider_recovery_available:boolean` と `provider_lease_expires_at:string|null` を追加した。状態は暗号化されたserver-only checkpointで、token、upload session URL、PKCE verifierをDTOへ出さない。実行中leaseは15分。lease有効中の回復、Agent/bearer権限のwrite、owner/Project/content/account/credential pin変更は拒否する。action→attemptの順でlockし、version CASをprovider呼出とcheckpoint保存の前に確認する。古いworkerが復旧後の状態を上書きできない。

Xのcreate応答が不明でpost IDを保存できなかった場合、timeline/searchで「未投稿」を証明できないため `receipt_unknown` のまま停止する。 quota上限の有無に関係なく、unknownのfailed/not_submitted reconcileを拒否する。任意note・ID欠落・架空lookup refは未送信証明にならない。過去のuncertain/reconciled.failed eventがある同じprovider intentの再承認・新attemptも拒否し、同じattempt/sessionの回復またはpositive receiptの照合に限定する。`resume:true` でも新createは送らない。途中threadもpartialのまま照合する。YouTubeは既存sessionを空PUTでprobeし、308 Rangeが確認したbyteから再開する。timeout/5xxのprobeは1呼出につき最大3回、session不明/期限切れ時は新sessionを作らない。API結果がunknownのときbrowser経路へfallback再送しない。正規の人間reconcile APIを使い、証拠なしの成功確定を拒否する。

## 正規OAuth接続

| API | operationId | request/response |
|---|---|---|
| `GET /api/operations/media/platform-accounts/{id}/oauth` | `media_platform_oauth_metadata` | project/account/revision、configured、blockers、required_scopes、client_ref、redirect_uri、external_checks。秘密は含まない |
| `POST .../{id}/oauth/start` | `media_platform_oauth_start` | `{expected_account_revision:1, expected_credential_revision:0}` → official authorization_url、10分のexpires_at、exact project/account |
| `POST /api/operations/media/oauth/callback` | `media_platform_oauth_callback` | `{state, code}` または `{state, error}` → connected/connection_failed/consent_deniedとsafe credential DTO |

callbackは同じ認証human/owner/Project/account revision/credential revision/configだけが一度消費できる。codeはbody上限16 KiB、token responseは64 KiB。実providerのidentity一致と付与scopeだけを既存暗号化vaultへ保存する。scopeが返らないtokenはidentityだけverifiedになり、publish/media権限を推測しない。state/PKCE verifier/config pinは別AADの暗号文で保持する。YouTube channel IDはcase-sensitiveで比較する。

設定はserver secret管理から `AOITALK_MEDIA_OAUTH_CONFIG_JSON` に供給し、brand Project UUID → `x`/`youtube` → `client_id`, `client_secret`, `redirect_uri` の順に指定する。共有default fallbackやCalendar client/scopesの流用はしない。redirect URIは登録済みHTTPSの専用UI callback（query/fragmentなし）。設定値をfrontend/DB平文/debug logへ送らない。

UI callbackのHTTP responseに **`Referrer-Policy: no-referrer` と `Cache-Control: no-store`** を設定し、asset/analyticsより先にqueryを除去する。その後、認証済み同origin proxyでcode/stateをPOST relayする。callback requestのqueryはfrontend/gateway/reverse-proxyのaccess logでredact/除外する。Python標準httpx INFOの全URL queryとHTTPcore DEBUGのheader/body、Bearer/Basic/code/state/access/refresh diagnosticsもlogger/handlerで秘匿する。gateway側の設定は別途必要。backendにGET callbackはない。リロード/二重callbackの409で新grantを自動発行しない。

X scopeは `tweet.read tweet.write users.read media.write offline.access`。YouTubeは `https://www.googleapis.com/auth/youtube.upload` と `https://www.googleapis.com/auth/youtube.readonly`（自身channel/private receiptの照合にも必要）。Developer App、API access/credit/rate limit、Google API enable/consent/審査、公開upload監査、custom thumbnail permissionは外部設定・実接続で確認する。metadataのexternal_checksは未確認の要件であり、PASS証明ではない。token失効/無効化では停止する。この版はrefresh tokenの自動更新をせず、再接続・rotation後は古いintentを新credentialへ移し替えず再preview/approvalする。

## 製品Bridgeの実行境界

既存 `RemoteControlBridge` のowner/device/接続epoch固定と共通operation lockを再利用する。汎用BrowserAgentの完了や外部CUAのpreviewは公開receiptにしない。既存 `execution_mode: manual | provider` を保持し、以下のprepare-onlyは承認済みmanual actionに結ぶ独立証跡である。providerのunknownをbrowserへ自動fallbackして再送しない。

固定agent_browser helper v4の配布契約にはSNS preparation commandsがない。現sourceはextension backendに閉じた専用commandsを追加した。extension source、PC sourceとmigrationを入れただけで、既存Windows exe/bundleや実SNS DOMの受入済みとは扱わない。metadataはsourceの有無とlive利用可能性を分けて返す。

2026-10-07確認の[X Automation Rules](https://help.x.com/en/rules-and-policies/x-automation)はnon-API website scriptingを禁止。[YouTube Terms](https://www.youtube.com/static?template=terms)は自動アクセスに事前書面許可等の例外条件を設ける。実アカウントでの利用にはplatform側条件と配布・DOMのlive受入が必要である。この開発受入はisolated fixturesに限定する。

## 現公式資料

- [X create Post](https://docs.x.com/x-api/posts/create-post)、[initialize](https://docs.x.com/x-api/media/initialize-media-upload)、[append](https://docs.x.com/x-api/media/append-media-upload)、[finalize](https://docs.x.com/x-api/media/finalize-media-upload)、[OAuth PKCE](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
- [YouTube videos.insert / 未監査project公開制約](https://developers.google.com/youtube/v3/docs/videos/insert)、[resumable protocol](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)、[Google OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)

## 2026-10-07 製品Bridge prepare-only実装と隔離受入

現sourceには既存manual MediaAction／人間approvalを使う準備専用ルートを追加した。`POST /api/operations/media/actions/{action_id}/browser-preparation`（`media_operations_prepare_browser_action`）は `expected_version, action_version, payload_hash, device_id, tab_id, idempotency_key` のみ受け取り、本文とaccountは承認済み保存内容から取得する。`GET` は保存結果、`POST .../inspect` は `preparation_id, expected_version` を受け取り同じepoch／documentへの読み取り専用照合を行う。フロントエンドから本文・account自己申告・公開確定フラグは受け取らない。

migration `20261007_0025` の `media_browser_preparations` はowner／project／personaに結ばれたaction revision/hash、account revision、明示device/tab、SHA、CAS versionとleaseを保存する。内部checkpointは既存credential vaultの暗号機構とbinding AADを使用し、credential・cookie・path・document/epoch/private lease tokenを公開DTOへ出さない。鍵は通常のfield crypto provider初期化を再利用する。WindowsのGetOrCreateDataKeyは必要に応じてDPAPI保護鍵を初期作成するため、必ず作成済みの鍵だけを読む契約ではない。raw keyのDB平文保存・frontend表示や別経路への移送はしない。actionごとの同revisionを一度だけclaimし、入力直前にfresh行lock／現承認／account／epochを確認する。timeout/cancel後は `uncertain`、自動再入力なし。`awaiting_final_confirmation` は下書きの照合証跡で、`current_action_matches` と `current_approval_valid` が現在の有効性を別に示す。過去のawaitingは承認失効後も公開の許可にならない。Attempt／PublicationReceiptを作らず、generic Attemptへの変換を拒否する。

X初期経路は明示 `@handle` のactual UI読取＋exact UTF-8本文SHA。numeric account IDからhandleを推測しないため、既存numeric-only accountは明示handle設定が必要。既存extension backendのみを対象とし、固定agent_browser helper v4は未対応として拒否する。既存PC WSS readyにconnection epochを付け、接続ごとのbounded素材storeはdisconnect／stopで破棄する。素材channelは内部専用、generic PC HTTP commandから `media_prepare_*` を実行できない。画像transferは正式MediaAssetServiceのimmutable readerのみ、SHA・MIME・size・owner/project・device/epochに固定し、pathやpublicURLfallbackなし。単一JPEG/PNG/WebP<=25MiBの固定DOM素材入力は、File SHAの各await後にlive inputの同一File・1件・name/type/size・同composerの同一inputを照合する。最後の同期確認からchange dispatchまでawaitを挟まず、change後もactual File SHAと本文・account・visible attachmentを読み直す。上位APIもfresh authority/lease確認後にreadonly asset inspectを実行してから準備完了を保存する。File/input置換はuncertainで止まり、自動再uploadしない。これはremote upload完了や公開receiptの代用にならない。YouTube素材senderの初期上限はMP4/WebM<=64MiBで、channel IDとapproved metadata SHAに固定する。

正常HTTP→実PcBridgeHub framing→実PC dispatcher→実extension background/content→隔離ChromiumのXテキスト受入、および正式asset reader→PC sender→実extension画像DOM受入を実行した。全fixtureは外部要求をローカルで満たし、最終Post/Publish/Save/予約確定は操作しない。実Windows配布bundle更新、実SNS DOM版との照合、各ブランドの正規account設定／platform利用条件の確認は別のlive受入が必要。API adapterとbrowser準備の自動fallbackはない。実投稿、実動画upload、OAuthgrant発行、外部account作成、課金はこのcheckpointで実行していない。


上位APIはX本文／単一画像とYouTube private動画を同じdurable ledgerへ結合した。YouTubeはexplicit `audience:not_made_for_kids`、empty tagsを必須とし、thumbnail/captions/disclosure/schedule/nonprivate指定を明確に拒否する。metadataのcanonical SHA、title/description SHA、動画SHAを照合し、最後にcurrent actual Fileと同時点のmetadata／private／audience／tags／最終Saveの有効状態を読み直して `awaiting_final_confirmation` を保存する。読み取り専用 `inspect` はmetadataとFileの両証拠を要求する。File消失・metadata変更・epoch変更はuncertainを公開済みや未送信へ変換しない。DTOの `content_sha256` は共通digestで、Xは `text_sha256`、YouTubeは `metadata_sha256` を返す。内部素材transfer IDとPC epochは暗号checkpointに保存し、公開DTOに出さない。

YouTube本文の各native edit直前・直後に同editor、focus、Selection範囲包含を確認する。同期inputイベントが別fieldへSelectionを移した場合は次の入力を止め、そのfieldを変更しない。Nextも最後のawait後に固定selectorの同一node、id/role/type/意味属性、同dialog・同stageを同期検査してからclickする。done-buttonへの変化やnode置換はuncertainで停止し、最終Saveを操作しない。

YouTubeの同documentに素材入力の証拠が残っている場合、別runはinspect段階で `browser_preparation_new_document_required` を返してblockedになる。素材転送・入力前に拒否し、既存の準備済み／unknown証拠を保持する。同runの再入力は拒否し、readonly照会のみ許可する。次の制作物は準備開始前に別タブで新しいUpload画面を開き、そのtabを新たな承認済みactionへ明示する必要がある。claim済みactionのtab差替え再実行は許可しない。UIは準備済み画面、結果不明の照会、次の制作物用の新規画面を分けて案内し、自動reload・新規画面への自動retryをしない。helper更新時も旧documentの書込みはnew_document_requiredに閉じ、保持した旧証拠のreadonly照会だけを残す。旧version 1〜3は現在の本人確認を証明できないため、読み取り結果も `browser_preparation_legacy_revision_unverified` として準備完了に昇格させずuncertainに保持する。prepared／accepted／files_verified／final confirmationはfalseで、旧closure・Mapは消去しない。

閉じたextension capability contractはversion 2を必須とする。旧version 1は準備claim前、および素材のreadonly復旧lease取得前にupdate_requiredとして拒否する。過去のuncertain証拠やaction versionを書き換えず、素材転送・DOM入力も実行しない。実Windows拡張の更新確認は別受入である。

現在のPersona停止条件はreadiness専用の表示判定にしない。共通DB結合検査を新規prepareのBridge接続前・capability照会後のclaim直前・各入力直前へ適用し、current Personaのpaused／archived、接続の失効、最新accountのinvalid／unsupportedを拒否する。実入力時はaccount／Persona／connectionを再読して行lockを次のDOM commandまで保持し、durable checkpointのcommit後に再取得する。過去のimmutable PersonaRevisionと人の承認が残っていても、この停止条件は迂回できない。保存済みGETは歴史的証拠を残して `current_approval_valid:false` を返す。unknownの再入力は許可せず、停止中のinspectは読み取りだけを実行し、現在の活性条件が戻らない限りawaitingへ昇格させない。実PostgreSQLでのlock-wait受入は未実行である。

`adapter-status` の `browser_publication.preparation` は `source_available:true`, `device_capability_required:true`, `execution_mode:manual` と3正規operationId、各媒体の初期制限を返す。対象なしの照会は `available:false` と明示owner-device-tab必須を返す。`platform_account_id`・`device_id`・`tab_id` を指定すると、本人の書込み権限、persona／project／最新account revision／connectionの結合、所有者に属するPC接続、拡張capability version 2、安全版helper（X 9／YouTube 4）、空の対象画面、本人のplatform識別子、既存暗号鍵の利用可否を読み取り専用で確認する。接続epochとaccount結合を応答直前に再照合し、全条件成立時だけ `available:true` を返す。`readiness_target` は公開してよいIDと確認時刻だけを含み、PC epoch／document ID／鍵／tokenを含まない。照会は鍵を作成せず、入力・upload・Action承認・準備claim・台帳書込みを行わない。

YouTube helper 4はHTTPS `studio.youtube.com` の対象UC channel root／dashboard／videos／videos/uploadだけを許可する。queryはvideos/uploadの空filter・date DESC sortと、既存draftの `d=ud`／11文字 `udvid` に限定し、重複・別query・別hostを拒否する。draft video IDはdialog内の可視動画linkと一致が必須で、新規run／素材入力先には使わない。可視の正規channel linkがない実Studioでは、表示中Accountメニューのname／@handle、sidebar name／home、同windowの唯一の公開handle tabにある可視name／handleと2個の所有者管理linkを照合し、その一致を条件にhidden channel IDを利用する。証拠は両tab・documentと同一DOM要素・値へ固定し3分で失効する。owner照合失敗はtokenを失効させ、閉じたメニューから復活させない。再確認にはAccountメニューの再表示が必要で、自動navigation・tab作成・account切替・公開操作を行わない。helper 1〜3の保持証拠はreadonlyでunknownに保ち、新documentからだけ新規準備する。

実Studioのタグ欄は `ytcp-form-input-container#tags-container` 配下のchip barにある。詳細欄がまだ生成されていない場合は、Details内の固定Show more／「すべて表示」native buttonを同一node・意味・editor・値・accountで照合して展開する。非空の既定タグは消去せず、欄の欠落・重複、Saveへの意味変更、表示中のeditor置換では本文入力前に停止する。表示操作を行った後の不明状態も自動再操作しない。

通常Bridgeビルドは同梱extensionの7ファイルのinventoryと全bytesを現sourceへ照合してからself-test／installへ進む。ビルド確認は配布notices・正式releaseのレビューを代替しない。現端末で既存draft／公開設定selector、Accountメニュー／公開channelの管理link、Show more／タグselectorをread-only確認したが、新版EXE・Native Messaging登録と現接続でのreadinessは未適用・未受入として扱う。既存draftのDetailsと公開設定間ではURLが変わらなかった。新規素材upload後のURL変化は未確認で、runに固定したURLが変わる場合は停止する。

UIでは準備対象のPC／tabをAction作成前に明示し、その組合せに対する最新可否を再確認する。既存の準備証拠の照会は、新規画面の可否と独立して利用できる。YouTubeの照会はrun／upload Mapを増やさず、旧helperや既存draft／転送証拠があるdocumentを利用不可に保つ。配布と実platform DOMの未受入は `verification_notes` として明示し、最終公開は常に `publication_confirmation_allowed:false`、`publication_available:false` のままである。準備の入口が利用可能でも、live受入済み・公開済みを意味しない。

受入証拠（すべて隔離、実SNS操作なし）:

| 対象 | 結果 | 証拠 |
| --- | --- | --- |
| 通常承認API／テキスト・素材準備／共有metadata契約 | 82 PASS | `tests/test_media_browser_preparation.py`、`tests/test_media_browser_preparation_assets.py`、`tests/test_media_publication_contract.py` |
| 対象付きreadonly準備可否／owner・project・失効・鍵・版競合 | 31 PASS | `tests/test_media_browser_readiness.py`。正常X／YTを実Hub・PC dispatcher・拡張・隔離Chromiumで確認、関連DB行snapshot不変、入力／upload／claim／Receipt 0 |
| 承認後のPersona停止／各入力境界／unknown非再送 | 24 PASS | `tests/test_media_browser_preparation_liveness.py`。修正前はpause後の通常POSTでfill1／awaiting／current approval trueを独立再現。修正後はclaim前0、checkpoint後のX／YT File・metadata入力を停止し、歴史的証拠とreadonly回復でauthority falseを維持 |
| PC ready epoch／内部channel／disconnect・stop | 3 PASS | `tests/test_media_bridge_client_lifecycle.py` |
| 正式reader／bounded byte転送／consume一度／cancel／X・YT sender | 86 PASS | `tests/test_media_bridge_asset_transfer.py` |
| X実extension DOMと既存Edge Bridge | 72 PASS、同revision旧YT helper拒否を追加確認 | `tests/test_media_bridge_dom.py`、`tests/test_edge_browser_bridge.py` |
| YouTube実helper・background・metadata/File照合／unknown復旧 | 113 PASS、marker追加後の影響対象6 PASS | `tests/test_media_bridge_youtube_dom.py` |
| 既存暗号鍵のreadonly利用確認／vault互換 | 暗号87 PASS、vault／migration23 PASS | `tests/test_field_crypto.py`、`tests/test_media_credential_crypto.py`、既存Enterprise／vault関連tests。実DPAPIは未実行 |
| 対象選択からのWeb準備／保存証拠照会 | unit23、E2E8、独立実ブラウザ2 PASS | 1440／390px。tab変更・foreign targetは書込み0、doubleclick各1、reload再入力0、新規準備不可でも保存inspect可。隔離認証／API fixtureを使用し、health fixtureの意図的503を対象API失敗と区別 |
| 既存公式provider／capability／checkpoint回帰 | 37 PASS | `tests/test_media_provider_execution.py`、`tests/test_media_provider_capability_registry.py`、`tests/test_media_provider_checkpoint_migration.py` |
| 独立レビュー | 承認失効race、旧authority、鍵不足500、X actualFile置換、YT Next/Selection競合、同document再準備、旧revision証拠の昇格を修正。prepare-only修正範囲のP0/P1/P2残件なし | read-only sourceレビューと正規reject／reapproval／lease／同device新epoch／並行claim／読取照合probes |

OpenAPIは実FastAPIから再生成し、Web/native生成型に伝搬した。Web `tsc --noEmit`、native生成型単体検査、Mobile Product Contract（13 capabilities／33 routes）はPASS。Native全体typecheckはこの環境にmobile TypeScript依存がないため未実行。PostgreSQLの実lock-waitとmigration適用、既存Windows配布物の更新、各persona/accountのlive操作は別受入であり、この表から実投稿済みと推論しない。

## 2026-10-10 Bridge専用profileの所有・排他境界

`pc_bridge/browser_profiles.py` はBridge管理下の `data/browser-profiles/{service}/{owner UUID}/{account UUID}` に専用profileを保持する。profile IDは保存し、leaseごとにbrowser context epochを更新する。通常のEdge profileやQA用helper sessionを取り込まない。取得時にCookieを読まず、browser起動・ログイン・Cookie importも行わない。manifestは閉じたschemaでservice/owner/accountを検証し、symlink/junction/UNCは拒否する。OSのプロセス間lockを保持し、owned browserの終了を確認するまで解放しない。lease終了ではbrowserの保存データを削除しない。

`pc_bridge/profile_media_preparation.py` は同じprofile path/ID、browser context、WSS epochを持つ生存driverへ既存 `media_prepare_*` だけを渡す。driverには既存X helper 9／YouTube helper 4のaccount DOM・document・本文/素材SHA照合を含む準備契約の実装が必要である。adapterはその照合を代替せず、Post/Publish/Save、generic browser操作、認証取得を提供しない。入力dispatch前にrun/phase/chunk offsetのdigestだけを永続記録し、応答喪失・payload変更・profile再取得・別transfer IDで同じ入力を再送しない。読取り専用inspectは既存台帳と同じcontext/epoch境界で行う。

サーバーはbackend名だけで準備権限を決めず、従来extensionのcontext pin、または `profile_binding` のservice/owner/account/profile ID/connection epochを確認する。専用profileの各準備command直前に再確認し、既存ContentVariantRevision→ExternalAction→human approvalと暗号checkpoint/CAS/leaseを維持する。公開DTO・DB schema・Web/Native生成型には追加がない。

専用profileの実driverは `pc_bridge/profile_browser_driver.py` と `resources/browser-profile-driver/driver.cjs`。固定したPlaywright SDKのNodeをprivate stdioで起動し、専用user-data-dirのEdgeを同じWindows Jobに所属させる。外部debugger endpointや普段のbrowserには接続しない。既存background/content/YouTube helperの正本を読み、owned pageのmain-frame loader IDとisolated worldを使うtransportとして実行する。account・document・本文/素材SHAの判定と最終Post/Publish/Saveを押さない制約は既存helperが担い、driverは成功結果を合成しない。

Windowsのprofile directoryは本人のSIDとSYSTEMだけの継承DACLで保護する。Job名とprocess作成時刻をruntime記録へ残し、親終了後の子process残存または照合不能時は再取得を拒否する。closeはinput requestと直列化し、cancelされた応答を成功扱いせず、owned Jobと全記録processの退出を確認してからleaseを解放する。任意PIDの停止や通常browserの終了は行わない。

owned pageの登録はpage単位で進行中の処理を共有し、pageイベントと明示newPage／discoverが重なってもtab IDを一つだけ作る。CDP session作成とPage.enableの各待機後にpage／contextの閉鎖を確認し、閉鎖中には登録・navigationへ進まない。未登録の失敗sessionはdetachし、明示discoverで再確認できる。登録済みpageが閉じたらtabとその選択状態を失効させる。遅延CDPで両呼出しを交差させる回帰テストは、同一windowのYouTube owner候補が重複しないこと、登録失敗後、closed page、context／stdio closeを確認する。

`managed_profile` factoryはWSS readyで認証ownerとX/YouTubeの候補だけを受け取る。候補のProject ACL・本人所有・Persona/current account revision・既存provider bindingの除外は通常のBridge connection検証を再利用する。Bridge GUIでProject・人格・アカウントを明示選択し「専用ブラウザを開く」、本人がログインして対象タブを選び、Operationsで既存preview/save/approvalを行う。再接続時にbrowserやタブを自動再取得しない。従来extension/agent-browser設定は保持し、専用profileへ認証をコピーしない。公開DTO・DB schema・Web/Native生成型は変更しない。

現受入は一つのprofileを本人が切り替えて接続する範囲に限る。複数アカウントの同時実行・自律profile切替は未対応で、完成扱いにしない。通常Chat／CrowdWorksのgeneric browser操作は準備専用profileのcommand契約に含まれない。現review branchの新設定既定をそのまま配布すると、generic browser能力の広告と実装が一致せず、通常操作が拒否される回帰がある。用途別dispatch・能力・接続状態の互換修正を親レビュー中であり、この状態を通常利用の配布受入完了とは扱わない。

新しい配布candidateはPlaywright 1.63.0とNode、固定driver/helper sourcesの同梱が必要。build後の `--verify-profile-driver` はbundleを読取りで照合し、browserは起動しない。recipient license/source inventoryのレビューと配布EXE更新、実Studioのselector確認は配布の受入対象として別途確認する。ソースと隔離fixtureのPASSだけで現配布版や実アカウントの受入完了とはしない。DLsite/FANZA/Fantiaの公開・販売対応やCookieファイルの存在による権限成立も宣言しない。
