# Gemini TTS

Gemini API の音声生成モデルを、AoiTalk のキャラクター音声エンジン `gemini_tts` として使う。通常チャットの読み上げ、Live Voice（`realtime_character_tts`）、Native Mobile の音声会話（`POST /api/mobile-voice/speech`）、Discord Bot は同じ `TTSManager` とキャラクター音声設定を共有する。

実装: `src/tts/engines/gemini_tts_engine.py`、`src/tts/manager.py`、`src/tts/engine_bootstrap.py`、`src/api/routes/tts_provider_routes.py`、`frontend/src/components/settings/gemini-voice-settings.tsx`。

## モデル

| モデルID | 用途 |
| --- | --- |
| `gemini-3.8-flash-lite-tts` | 既定。低遅延・低コスト。通常会話と音声会話向け |
| `gemini-3.8-flash-tts` | 高表現力。演技・感情表現・キャラクター性を重視する場合 |

既定は `tts_settings.gemini_tts.model`、キャラクターごとの上書きは `voice_parameters.model`。1つのエンジンでモデルをリクエスト単位に切り替えるため、モデル違いのキャラクターが混在しても再初期化しない。一覧にない将来のモデルIDも `voice_parameters.model` に書けば使える。

## 認証

APIキーは `.env` の `GEMINI_API_KEY`（または `GOOGLE_API_KEY`）から読む。LLMの Gemini プロバイダーと同じキーを共有し、キャラクター設定・DB・ソースコードには保存しない。キーがない場合、エンジンは初期化失敗として扱い、設定画面に警告を出す。

## キャラクター設定

WebUI の「設定 → キャラクター → 音声」で音声エンジンに `Gemini TTS` を選ぶ。

| 項目 | 保存先 | 内容 |
| --- | --- | --- |
| モデル | `voice_parameters.model` | 空ならサーバー既定 |
| ボイス | `voice_id` | 30種の組み込みボイス名（`Kore`, `Puck` など）、または Voice Design / Voices API で作成した `voice_...` ID |
| 読み上げスタイル | `voice_parameters.style` | 話し方・感情・テンポの自然文指示。Interactions API の `speech_metadata.style` として送り、本文としては読まれない |
| 言語 | `voice_parameters.language_code` | Gemini 3.8 TTS は入力言語を自動判定する。設定値は既存設定との互換性のため保持するが、未対応の `languageCode` はAPIへ送らない |

`voice_name` は表示名として残り、Gemini には送らない。`speaker_id`、`speed`、`pitch` などの数値パラメータは Gemini TTS に対応する項目がないため無視する。話速はスタイル（例:「少しゆっくり」）で指定する。

「生成して試聴」は保存前の設定で `POST /api/tts/gemini/preview` を呼ぶ。試聴も Gemini API の利用料金の対象になる。

キャラクター設定はDBが正本で、発話ごとに再読込される。音声会話中に別エンジンから Gemini TTS へ切り替えても、プロセスの再起動なしで次の発話から適用される。

YAML でキャラクターを定義する場合:

```yaml
voice:
  engine: gemini_tts
  voice_id: Sulafat
  parameters:
    model: gemini-3.8-flash-tts
    style: 落ち着いた優しい声で、語尾はやわらかく
    language_code: ja
```

2人の掛け合いを1回で生成する場合は `parameters.speakers` に `[{speaker: A, voice: Puck}, {speaker: B, voice: Kore}]` のように2人分を指定し、本文を `A: ...` / `B: ...` の形式にする（WebUI には未露出）。

## サーバー設定

`app_config_settings`（既定値は `src/config_defaults.py`）の `tts_settings.gemini_tts`:

```yaml
tts_settings:
  gemini_tts:
    model: gemini-3.8-flash-lite-tts
    voice: Kore
    style: ""
    language_code: ""
    timeout: 180.0
    max_chars_per_request: 600
```

`max_chars_per_request`（既定600文字）を超える文章は句点・改行で分割して順に生成し、1つのWAVへ連結する。実APIで1500文字前後の単一リクエストが数分待ちになるケースを確認したため、既定値を600にして会話中のタイムアウトを抑える。Gemini 3.8 TTS の入力言語は自動判定で、`language_code` に `ja` を保存しても `languageCode` として送信しない。

## 通信と出力

- `POST https://generativelanguage.googleapis.com/v1beta/interactions`。`input[].content[]` に本文と `speech_metadata` を置き、`generation_config.speech_config` でボイスを指定する。
- 単発応答は `response_format.mime_type=audio/wav` を指定し、`output_audio.data`（または `steps[].content[]` の audio item）をBase64デコードする。WAVと `audio/l16` の両方を受け取り、内部で24kHz / 16bit / mono PCMへ正規化して1つのWAVへ連結する。
- 送信は `OutboundPrivacyGateway.execute` を通る外部送信（provider `gemini`、tool `tts.gemini`）。自動リトライとリダイレクト追従はしない。`docs/security/external_egress_inventory.json` に登録済み。
- Enterprise プロファイルは `tts_output` が無効のため使用しない。
- 誤読検出（Yomi Linter）の既定ポリシーは `detect_only`。

## 回帰テスト

Python: `tests/test_gemini_tts_engine.py`（Interactions APIリクエスト構築、WAV/PCM抽出、分割、リトライ無効、キャラクター設定の反映、ブートストラップ、ホットスイッチ、試聴API）。

Web: `frontend/src/components/settings/__tests__/gemini-voice-settings.test.tsx`。

実APIを使う確認は `GEMINI_API_KEY` を設定したローカル環境で、キャラクター設定画面の試聴と Mobile の音声会話で行う。
