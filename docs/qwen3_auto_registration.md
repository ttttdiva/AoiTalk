# Qwen3-TTS Voice Cloning - 自動登録システム

## 概要

`config/cloning/`ディレクトリに音声ファイルを配置するだけで、起動時に自動的にVoice Cloningが実行され、音声が登録されます。

## セットアップ

### 1. 依存ライブラリのインストール

```bash
pip install -r requirements-qwen3.txt
```

### 2. Gemini API キーの設定（オプション）

音声ファイルに対応する`.txt`ファイルがない場合、Gemini APIで自動文字起こしを行います。

1. [Google AI Studio](https://ai.google.dev/)でAPIキーを取得
2. 環境変数に設定:

```bash
# Windows (PowerShell)
$env:GEMINI_API_KEY="your_api_key_here"

# Linux/Mac
export GEMINI_API_KEY="your_api_key_here"
```

または`.env`ファイルに追加:
```
GEMINI_API_KEY=your_api_key_here
```

## 使い方

### 基本的な使い方

1. **音声ファイルを配置**

   `config/cloning/`ディレクトリに音声ファイルを配置します:
   
   ```
   config/cloning/
   ├── alice.wav
   ├── bob.wav
   └── charlie.wav
   ```

2. **文字起こしテキストを配置（推奨）**

   各音声ファイルと同名の`.txt`ファイルを作成し、音声の内容を記載します:
   
   ```
   config/cloning/
   ├── alice.wav
   ├── alice.txt      ← "Hello, my name is Alice."
   ├── bob.wav
   ├── bob.txt        ← "こんにちは、ボブです。"
   └── charlie.wav    ← .txtなし（自動文字起こしされる）
   ```

3. **AoiTalkを起動**

   ```bash
   python main.py
   ```

   起動時に自動的に:
   - `config/cloning/`をスキャン
   - 未登録の音声を検出
   - `.txt`がなければGeminiで文字起こし
   - Voice embeddingsを生成して`cache/qwen3_voices/`に保存

### 冪等性（重複生成の防止）

- 既に登録済みのvoiceは再生成されません
- 2回目以降の起動は高速です
- 新しい音声ファイルを追加した場合のみ処理されます

### ログ例

```
[Qwen3-TTS] Scanning cloning directory: config/cloning
[Qwen3-TTS] Found 3 audio file(s)
[Qwen3-TTS] Voice 'alice' already registered, skipping
[Qwen3-TTS] Processing new voice: charlie
[Qwen3-TTS] No transcription file found, attempting auto-transcription
[AudioTranscriber] Transcribing: config/cloning/charlie.wav
[AudioTranscriber] Transcription successful: 45 chars
[Qwen3-TTS] Saved transcription to charlie.txt
[Qwen3-TTS] Creating voice embedding for: charlie
[Qwen3-TTS] Voice 'charlie' saved successfully
[Qwen3-TTS] ✓ Successfully registered voice: charlie
```

## ディレクトリ構造

```
AoiTalk/
├── config/
│   └── cloning/              # 音声ファイルを配置
│       ├── alice.wav
│       ├── alice.txt
│       ├── bob.wav
│       └── bob.txt
├── cache/
│   └── qwen3_voices/         # 自動生成されるembeddings
│       ├── voices_index.json
│       ├── alice.pkl
│       └── bob.pkl
└── config/characters/
    └── my_character.yaml     # キャラクター設定
```

## キャラクター設定での使用

`config/characters/`のYAMLファイルで、登録したvoiceを指定:

```yaml
voice:
  engine: "qwen3tts"
  voice_name: "alice"  # config/cloning/alice.wavから生成されたvoice
  language: "Auto"
```

## 対応音声形式

- `.wav` (推奨)
- `.mp3`
- `.flac`
- `.ogg`
- `.m4a`

## トラブルシューティング

### Q: 文字起こしが失敗する

**A:** 以下を確認してください:
- `GEMINI_API_KEY`が正しく設定されているか
- 音声ファイルが破損していないか
- 手動で`.txt`ファイルを作成することも可能です

### Q: Voice登録がスキップされる

**A:** 以下の理由が考えられます:
- 既に登録済み（`cache/qwen3_voices/voices_index.json`を確認）
- `.txt`ファイルがなく、Gemini APIキーも未設定
- 音声ファイルの形式が非対応

### Q: embeddings を再生成したい

**A:** 以下の手順で再生成できます:
1. `cache/qwen3_voices/<voice_name>.pkl`を削除
2. `cache/qwen3_voices/voices_index.json`から該当エントリを削除
3. AoiTalkを再起動

## 注意事項

- **音声サンプルの品質**: 3-10秒のクリアな音声を推奨
- **Gemini API使用量**: 文字起こしはAPIコールを消費します
- **プライバシー**: 音声ファイルはGemini APIにアップロードされます（.txtがない場合）
- **ストレージ**: embeddingsは`cache/`配下に保存されます（リポジトリ内）

## 手動管理（従来の方法）

自動登録を使わず、手動でvoiceを管理することも可能です:

```bash
python scripts/manage_qwen3_voices.py --add sample.wav --name "my_voice" --text "サンプルテキスト"
```

詳細は[walkthrough.md](../../../.gemini/antigravity/brain/edc78c92-27a0-461b-968e-8ab93963a28e/walkthrough.md)を参照してください。
