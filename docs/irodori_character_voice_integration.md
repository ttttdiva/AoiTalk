# Irodori キャラクター音声設定

Updated: 2026-07-18

AoiTalkのIrodoriエンジンは `Aratako/Irodori-TTS-600M-v3-VoiceDesign` 専用です。画像から別checkpointを選ぶ実験的なCharacter Voice providerや外部サービスは統合していません。

キャラクターごとの音声は、既存の次の情報を単一モデルへ渡して設定します。

- `voice_name`: `config/irodori_refs/` から同名の参照音声を検索する名前
- `ref_wav`: 参照音声ファイル
- `ref_latent`: 事前encode済み参照latent
- `caption`: 声質、感情、話し方の説明
- `no_ref`: 参照条件を使用しない明示指定
- `seconds`: 必要な場合だけ指定する固定出力時間
- `duration_scale`: Duration Predictorの予測時間に対する倍率

`ref_wav` / `ref_latent` と `caption` は同時に利用できます。参照音声は話者性、captionは声質や演技の方向づけに使用するため、互いに矛盾しない内容を指定してください。

```yaml
voice:
  engine: irodori_tts
  voice_name: narrator
  parameters:
    ref_wav: config/irodori_refs/narrator.wav
    caption: 落ち着いた低めの声で、丁寧に説明する
    duration_scale: 1.0
```

参照なしのVoice Designでは `no_ref: true` と `caption` を指定します。
