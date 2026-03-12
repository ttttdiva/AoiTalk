# Discord Bot セットアップガイド

このガイドでは、AoiTalkをDiscord Botとして動作させるための設定方法を説明します。

## 1. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセス
2. 「New Application」をクリックしてアプリケーションを作成
3. 左メニューから「Bot」を選択
4. 「Add Bot」をクリックしてBotを作成
5. 「Token」セクションの「Copy」ボタンでトークンをコピー

## 2. 環境設定

1. `.env.sample` を `.env` にコピー：
   ```bash
   cp .env.sample .env
   ```

2. `.env` ファイルを編集して、Discord Bot トークンを設定：
   ```
   DISCORD_BOT_TOKEN=your-discord-bot-token-here
   ```

## 3. Bot の権限設定

Discord Developer Portal で以下の権限を設定します：

### Bot Permissions:
- Send Messages
- Read Message History
- Connect (音声チャンネル用)
- Speak (音声チャンネル用)
- Use Voice Activity

### Privileged Gateway Intents:
- Message Content Intent
- Server Members Intent

## 4. Bot をサーバーに招待

1. Discord Developer Portal の「OAuth2」→「URL Generator」を開く
2. Scopes で「bot」を選択
3. Bot Permissions で必要な権限を選択
4. 生成されたURLをコピーしてブラウザで開く
5. 招待したいサーバーを選択

## 5. 起動方法

### 通常起動
```bash
python main.py --mode discord
```

### デバッグモード（トークンなし）
```bash
python main_debug.py --mode discord
```

## 6. 使い方

### テキストチャット
- Botをメンションして話しかける: `@AoiTalk こんにちは`
- 画像も一緒に送信可能

### 音声チャット
- `/join` - ボイスチャンネルに参加
- `/leave` - ボイスチャンネルから退出
- ボイスチャンネルで話すと自動的に応答

### その他のコマンド
- `/help` - ヘルプを表示
- `/character [name]` - キャラクターを変更
- `/mode [text/voice]` - モードを切り替え
- `/nanobanana` - Nanobanana Proを検索し、生成イメージ付きで紹介

> ⚠️ `/character` や `/nanobanana` などのスラッシュコマンドが見つからない場合は、`.env` に `DISCORD_SYNC_COMMANDS=true` を一時的に設定するか、`config/config.yaml` の `discord.sync_commands` を `true` にしてBotを再起動し、コマンド同期を実行してください。

### セッション記憶
- 同じユーザーが同じサーバーで話しかけると過去の会話を自動的に再読み込みします。Botを再起動しても対話の流れが繋がります。
- `config/config.yaml` の `discord.memory_prefill_message_count` で復元する履歴数を調整できます（デフォルト12）。
- 大量の履歴は `discord.max_history_length` を超えた分から自動的に要約され、メモリDBに保存されます。

## トラブルシューティング

### トークンエラー
- トークンが正しくコピーされているか確認
- トークンの前後に余分なスペースがないか確認
- トークンが無効化されていないか確認

### 接続エラー
- インターネット接続を確認
- ファイアウォールやプロキシの設定を確認
- Discord APIのステータスを確認

### 権限エラー
- Botに必要な権限が付与されているか確認
- サーバーのロール設定を確認
