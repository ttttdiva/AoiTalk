# TTS共通 Yomi Linter

AoiTalkは、各TTSエンジンを呼び出す直前の共通プリフライトとして
`ayousanz/yomi-linter-modernbert-ja-130m` を利用できます。既定では無効です。

このモデルは誤読リスクのある文字スパンと信頼度を検出するだけです。正しい読みを推測せず、
検出結果だけを根拠に原文をひらがな・カタカナへ置換したり、辞書へ登録したりしません。
絵文字、句読点、英数字は元のTTS入力に保持されます。

## 設定

Web UIの「設定 > 音声・読み」でON/OFF、しきい値、ログ、モデル状態を確認できます。
変更は次回のTTSから反映され、再起動は不要です。DB-backed設定の既定値は次のとおりです。

```yaml
tts:
  yomi_linter:
    enabled: false
    model_id: ayousanz/yomi-linter-modernbert-ja-130m
    device: cpu
    quantization: int8
    confidence_threshold: 0.5
    log_detections: true
```

有効にして最初の日本語TTSを実行した時だけモデルをロードします。未取得ならHugging Faceから
自動ダウンロードし、以降は`cache/yomi_linter`のキャッシュを再利用します。無効時や日本語を
含まない入力ではロードもダウンロードも行いません。依存関係だけを追加する場合は
`pip install -e ".[yomi-linter]"`を使用できます。

## 辞書とTTSポリシー

共通読み辞書は、表記、カタカナ読み、任意のアクセント型、有効状態、対象TTS、備考を保持します。
VOICEVOXとAivisSpeechでは明示登録された語を互換ユーザー辞書APIへ同期し、原文自体は書き換えません。
Irodori-TTS、MioTTS、VOICEROID、A.I.VOICE、CeVIO、Nijivoiceは初期状態で検出と記録のみです。
未登録の検出語は「未解決の誤読候補」に保存され、設定画面で確認できます。

モデル、DB、または辞書APIの初期化・推論・同期に失敗した場合は警告を残し、元のテキストで
TTS生成を継続します。
