# 会話添付を手がかりにした統合検索

保存済み会話の画像・音声・動画・本文資料を、認識設定や認識結果から独立した検索用原本として使う。会話で「この画像と音声を手がかりに探して」と依頼すると、`media_search(use_current_attachments=True)` が現在ターンの正規保存済み `TurnContext.message_id` と DB 添付配列から順序付き `SourceRef(chat_attachment, message_uuid:index)` を生成する。`chat_search_sources` は filename/index/kind/size のみを列挙し、モデルへ保存pathや推測UUIDを渡さない。

未保存・client_message_id・別会話・偽index・upload_failed・不可読原本は検索前に拒否する。明示sourceも既存 `SearchSourceResolver` の現在ACL・保存領域・hash検証を通す。query原本に検索索引のREADYは要求しない。native query準備、encoder、各検索laneは既存 `UnifiedSearchService` が所有し、この接続層に複製しない。

`media_search` / `media_read` / `chat_search_sources` は Main の `agents.search.enabled` が有効な場合に公開する。Knowledge search featureflag とは独立している。無効な検索laneや準備失敗は `lane_status` / `query_sources` / `complete` / `warnings` で示し、未検索を0件成功へ変換しない。

## 共有APIと範囲

- `POST /api/search/media`: `plan`（version/instruction/ordered sources/selector/mode/scope/targets/limit）と `use_current_attachments` / `attachment_indices` / 保存済み session_id/message_id。server budgetはclientから指定できない。
- `GET /api/search/media/attachments`: 認可した保存済み添付descriptor。
- `POST /api/search/media/evidence`: Docs/Knowledge/会話のcanonical locatorとcitationを既存reader・現在認可で取得。
- media原本は既存unit reader・content/preview routeを使い、page/time/text範囲を保つ。

`current_context` は認可済み会話から導いた現在キャラクターとProject、`project` は明示Project内の合法な全キャラクター、`space` / `all_readable` は各範囲の合法partition全体。client/model指定characterを信頼しない。検索先のscopeとquery添付の現在コンテキストは区別し、all_readableでも現在Projectの添付を解決できる。combinedは関連度用の手がかり統合で、論理ANDの保証ではない。separateは手がかり別に検索する。

会話sessionを付けたAPIのproject検索では、project_idは現在会話のProjectと一致する必要がある。現在会話とは別Projectの明示refを使う場合は、合法な範囲のspaceまたはall_readableを選ぶ。

レスポンスはnative `source_status` を公開契約の `query_sources` へ明示的に写し、`results` / `lane_status` / `complete` / `warnings` を保存する。引用はDocsの正規化本文範囲、Knowledgeの現在chunk、会話のUnicode文字範囲、mediaのpage/time/text範囲に従う。取得前の原本URLはUIへ公開しない。

Docs/会話のreaderは検索citationの`body_sha256`と現在の正規化全文のSHA-256（UTF-8）を照合する。変更済み・hash欠落は409として拒否し、古い文字offsetを現在本文へ適用しない。Webは再検索を案内し、更新したcitationで根拠を取得する。toolの`media_read(scope=..., project_id=..., space_id=...)`は検索hitの明示scopeで読込できるため、現在Project Aからall_readable/Spaceの合法なBの原本も利用できる。strictなProject制約は広げない。

接続層が内部の `QuerySource.authority = current_context | selected_scope` を決定する。DBから展開した現在添付は `current_context`、明示既存refは `selected_scope`。client/modelのsource入力は `authority` を受け付けず、同じ現在添付を明示refにも含めた場合は現在添付のauthorityを優先する。現在添付は会話Projectの実ACLで解決し、検索先Spaceフィルタを混ぜない。selected_scopeはplan.scopeの現在ACLで解決するため、all_readableでは合法な別Projectのrefを使える。strict Project制約は接続層とengineの両方で維持する。decode前・encode前後・検索後・結果ACL確認後の再認可まで同じauthorityと正規保存領域を使い、現在ACLまたは原本が変わればquery全体を失敗として返す。

Docs producerは `_prepared_originals` のoffset元全文、会話producerは無変換の `message.content` 全文からUTF-8 SHA-256を算出し、`citation.body_sha256` としてreaderへ渡す。私有の `_query_content_hash`、検索snippet、媒体fileの `content_hash` はbodyの版照合へ流用しない。Docs/会話citationへ旧 `content_hash` キーを混ぜない。

## 添付とWeb操作

選択・paste・drag/dropは同じ検証で合計12件・100 MB。各fileは50 MB、画像10 MB、音声25 MB、メール10 MB。複数audio/videoを保存して全件を検索sourceへ使える。既存の補助認識が最初のaudio/videoだけを処理する場合は明示し、保存・検索を切り捨てない。認識OFFや失敗でも保存済みnative sourceを検索できる。

初回WebSocket・編集/再実行RESTも同じ12件上限を適用する。保存済み添付配列の9〜12件を再送時に切り捨てず、13件は永続化前に拒否する。添付一覧GETが失敗した場合は空queryでも「添付一覧を再取得」から復帰し、添付chipだけで検索できる。

composerの「添付で探す」「個別に探す」は自然な会話指示を入力する。Ctrl+Fの「添付・統合検索」では保存済み添付chip、4検索先、scope、combined/separate、処理範囲、部分検索・失敗・未検索を表示する。query/scope変更、再送、取消、閉じる、session切替では古い結果・引用・原本URLを破棄し、過去queryへ戻しても復活させない。取得失敗は再試行できる。

## 検証の境界

合成SQLiteの実ACL・保存原本・既存Docs/Knowledge/会話reader、fake UnifiedSearchServiceへのnative QueryPlan接続、Webコンポーネントの対象回帰を検証する。独立AIの実Chromium確認はAPI fixtureを使い、1280px/375pxで40操作と追加scope整合を確認した。追加QAでは空queryの添付GET再取得、Docs/会話の409から再検索して更新hashで引用取得、recognition OFFの音声2/動画2を含む12件のupload/WS保存/編集/再実行RESTを両viewportで確認した。実DB＋native encoder＋統合4laneのend-to-end、Native Mobile実機の意味検索はこの検証に含めない。共有OpenAPIとWeb/Mobile生成型は同じ正本から更新する。
