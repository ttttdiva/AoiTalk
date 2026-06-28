# MioTTS

AoiTalk は MioTTS と MioCodec を同一 Python プロセス内で読み込みます。MioTTS-Inference の FastAPI サーバー、llama.cpp/Ollama/vLLM などの別サーバー起動は不要です。

## セットアップ

```bash
pip install -e ".[audio,miotts]"
```

モデル本体は初回合成時に Hugging Face から自動取得されます。取得済みファイルは `tts_settings.miotts.cache_dir` と Hugging Face のキャッシュに保存されます。

```yaml
tts_settings:
  miotts:
    model_id: Aratako/MioTTS-0.6B
    codec_model_id: Aratako/MioCodec-25Hz-44.1kHz-v2
    refs_dir: config/miotts_refs
    presets_dir: config/miotts_presets
    cache_dir: cache/miotts
    device: auto
    dtype: auto
```

`model_id` は `Aratako/MioTTS-1.2B` や `Aratako/MioTTS-1.7B` などへ変更できます。重いモデルほど初回ダウンロードとVRAM/RAM使用量が増えます。

## 参照音声

MioTTS は参照音声またはプリセットが必須です。参照音声をそのまま使う場合は `config/miotts_refs/` に wav を置きます。文字起こし `.txt` は不要で、置いてあってもAoiTalkは使用しません。

```text
config/miotts_refs/
  Mio.wav
  Mio.txt  # 任意。MioTTSでは使わない。
```

キャラクター設定では `voice_name` または `voice_id` が `config/miotts_refs/<名前>.wav` と一致すると自動で使われます。

```yaml
voice:
  engine: miotts
  voice_name: Mio
  parameters:
    temperature: 0.8
    top_p: 1.0
    max_tokens: 700
```

明示的にファイルを指定する場合は `ref_wav` を使います。

```yaml
voice:
  engine: miotts
  ref_wav: config/miotts_refs/Mio.wav
```

## プリセット

同じ参照音声を何度も使う場合は、MioCodec の global embedding をプリセット化できます。

```bash
venv\Scripts\python.exe scripts\generate_miotts_preset.py --audio config\miotts_refs\Mio.wav --preset-id Mio
```

生成された `config/miotts_presets/Mio.pt` は `voice_id` または `default_preset_id` から参照できます。

```yaml
voice:
  engine: miotts
  voice_id: Mio
```

## 注意

MioTTS の各モデルはモデルごとにライセンスが異なります。商用利用や配布に使う場合は、選んだ `model_id` と参照音声のライセンスを確認してください。
