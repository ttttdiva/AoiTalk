# Live Voice backend

AoiTalk の Live Voice は、未公開の GPT-Live API ではなく公開中の OpenAI
Realtime API に対する `openai_realtime` provider adapter です。ブラウザへ
通常の `OPENAI_API_KEY` は返しません。

## 接続方式

- ブラウザのマイク・スピーカー: WebRTC
- FastAPI: authenticated AoiTalk actor の認証、ConversationSession/TurnContext、
  AgentRun、tool permission、Transcript/audit
- Provider sideband: `wss://api.openai.com/v1/realtime?call_id=...`（標準キーは
  provider 内だけで使用）
- 通常の AoiTalk JSON `/ws` は変更せず、既存 ConnectionManager へ
  `live_voice.event` を scoped broadcast する

公式資料（作業時点の仕様）:

- [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [Webhooks and server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)

## HTTP contract

すべての route は既存 AoiTalk cookie/JWT 認証を要求し、actor はリクエストの
`user_id` ではなく `server._get_user_info_from_request()` から解決します。

1. `POST /api/live-voice/sessions`（`/session`、`/token`、`/client-secret` は互換 alias）
   - body: `conversation_session_id?`, `project_id?`, `character_name?`,
     `provider: "openai_realtime"`, `model?`, `voice?`, `instructions?`
   - response: `session`、`id/session_id/live_session_id`（runtime id）、
     `conversation_session_id`（durable id）
   - 旧クライアントが `client_secret` を body に含めても extra field として受理するが、
     値は無視し、レスポンスや runtime state へコピーしない
2. `POST /api/live-voice/sdp` または `/sessions/{live_id}/sdp`
   - JSON `{session_id, sdp, client_secret?}` または `application/sdp` body
   - server provider が公式 `/v1/realtime/calls` multipart (`sdp`, `session`) を呼び、
     SDP answer と `call_id` を返す。旧 `client_secret` JSON field は互換のため受理するが、
     unified call へ再送せず、保存・ログ出力もしない
   - `instructions` はこの route では無視し、session start 時に server が保持した値だけ使う
3. `POST /api/live-voice/sessions/{live_id}/events`
   - ブラウザから受けるのは接続・ミュート等の lifecycle telemetry のみ。transcript、
     provider response、function call/tool event は 403 で拒否し、永続化・AgentRun・
     broadcast しない
   - transcript/tool は provider sideband のサーバー内部 provenance だけが処理し、
     音声 blob は破棄、確定 transcript を既存 ConversationMessage へ保存する
   - sideband の `event_id`（または決定的 fingerprint）で idempotent に無視する
   - event/SDP body はサーバー側サイズ上限で切断する
4. `POST /api/live-voice/sessions/{live_id}/end`（`close`/`DELETE` alias）
   - AgentRun を完了し、in-process provider sideband を破棄する

## Provider/test

`src/services/live_voice_service.py` の `OpenAIRealtimeProvider` が唯一の公開
OpenAI adapter です。`MockRealtimeProvider` を `LiveVoiceService(provider=...)`
へ差し込むと credential/network なしで route、transcript、permission、sideband
tool output のテストを実行できます。

本番で `OPENAI_API_KEY` が未設定の場合は、start 時のネットワーク不要 ready check
で 503 を返して fail closed します。ローカル mock は
`AOITALK_LIVE_VOICE_PROVIDER=mock` またはテスト注入で明示的に選択してください。

Realtime の model/voice と tool はサーバー allowlist で制限します。tool allowlist
が空の場合は `tool_choice: "none"` を `session.update` へ送信し、allowlist にない
function call は permission が承認しても実行しません。sideband は call ごとに一つの
双方向 WebSocket と writer queue を保持し、tool 出力ごとの新規接続を作りません。
既定 allowlist は Docs の検索・読取・query と task の list/create/update（破壊的な
delete/file/command は非公開）で、task mutation は既存 permission を必ず通します。
actor ごとの同時セッション数は既定 1、失敗した session start には actor 単位の rate limit
を適用します。

既定の model allowlist は function calling と audio に対応する現行モデル
`gpt-realtime-2.1`、`gpt-realtime-2.1-mini`、`gpt-realtime-2`、
`gpt-realtime-1.5` のみです。旧 preview/`gpt-realtime` 名は通常拒否します。
`AOITALK_LIVE_VOICE_MODELS` を設定した場合は、その明示的な環境変数 allowlist を
優先します。

Python の公開 `EphemeralClientSecret` / `normalize_client_secret` symbol は、旧 import
を壊さないため deprecated stub として残していますが、呼出し時に 410 を返し、値を
保持・返却しません。
