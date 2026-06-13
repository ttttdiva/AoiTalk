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
- Use Application Commands
- Connect (音声チャンネル用)
- Speak (音声チャンネル用)
- Use Voice Activity

### Privileged Gateway Intents:
- Message Content Intent
- Server Members Intent

## 4. Bot をサーバーに招待

1. Discord Developer Portal の「OAuth2」→「URL Generator」を開く
2. Scopes で「bot」と「applications.commands」を選択
3. Bot Permissions で必要な権限を選択
4. 生成されたURLをコピーしてブラウザで開く
5. 招待したいサーバーを選択

## 5. 起動方法

### 通常起動
```bash
venv\Scripts\python.exe main.py
```

Linux/macOS の場合:
```bash
venv/bin/python main.py
```

## 6. 使い方

### AoiTalk 機能トグル
Discord Bot や VC 音声入出力は `config/config.yaml` の `runtime_features` で有効化します。
Discord から `/feature` を使うユーザーIDは、`runtime_feature_permissions.allowed_discord_user_ids` に登録します。

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
- `/mode [text/voice]` - Discord セッション内の応答モードを切り替え
- `/feature [feature] [true/false]` - AoiTalk 機能トグルを変更
- `/status` - 現在の状態を表示
- `/settings` - Discordセッション、音声、同期、会話履歴設定を表示
- `/clear` - 会話履歴をクリア
- `/nanobanana` - Nanobanana Proを検索し、生成イメージ付きで紹介
- `/setavatar [image]` - Botアイコンを変更（管理者のみ）

### Spotify コマンド
- `/spotify_auth` - Spotify認証URLを表示
- `/spotify_code [code]` - リダイレクトURLの `code` を登録
- `/search [query] [search_type] [limit]` - 楽曲/アルバム/アーティスト/プレイリストを検索
- `/play [query]` - 曲を検索して即時再生
- `/pause` - 再生を一時停止
- `/skip` - 次の曲にスキップ
- `/previous` - 前の曲に戻る
- `/queue [query]` - 曲を内部キューとSpotifyキューに追加
- `/show_queue` - 内部キューを表示
- `/clear_queue` - 内部キューをクリア
- `/remove_queue [position]` - 内部キューから指定位置の曲を削除
- `/nowplaying` - 現在再生中の曲を表示
- `/playlists [limit]` - ユーザーのプレイリスト一覧を表示
- `/create_playlist [name] [description] [public]` - プレイリストを作成
- `/play_playlist [uri]` - プレイリストを再生
- `/queue_playlist [uri] [shuffle]` - プレイリストをキューに追加

> ⚠️ スラッシュコマンドが見つからない場合は、Botを再起動して `config/config.yaml` の `discord.sync_commands: true` による同期ログを確認してください。`discord.sync_command_scope: guild_and_global` ではギルドコマンドが即時反映され、グローバルコマンドはDiscord側の反映待ちになります。

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
