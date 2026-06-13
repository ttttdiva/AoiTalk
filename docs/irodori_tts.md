# Irodori-TTS

AoiTalk の参照音声系ローカル TTS は Irodori-TTS に一本化されています。Qwen3-TTS の voice embedding (`cache/qwen3_voices/*.pkl`) は使用しません。

## セットアップ

```bash
pip install -e ".[audio,irodori]"
pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" descript-audiotools argbind julius pystoi torch-stoi flatten-dict markdown2 randomname importlib-resources
```

AoiTalk は Irodori-TTS の推論 runtime を `src/vendor/irodori_tts/` に同梱して使います。外部の `D:/tool/Irodori-TTS` などのローカル clone は参照しません。

`dacvae` / `descript-audiotools` は upstream の依存 metadata が AoiTalk の `qdrant-client` と protobuf 範囲で衝突するため、setup スクリプトでは Irodori 実行に必要なパッケージだけを `--no-deps` で追加します。

## 高速化設定

Irodori-TTS は既定で Sway Sampling を使います。

```yaml
tts_settings:
  irodori_tts:
    num_steps: 6
    t_schedule_mode: sway
    sway_coeff: -1.0
```

品質確認や比較のために従来の線形 schedule を使う場合は `t_schedule_mode: linear` を指定します。

## 重みの取得

ユーザーが手動で重みを探す必要はありません。初回合成時に以下を自動取得します。

- `Aratako/Irodori-TTS-500M-v2`
- `Aratako/Irodori-TTS-500M-v2-VoiceDesign`
- `Aratako/Semantic-DACVAE-Japanese-32dim`

取得済みファイルは `tts_settings.irodori_tts.cache_dir` と Hugging Face のキャッシュに保存されます。

## 参照音声

参照音声は `config/irodori_refs/` に置きます。

```text
config/irodori_refs/
  ずんだもん.wav
  ずんだもん.txt
```

キャラクターの `voice.voice_name` が `ずんだもん` の場合、`config/irodori_refs/ずんだもん.wav` を自動で探します。明示的に指定する場合は DB のキャラクター音声設定に `ref_wav` を入れます。

```yaml
voice:
  engine: irodori_tts
  voice_name: ずんだもん
  ref_wav: config/irodori_refs/ずんだもん.wav
```

参照音声なしで生成する場合は `no_ref: true` を指定します。VoiceDesign を使う場合は `voice_design: true` と `caption` を指定します。

```yaml
voice:
  engine: irodori_tts
  no_ref: true
  voice_design: true
  caption: 落ち着いた女性の声で、やわらかく自然に読み上げる
```
