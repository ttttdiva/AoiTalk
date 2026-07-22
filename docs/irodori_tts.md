# Irodori-TTS

AoiTalk の Irodori-TTS エンジンは、内蔵推論runtimeと次の単一モデルを使用します。外部TTSサーバーや別モデルへの切り替えは行いません。

`Aratako/Irodori-TTS-600M-v3-VoiceDesign`

## セットアップ

```bash
pip install -e ".[audio,irodori]"
pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" descript-audiotools argbind julius pystoi torch-stoi flatten-dict markdown2 randomname importlib-resources
```

推論runtimeは `src/vendor/irodori_tts/` に同梱されています。外部のIrodori-TTS cloneは参照しません。SilentCipherも `irodori` extraから導入され、生成音声へ不可聴ウォーターマークを付与します。利用できない場合はruntimeが警告を出します。

## 設定

```yaml
tts_settings:
  irodori_tts:
    hf_checkpoint: Aratako/Irodori-TTS-600M-v3-VoiceDesign
    codec_repo: Aratako/Semantic-DACVAE-Japanese-32dim
    cache_dir: cache/irodori_tts
    num_steps: 6
    t_schedule_mode: sway
    sway_coeff: -1.0
    duration_scale: 1.0
```

モデルの `model.safetensors`、tokenizer、codecは初回合成時にHugging Faceから自動取得されます。取得済みファイルは `cache_dir` 配下とHugging Face cacheから再利用されるため、手動配置は不要です。

## 条件の組み合わせ

1つのモデルで次の4モードを利用できます。

- テキストのみ: 参照音声とcaptionを指定しない
- テキスト＋参照音声: `ref_wav` または `ref_latent` を指定する
- テキスト＋caption: `caption` を指定し、参照がなければ `no_ref` として扱う
- テキスト＋参照音声＋caption: 参照と `caption` の両方を指定する

captionは声質、感情、話し方を記述します。参照音声とcaptionを同時に指定した場合も両方がモデルへ渡されます。入力テキスト内の絵文字は削除されず、スタイルや非言語表現の条件として利用されます。

参照音声は `config/irodori_refs/` に置けます。`voice_name` と同名の音声ファイルは自動検出されます。

```yaml
voice:
  engine: irodori_tts
  voice_name: ずんだもん
  parameters:
    ref_wav: config/irodori_refs/ずんだもん.wav
    caption: 明るく親しみやすい声で、少し嬉しそうに話す
```

参照音声を使わないことを明示する場合は `no_ref: true` を指定します。

## 出力時間

`seconds` を指定しない既定動作では、v3 Duration Predictorがテキスト、参照音声、captionの条件から出力時間を予測します。予測時間だけを調整する場合は `duration_scale`、秒数を固定する必要がある場合に限り `seconds` を明示します。

```yaml
voice:
  engine: irodori_tts
  parameters:
    duration_scale: 1.1
```
