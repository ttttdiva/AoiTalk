# Mage-VL動画認識

AoiTalkのWebチャットは、Microsoft `microsoft/Mage-VL` の公式オンライン推論経路に合わせ、SGLangのOpenAI互換APIへ動画フレームをまとめて送信します。ブラウザから動画本体をWebSocketへ再送せず、先にワークスペースへ保存したパスをバックエンドへ渡します。

## 既定の動作

- `mage_vl.preload_on_start: false` のため、AoiTalk起動時はモデルをロードしません。
- 動画を初めて送信したとき、`managed: true` なら `python -m sglang.launch_server ... --model-path microsoft/Mage-VL --trust-remote-code` を起動します。
- SGLangがモデルを取得するため、初回動画の解析には時間がかかります。起動中はチャットへ進捗を表示します。
- AoiTalkが起動したSGLangプロセスだけを終了時に停止します。`managed: false` では外部サーバーへ接続するだけです。

## 外部SGLangを使う場合

先にMage-VL対応のSGLangサーバーを起動し、設定画面の「動画認識」で以下を設定します。

```yaml
mage_vl:
  managed: false
  base_url: http://127.0.0.1:30000/v1
  model: microsoft/Mage-VL
model_routing:
  classes:
    video:
      provider: mage_vl
      model: microsoft/Mage-VL
      base_url: http://127.0.0.1:30000/v1
```

SGLangの起動方法・GPU要件は、Mage-VL公式リポジトリのサーバー手順に従ってください。AoiTalkの設定画面では、必要に応じて `server_command` を指定できます。

## 上限とキャッシュ

`mage_vl.max_video_bytes`、`max_video_duration_seconds`、`num_frames`、`max_pixels`、`max_new_tokens` で入力と推論量を制限できます。成功した解析だけを、動画バイト列のSHA-256・指示文・モデルルート・設定・プロンプトバージョンを含むキーで最大64件キャッシュします。エラーはキャッシュしません。
