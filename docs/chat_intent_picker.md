# チャットの意図選択

Web / Native Mobile のチャットで、「＋」メニューの「意図確認」をON/OFFできます。初期状態はOFFです。ONの間は通常の送信ボタンや送信キーで、実際に送る前に JEV Choice を1回だけ呼び出し、発言の意図候補を表示します。入力中の自動判定や、候補を作る事前のLLM呼出しはありません。OFFのときは通常どおり送信します。

## 操作と送信

- 固定14候補の短いラベルと推定割合を、推定スコアの高い順に表示します。同点では定義順を維持します
- たとえば「意思表明 80%」「皮肉 3%」のように、発言が何を伝えようとしているかを示します。文章の書き換え案やAIにさせる作業の選択ではありません
- 割合はモデルの候補間の推定値で、実際の意図や正しさを保証する校正済みの確率ではありません
- 候補は自動選択しません。意図を選んで「この意図で送信」を押すと、通常の送信経路を使い、原文を保持したまま本文末尾に `\n\n[意図: 選んだラベル]` を追加します。タグは履歴にも見えます。原文の言い換えや生成文への置き換えは行いません
- 意図タグはユーザーが選んだ解釈の補足です。操作の認可、ツール権限やプライバシー設定を変更しません
- 候補取得ではチャット送信・会話作成・本文の履歴保存を行いません。添付ファイルや会話履歴はJEVへ渡さず、現在の本文のみを判定します
- キャンセルや取得失敗では送信せず、本文・添付を保持します。意図確認ONのまま候補取得に失敗しても、自動で通常送信へ進みません。確認を省く場合は「＋」から意図確認をOFFにして送信できます。送信受付に失敗した場合も下書きを保持します。Nativeで既にタイムラインに保持された送信の再試行は、既存の履歴側操作を使います
- 本文、会話、プロジェクト等が変わった候補は無効になります。古い応答で新しい下書きや選択を上書きせず、連打で二重送信しません
- 送信に伴って新規会話が作成された場合は、同じ本文・添付・ユーザー・プロジェクトの候補と選択を引き継ぎます。手動の会話切替は引き継ぎません
- Webの添付送信では、再接続待ちは20秒、アップロードは120秒で打ち切ります。再接続を繰り返しても保留キュー全体は140秒を超えて延長しません。確定失敗後は本文・添付と選択を保持して再試行でき、期限後の遅延アップロード結果から送信しません。通常送信も失敗した下書きを復元し、新しい入力を上書きしません

候補（同点時の定義順）: 意思表明 / 質問 / 依頼 / 確認 / 報告 / 感情表現 / 同意 / 反対 / 感謝 / 謝罪 / 冗談 / 皮肉 / 挨拶 / その他

質問は未知の情報を尋ねる発言、確認は既にある理解や予定が合っているかを確かめる発言です。意思表明は自分の考えや意向、報告は出来事や結果を伝えます。皮肉は字面と異なる意味での遠回しな批判、冗談は笑いや軽いやり取りを狙う発言として区別します。複数の解釈があり得る場合も、最終的な意図はユーザーが選びます。

## 接続と保護

`POST /api/chat/intent-candidates` は認証必須で、`text` と任意の `session_id` / `project_id` を受けます。IDはサーバー側でアクセス権を確認し、会話とプロジェクトの組合せも検証します。応答は `candidates: [{ id, label, score }]` と `model` です。本文は4000文字・UTF-8で16000バイトまでです。

既存の `JevDecisionService` と `OutboundPrivacyGateway` を再利用し、JEVの接続先、モデル、サーバーの `JEV_API_KEY` は既存設定に従います。クライアントへAPIキーを配信しません。JEVへの外部送信には、グローバル・会話・プロジェクトのうち最も強い保護設定を適用します。local-onlyや保護確認失敗では候補を返さず、通常の送信操作は維持します。プロジェクト復元中や `/masking` の未処理本文には実行しません。

JEVの通信は8秒・自動再試行なし、FastAPIルートは本文受信・認証・会話の保護設定確認・保護確認を含めて合計18秒、候補UIは20秒で打ち切ります。本文受信には合計期限とは別に5秒の上限も適用します。認証後に合計期限を再スタートしません。保護確認が時間内に終わらない場合も許可扱いにせず中止します。同一ユーザーの同時候補リクエストを制限します。通信や保護のエラーは固定文言で表示し、本文、資格情報、providerの生エラーを表示しません。

FastAPIの504応答では、`Server-Timing` の `chat_intent_timeout` とサーバーログに固定の段階名だけを記録します。`request_body` は本文受信、`authentication` は認証、`scope` は会話・プロジェクトの設定確認、`inference` は保護確認を含む推定処理、`provider` はJEV通信の期限です。本文・ユーザーID・会話ID・資格情報は記録しません。公開経路のNext.js認証やネットワーク待ちはこのFastAPI期限の開始前にも起こり得るため、ブラウザの20秒期限とは区別します。

成功応答の `Server-Timing` には固定名 `chat_intent_body`、`chat_intent_auth`、`chat_intent_scope`、`chat_intent_inference`、`chat_intent_gateway`、`chat_intent_provider`、`chat_intent_total` のミリ秒値を含めます。`gateway` は保護・確認などの処理時間（provider送信時間を除く）、`provider` は確認後の送信・応答読込時間です。`inference` は両方に加えてclient作成や結果検証などを含むため、すべての値を足し合わせません。これらはモデル内部の推論時間を直接測るものではありません。

Webの対象proxyは既存ヘッダーを保持したまま `chat_intent_web_auth`（Next.js認証）と `chat_intent_web_total`（認証開始からproxyが応答を返すまで）を追加します。`web_total` はブラウザへの本文転送完了までの時間ではなく、公開gatewayやブラウザまでの通信を含む実測とは区別します。名前と非負の数値だけを返し、本文・ID・URL・モデル名・資格情報は含めません。計測による追加AI呼出しや期限の変更はありません。

JEV通信のtimeoutでは、既存の `chat_intent_timeout;desc="provider"` に加えて `chat_intent_timeout_kind` を返します。固定値は `connect`（接続）、`read`（読込）、`write`（書込）、`pool`（接続枠待ち）、`overall`（JEV送信全体の8秒期限）、`unknown`（分類できないtransport timeout）です。エラーJSONのcodeとUI文言は変更しません。生の例外、本文、URL、資格情報を診断へ含めません。`overall` だけで読込や接続など特定の下位処理を原因と断定しません。

意図候補requestだけはHTTPXの固定trace event名から `connect_tcp` / `start_tls` の時間を取り、timeout時には `chat_intent_transport_phase`（`connect_tcp` / `start_tls` / `connected` / `unobserved`）も返します。trace引数には触れず、host・IP・headers・SSL情報・生例外を読み取り/保存しません。`connect_tcp` はDNSやdefault executor待ちも含み得るため、DNSとTCPを単独では分離できません。

送信中だけ100ms間隔の単一timerでevent loopの遅れの最大値を測り、`chat_intent_loop_lag` として返します。終了・エラー・取消でtimerを解除し、別taskや専用threadは作りません。0msでも短いloop停止がなかった証明にはなりません。`connect_tcp` / `start_tls` は成功時のServer-Timingにも含め、timeout時の固定phase・数値はログでも確認できます。汎用JEVではこの計測は既定OFFです。

通常serverは意図候補専用のHTTP client poolを起動・停止のlifecycleへ登録します。最初の利用時に生成して、同じevent loopの複数requestで接続だけを再利用します。上限は16接続・16 keep-alive接続、idle期限は60秒です。これはrequestの8秒期限とは別の接続維持設定です。各requestのJEV adapter、privacy gateway、本文、確認結果、timing、Authorizationは共有しません。API keyはevaluateごとにserver環境から取得し、共有clientのdefault headerへ保存しません。providerのSet-Cookieを保存・送信しないため、利用者間でcookie状態も共有しません。

requestの取消や完了では借りたclient全体を閉じず、その通信を終了します。clientを閉じるのは所有するserverの停止処理です。停止後のrequestは個別clientへ自動fallbackせず、次の正常な起動で新しいclientを作ります。通常のJevDecisionService利用と既存run-scoped sessionの既定動作は維持します。接続再利用による速度改善やprovider timeout解消は、実環境の計測で確認します。

## 参考

[SlowDialog](https://github.com/gpsnmeajp/SlowDialog) の [System one Intent仕様](https://github.com/gpsnmeajp/SlowDialog/blob/main/intent_feature.md) と公式実装を参考に、AoiTalkの既存認証・プライバシー・送信経路へ独立実装しました。SlowDialogは [MIT License](https://github.com/gpsnmeajp/SlowDialog/blob/main/LICENSE)、Copyright (c) 2026 Segment (gpsnmeajp) です。元コードや素材の転載はしていません。

## 対象確認

Web: 初期OFF、＋からのON/OFF、ON時の通常送信ボタン・送信キーによる候補表示、推定待ち取消、低い順位の候補選択、送信受付失敗時の原文・添付保持、連打、本文変更・会話切替・新しい添付の保護、OFF時の通常送信を確認します。

Native: 同じ操作に加え、local会話からserver会話への昇格、別会話へ移動した後の遅延受付、新しいコマンド付き下書きの保持を確認します。APKの実機確認・公開は [Mobile更新手順](mobile-auto-update-standard.md) に従います。

接続待ちの切り分けとして、意図候補のprovider開始時にdefault executorへ引数なしの時計読取を1回だけ非同期投入します。これをawaitして送信を遅らせません。完了時は投入からworker開始までの `chat_intent_executor_queue`（ms）、timeout時は固定状態 `chat_intent_executor_probe`（completed / pending / unavailable）を記録します。本文・資格情報・contextvarsをworkerへ渡しません。request終了時に未完了Futureをcancelしますが、内部queueからの即時削除は保証しません。既に実行された場合も時計を読むだけです。pendingはworker完了通知が未処理でも起こり、単独ではexecutor飽和を断定できません。queue時間にはexecutor初期化やスケジューリングも含み得ます。
