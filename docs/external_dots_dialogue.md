# AoiTalkから本人のDOTSへ話しかける経路

対象Task: dbb98e4f-a25b-45d0-aa4a-37f53edbf4e3。対象は外部ChatGPT DOTSであり、AoiDots Agent、汎用OpenAIモデル、Slack botを表さない。

## 実装したソース契約

- src/memory/models/dots_dialogue.py にowner binding、principal fingerprint、dedicated接続用のnullable immutable conversation ID、subscription、依頼、outbox、回答receiptを保存するモデルを追加した。
- src/services/dots_dialogue_service.py にowner-confirmed binding、owner会話への受付、冪等性、期限/取消し/revocation/generation fencing、連続ACK cursor、署名secret rotation、相関済み最終返信を追加した。専用接続の固定conversation IDは初回owner確認時に保存され、再確認・設定変更で付け替えられない。送信user messageと返信assistant messageは同一conversationに一度ずつ保存する。DOTS replyやsystem更新からowner inbox eventは発生しない。dispatcher候補をlock後に再評価し、他workerのlive leaseを取消さない。外部read/delivery時にも現在の会話ownerとsoft-delete ACLを再確認する。回答済みrequestの状態は配送replay失敗で後退しない。
- src/api/routes/dots_dialogue_routes.py はcookie認証されたAoi ownerからのbinding/request/status/cancel APIを定義する。owner IDはrequest bodyから取らない。binding確認ではowner sessionとserver-owned MCP principalを照合する。src/api/routes/dots_dialogue_chat_routes.py は固定conversationに限る `POST /api/conversations/{conversation_id}/dots-dialogue/messages` を提供し、bodyからowner/binding/conversationを受け取らない。履歴取得は既存のconversation messages routeを使う。
- plugins/AoiTalk_Plugin/aoitalk_plugin/aoitalk_dots_events.py はMCP SDKのpublic Extension / MethodBinding / ToolBinding seamでEvents三methodと限定read/reply toolsを定義する。read/replyの厳格なPydantic request schemaは実ToolBindingに接続し、長さ・未知fieldをbackend到達前に拒否する。top-level events capabilityはSDK 2.3.0の通常wire型で捨てられるため、public ServerMiddlewareでSDK serialization後に加える。reply toolはconversation IDを受け取らず、requestから解決する。
- dots_events_transport.py はStandard Webhooks署名、callback challenge、HTTPS/public-unicast検証（multicastを明示拒否）、接続時のIP pin、hostname TLS確認、redirect禁止、response/event size制限、旧新secretの短いrotation、最大5回のretry dispatcher境界を実装する。callback検証failureはMCP `-32015`と列挙済み安全reasonへ写像し、peer入力を返さない。自動workerから呼ばず、署名secretは暗号化列から要求時だけ読み込む。
- alembic/versions/20261024_0001_external_dots_dialogue.py と alembic/versions/20261026_0001_dots_dialogue_dedicated_conversation.py はmigration sourceのみ。実DBへ適用していない。後者は専用bindingのimmutable conversation列を追加し、既存OAuth bindingをnullableのまま保つ。testではisolated in-memory SQLiteを使う。

主なAPI契約は POST /api/dots/dialogue/bindings、GET /api/dots/dialogue/bindings、DELETE /api/dots/dialogue/bindings/{binding_id}、POST /api/dots/dialogue/requests、GET /api/dots/dialogue/requests/{request_id}、DELETE /api/dots/dialogue/requests/{request_id}、専用Chat送信の POST /api/conversations/{conversation_id}/dots-dialogue/messages。公開API型の生成、contracts/openapi/fastapi.json、Web/Native生成型更新は別ownerとの統合作業に渡す。UIも追加していない。

## 専用stdio/Tunnel接続とdefault-off境界

通常の正規Plugin entryは従来どおり35 toolsで、Events Extensionは追加されない。明示opt-inされた別instanceだけが同じ `aoitalk_plugin.stdio` entryから既存の専用stdio/Tunnel runtimeで起動し、既存35 toolsにread/reply 2 toolsとEvents capabilityを加える。新しいHTTP MCP listenerは作らない。Plugin builderは固定app/plugin/Tunnel/runtime/profile/owner値、既存Tunnel ID一致、Conversation UUID、個別capabilityがそろわない場合にdefault serverへfail-closedする。OAuth AccessTokenやissuer/subjectは作らず、synthetic test contextもproductionでは使わない。

PluginとAPIは `plugins/AoiTalk_Plugin/.env` の `AOITALK_DOTS_DIALOGUE_DEDICATED_ENABLED`、`AOITALK_DOTS_DIALOGUE_DEDICATED_CONVERSATION_ID`、`AOITALK_DOTS_DIALOGUE_DEDICATED_CAPABILITY` と既存 `HOME_MCP_AOITALK_TUNNEL_ID` を読み取る。secret値はログ・status・request bodyへ出さない。API bridgeは専用capabilityだけを検証し、owner routeとChat send routeは通常のAoi owner sessionを別に必須とする。MCP header、body、session IDからprincipalやownerを作らない。capabilityはTunnel runtime keyやbootstrap admin credentialと別物である。

専用principal fingerprintは固定接続とownerを表し、Conversation UUIDは初回owner-confirmed bindingで `dots_dialogue_bindings.dedicated_conversation_id` に一度だけ保存する。再確認によるgeneration rotationはせず、別Chat設定での再bindingは409で拒否する。dedicated API send routeは固定chat以外を拒否し、既存のGET conversation messages routeでuser requestと相関assistant replyを読める。dispatcher queryもfingerprintとConversation UUIDの両方に限定し、既存lease/SSRF callback senderを再利用する。

API routeは既存 `dots_dialogue.enabled=true` と非Enterprise gateに加えて有効な専用設定がある場合のみ有効になる。Settings defaultはOFF。owner bindingはnormal Aoi login後の明示確認、Conversation UUID設定、secret配置、Alembic migration適用が完了するまで使えない。migrationの正規適用入口は `venv/Scripts/python.exe scripts/init_db_schema.py`。このcandidateでは実設定、migration、DB、runtime、Tunnel、Plugin rescanは変更していない。

Event本文には依頼本文を含めず、request/conversation/binding/generation IDと期限付きtokenだけを入れる。期限切れ・取消し・revocation後にin-flight callbackが到着してもread/replyは拒否される。Callback challengeはAPI bridgeの既存検証経路だけで一回行い、direct bridge callerも検証を迂回できない。

これはapp+Tunnelの固定接続をユーザーが承認した構成であり、DOTS provider発行のOAuth subject/issuerを証明する仕組みではない。app-infoのauthentication=Noneに対してOAuth identityを偽造しない。ユーザーが承認したこのconnection capabilityとAoi owner-confirmed bindingをサーバーで対応付ける。OpenAI Eventsのhostが実際にsubscribeすること、本人DOTS chatからreplyすることはlive確認が必要である（[OpenAI Developers: MCP Events](https://developers.openai.com/plugins/build/mcp-events)）。

現在のcandidateにコード・offline testsはあるが、確認用Chat作成、Plugin/Tunnel設定追加、migration、host登録、Events subscribe、外部callback、実replyはいずれも未実施。canonical API/Pluginへの適用はintegration担当のREADY解除後に行う。

## Verification

- Root Python 3.12 environment: `tests/test_dots_dialogue_routes.py`, `tests/test_dots_dialogue_bridge_dispatcher.py`, `tests/test_dots_dialogue_private_composition.py`, `tests/test_dots_dialogue_dedicated_connection.py` — 26 passed。
- Plugin専用Python 3.12 environment: `tests/test_aoitalk_dots_events.py`, `tests/test_dots_dialogue_http_backend.py`, `tests/test_dots_dialogue_private_http.py`, `tests/test_dots_dialogue_dedicated.py` — 24 passed。
- Root Ruff: すべての変更Python source/test/migrationをcheckしPASS。
- SQLite memory + private ASGI fixtures + fake callbackだけを使った。通した条件はowner binding固定、conversation変更拒否、body/header/session spoof拒否、wrong owner/chat、idempotent send、callback challenge一回、read/reply、ConversationRepository履歴、revocation/cancel、dispatcherのconversation scope。既存dispatcher testsでmultiworker lease/restart/uncertain deliveryも再実行済み。

SQLite interleaving fixtureは状態遷移を決定的に検査するが、SQLiteは `FOR UPDATE` を実行しない。PostgreSQL上の同時transaction、deadlock、restart耐久性はNOTRUN。PostgreSQL row-lock句のcompile確認と実DB concurrencyを混同しない。

実DB、AoiTalk API、OpenAI/Tunnel、ChatGPT、DOTS本人chatへ接続していない。migration未適用。成功条件はsource・合成contractだけで、実往復は未検証。

## READY後の次のruntime/host checkpoint

integration担当のrelease後、以下を順番に行う。canonical source/profileは現在のintegrationが完了するまで変更しない。

1. Normal Aoi Chat UI/APIでowner `118eb287-43cf-43aa-bb10-c8cabaf21b0f` のため新規confirmation conversationを1件作成し、そのUUIDだけを専用configへ設定する。incident/Muse conversationは使わない。
2. 既存標準migration入口 `venv/Scripts/python.exe scripts/init_db_schema.py` で追加列を適用する。直接DB書込や手作業DDLはしない。
3. 同じprivate Plugin `.env`へ専用enabled flagとConversation UUIDを置き、`AOITALK_DOTS_DIALOGUE_DEDICATED_CAPABILITY` は `secrets.token_urlsafe(48)` 相当の新規ランダム値を生成してsecretとして保存する。値は出力・貼付しない。Tunnel runtime API keyやbootstrap admin credentialを代用しない。
4. 通常のAoi owner sessionから `/api/dots/dialogue/bindings` で明示確認し、返されたbindingと保存されたConversation UUIDが一致することを確認する。その後 `dots_dialogue.enabled=true` を通常のAoi config操作で有効にする。
5. 正規Plugin server rescan後、Hostが実際に `events/list` と `events/subscribe` を同一stdio/Tunnel instanceへ送ることを確認する。hostがEvents subscribeを出さなければ、必要な具体的host登録操作を確認して止める。callbackの challenge確認後にだけ、指定Chatへ一件送信し、同じconversationのassistant reply/historyを確認する。不確かな送信は再送しない。

ここまでのcandidateで実施したのはoffline source/tests/docsだけ。確認用Chat作成、secret設定、migration、host登録、subscribe、callback、実返信、production restart/buildは未実施。
