# AoiTalk の Irodori-TTS

このページは、AoiTalk に組み込まれている Irodori-TTS の使い方と運用上の注意をまとめたものです。現在の既定値は **Irodori-TTS v4.1 Small** です。

## 現在のモデルと vendored runtime

- 既定 checkpoint: `Aratako/Irodori-TTS-v4.1-Small`
- キャラクター selector: `v4.1-small`（既定） / `v3-voice-design`
- v3 VoiceDesign checkpoint: `Aratako/Irodori-TTS-600M-v3-VoiceDesign`
- codec: `Aratako/Semantic-DACVAE-Japanese-32dim`
- runtime: `src/vendor/irodori_tts/` に同梱（実行時に `D:/tool/Irodori-TTS` などの外部 checkout は参照しません）
- vendored の基準 upstream commit: [`8224dafb46d0aba89209a8f905f1cb7e3299d9c1`](https://github.com/Aratako/Irodori-TTS/tree/8224dafb46d0aba89209a8f905f1cb7e3299d9c1)

v4.1 は text・参照音声・caption を一つの checkpoint で扱い、duration predictor による出力時間の自動推定、複数参照の順序付き連結、絵文字を含むスタイル制御に対応します。v3 以前を廃止したわけではありません。キャラクターの `voice_parameters.irodori_model` に上記 selector を保存すると、通常読み上げと試聴が同じ checkpoint を使います。`hf_checkpoint` に明示した v2/v3・ローカル checkpoint は selector より優先してそのまま使用でき、`Aratako/Irodori-TTS-600M-v3-VoiceDesign` と従来の `ref_wav` / `ref_latent` も互換経路として残っています。旧設定の `voice_design_checkpoint` は、主 checkpoint が空の場合だけ `hf_checkpoint` に移行されます。

公式資料:

- [Irodori-TTS upstream](https://github.com/Aratako/Irodori-TTS)
- [Irodori-TTS-v4.1-Small model card](https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small)
- [Semantic-DACVAE-Japanese-32dim model card](https://huggingface.co/Aratako/Semantic-DACVAE-Japanese-32dim)
- [DACVAE upstream](https://github.com/facebookresearch/dacvae)

## セットアップと依存関係

プロジェクトは Python **3.12 以上**です。Irodori を使う場合は audio と irodori extra を同じ仮想環境へ入れてください。

### Windows

管理者権限の PowerShell またはコマンドプロンプトで、リポジトリ直下のセットアップを実行します。

```bat
setup.bat
```

`setup.bat` は NVIDIA Windows では先に公式 cu128 index から `torch==2.10.0` / `torchaudio==2.10.0` を導入してから、`python -m pip install -e ".[audio,windows,test,irodori,yomi-linter]"` を実行します。全依存導入後にも CUDA build を検証し、NVIDIA 環境へ CPU-only torch が残っていればセットアップを失敗させます。NVIDIA GPU がない Windows では通常の CPU 対応 resolver を使います。PyTorch の CUDA化は Whisper の device policy を変更せず、Whisper は CPU/fp16 false のままです。`dacvae` は固定 commit `414c20785fc3a28373073ea8ef7a1316eeeaca6e`、`descript-audiotools` は 0.7.2 をどちらも `--no-deps` で入れ、必要な runtime import パッケージを明示的に追加します。これは `descript-audiotools` の `protobuf<3.20` が AoiTalk/mem0 の `protobuf>=5.29.6` と衝突するためで、既存の protobuf を pip に置き換えさせないための手順です。既存の仮想環境で手動更新する場合は、同じ順序で次を実行します。

```bat
venv\Scripts\activate
python scripts\windows_torch_setup.py install
python -m pip install -e ".[audio,irodori]"
python -m pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae@414c20785fc3a28373073ea8ef7a1316eeeaca6e"
python -m pip install --no-deps descript-audiotools==0.7.2
python -m pip install absl-py argbind einops ffmpy ipython julius librosa markdown2 matplotlib flatten-dict importlib-resources pyloudnorm pystoi randomname rich scipy soundfile tensorboard torch-stoi
python scripts\windows_torch_setup.py verify
```

Windows の PC スピーカー録音には audio extra の `PyAudioWPatch` が必要です。これはマイク入力ではなく、Windows WASAPI の **render-loopback（再生中のシステム出力）**を取得するための依存です。

### Linux / WSL / macOS

通常の `setup.sh` は巨大な audio 依存を入れません。Irodori を使う場合だけ、リポジトリ直下で次を実行します。

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

または既存の venv に手動で次を実行します。

```bash
source venv/bin/activate
python -m pip install -e '.[audio,irodori]'
python -m pip install --no-deps \
    'dacvae @ git+https://github.com/facebookresearch/dacvae@414c20785fc3a28373073ea8ef7a1316eeeaca6e'
python -m pip install --no-deps descript-audiotools==0.7.2
python -m pip install \
    absl-py argbind einops ffmpy ipython julius librosa markdown2 matplotlib \
    flatten-dict importlib-resources pyloudnorm pystoi randomname rich scipy \
    soundfile tensorboard torch-stoi
```

`irodori` extra には DACVAE を含めず、`huggingface-hub`、`transformers`（ModernBERT 対応）、`torch` / `torchaudio`、`torchcodec`、`safetensors`、`soundfile`、`sentencepiece`、`silentcipher` などの Irodori 固有依存だけを含めます。DACVAE と `descript-audiotools` は上記の no-deps + 明示 runtime imports の経路で一度だけ導入します。Windows NVIDIA 環境では PyTorch 2.10.0/cu128 が正常系ですが、Linux/WSL の setup は別方針です。Irodori の合成自体は CPU / CUDA / MPS / XPU に対応しますが、GPU が利用できない場合はアダプターが CPU にフォールバックします。

### 公式 upstream と AoiTalk の依存レンジ

公式 upstream の基準 commit [`pyproject.toml`](https://github.com/Aratako/Irodori-TTS/blob/8224dafb46d0aba89209a8f905f1cb7e3299d9c1/pyproject.toml) は、`transformers>=5.12.1,<6` と `sentencepiece<0.2`（metadata 上は `>=0.1.99,<0.2`）を要求します。一方、AoiTalk は Irodori だけでなく MioTTS/yomi と同じ venv で動かすため、`pyproject.toml` の採用レンジを次のようにしています。

- `transformers>=4.57.6,<6`（MioTTS/yomi-linter の `<5` 制約も同時に選ぶ場合、resolver が実際に選べる上限は `<5`）
- `sentencepiece>=0.2.1,<0.3`

この差分は upstream の v4.1 対応を取り下げるものではなく、既存の MioTTS/yomi 互換性を保つための統合上の選択です。vendored runtime の [`model.py`](../src/vendor/irodori_tts/model.py) には transformers 4.x と v5 checkpoint の差分を吸収する ModernBERT shim があります。`no_init_weights` は `transformers.initialization` からの import を試し、4.x では `transformers.modeling_utils` へフォールバックします。また checkpoint の `rope_parameters.full_attention` / `sliding_attention` にある `rope_theta` を、transformers 4.x の `global_rope_theta` / `local_rope_theta` へ変換します（transformers 5 の native 表現は変更しません）。

依存検証の範囲にも注意してください。`.[irodori,miotts,yomi-linter]` 全体の `pip install --dry-run` は、既存の base dependency にある `openai` / `mem0ai` の競合で失敗するため、全 extra の dry-run 成功を Irodori の検証結果とはみなしません。Irodori の確認は `pyproject.toml` の Irodori-specific extras、setup script の no-deps DACVAE + 明示 runtime imports、vendored runtime の import/compile（`tests/test_irodori_vendor.py`）、およびこのページのローカル smoke の範囲で行います。MioTTS/yomi を同時に使う場合は上記の統合レンジを優先し、全 extra の依存解決を合否判定にしないでください。

## 設定

既定設定（`src/config_defaults.py`）は次のとおりです。`max_ref_seconds: null` は checkpoint の metadata を使う指定で、v4.1 の参照上限 120 秒を選びます。

```yaml
tts_settings:
  irodori_tts:
    hf_checkpoint: Aratako/Irodori-TTS-v4.1-Small
    codec_repo: Aratako/Semantic-DACVAE-Japanese-32dim
    refs_dir: config/irodori_refs
    model_device: cuda
    codec_device: cuda
    model_precision: fp32
    codec_precision: fp32
    use_gpu: true
    num_steps: 40
    t_schedule_mode: linear
    sway_coeff: -1.0
    duration_scale: 1.0
    max_ref_seconds: null
    ref_normalize_db: -16.0
    ref_ensure_max: true
```

### キャラクターごとのモデル選択

設定画面の「Irodori-TTSモデル」で選んだ値は、既存の `characters.voice_parameters` JSON に保存されます。selector 未指定の既存キャラクターは v4.1 Small として扱います。

```json
{
  "irodori_model": "v3-voice-design",
  "caption": "明るく親しみやすく話す",
  "irodori_reference_assets": []
}
```

`irodori_model` から具体的な checkpoint への解決はバックエンドが一元管理します。既存の `hf_checkpoint`、ローカル checkpoint、`voice_design_checkpoint` がある場合は互換性のため selector より優先されます。モデルを v4.1 → v3 → v4.1 と切り替えても、プロセス内の単一 runtime cache が要求された checkpoint をロードし直します。

既存の v3 VoiceDesign を明示的に使う場合は、checkpoint だけを置き換えます。明示値を既定 v4.1 へ上書きする処理はありません。

```yaml
tts_settings:
  irodori_tts:
    hf_checkpoint: Aratako/Irodori-TTS-600M-v3-VoiceDesign
```

### 初回の自動取得と cache

エンジンの `initialize()` は runtime の import とディレクトリ作成だけを行い、重いモデルを読みません。最初の合成時に次を順に行います。

1. Hugging Face から checkpoint の `model.safetensors` と同梱 tokenizer を snapshot 取得します。
2. codec の `weights.pth` を取得します。
3. tokenizer、model、codec をメモリへロードし、プロセス内 runtime cache を作ります。

各 runtime は Hugging Face の標準 cache 解決規則に従います。AoiTalk は `HF_HOME` / `HF_HUB_CACHE` 等の process-global 環境変数を上書きせず、loader へ独自 `cache_dir` も渡しません。起動前から設定されている標準 env があれば Hugging Face library がそのまま利用します。同じ checkpoint・device・precision の合成では runtime を再利用するため、2 回目以降はダウンロードしません。ネットワークと書き込み権限が必要です。

取得サイズの目安（2026-08 時点の公式 Hub ファイル）:

| ファイル | サイズの目安 |
| --- | ---: |
| v4.1 `model.safetensors`（0.8B params） | 約 3.06 GB（2.85 GiB） |
| v4.1 tokenizer 2 ファイル | 約 6.7 MB |
| `weights.pth` codec | 約 430 MB（410 MiB） |
| 合計 | 約 3.5 GB（3.25 GiB） |

初回は数 GB のダウンロードに加えてモデルロード・codec 初期化・GPU 転送が行われるため、回線と GPU により数分以上待つことがあります。起動直後に合成を連打せず、ログの checkpoint / codec 取得完了を待ってください。ディスク空き容量は合計サイズより十分に確保し、途中で停止した場合は cache の空きを確認して再試行します。

## 入力条件の組み合わせ

`IrodoriTTSEngine.synthesize()` と vendored runtime は次の組み合わせを受け付けます。

| text | 参照 | caption | 用途 |
| --- | --- | --- | --- |
| 必須 | なし | なし | text のみ。内部では `no_ref=True` として扱います |
| 必須 | あり（`ref_wav` / `ref_wavs` または latent） | なし | 参照話者の voice cloning |
| 必須 | なし（`no_ref: true`） | あり | caption だけの Voice Design |
| 必須 | あり | あり | 話者性を参照音声、感情・話し方を caption で指定 |

参照音声と caption は同時に渡せます。caption は参照音声と矛盾しない声質・演技を記述してください。`no_ref: true` と参照を同時に指定すること、waveform（`ref_wav*`）と latent（`ref_latent*`）を混ぜることはできません。

### 複数参照と時間制限

- `ref_wavs` / `ref_latents` は入力順を保持したリストです。各 clip を個別に encode してから latent を順に連結します。
- v4.1 の combined reference 上限は **120 秒**です。長すぎる場合は runtime が連結結果を上限まで trim します。
- 同じ話者の短くてきれいな clip を複数使い、合計 **約 30 秒**をまず推奨します。1 本の連続した長録音も受け付けますが、公式評価は複数の短い発話を連結した形式です。
- GUI の資産サービスは各ファイルと合計の両方を 120 秒以内に検証します。

### 出力時間

v4.1 は `seconds` を省略すると text と有効な条件から duration predictor で出力時間を推定します。`duration_scale` で推定値を倍率調整し、厳密な長さが必要な場合だけ `seconds` を指定します。アダプターの生成時間 `max_seconds` 既定値は 30 秒です。旧 checkpoint に duration predictor がない場合は runtime が 30 秒へフォールバックします。

## 既存のずんだもん音声でローカル smoke

現在のローカル環境に [`config/irodori_refs/ずんだもん.wav`](../config/irodori_refs/ずんだもん.wav)（mono / 44.1 kHz / 約 3.84 秒）が存在する場合は、検証用の参照音声として利用できます。この WAV は `*.wav` の gitignore 対象であり、clone に必ず含まれる追跡資産ではありません。ファイルがない環境では、自分で用意した参照音声のパスへ以下の例を置き換えてください。モデルを実際に取得して最初の合成まで確認する場合は、依存を入れた venv で次を実行します。初回は前述の数 GB 取得と長い待ち時間が発生します。

```powershell
@'
import asyncio
from pathlib import Path
from src.tts.engines.irodori_tts_engine import IrodoriTTSEngine

async def main():
    engine = IrodoriTTSEngine(use_gpu=True)
    if not await engine.initialize():
        raise SystemExit("Irodori runtime の初期化に失敗しました")
    try:
        audio = await engine.synthesize(
            "こんにちは、ずんだもんです。Irodori-TTS v4.1 のローカル smoke です。",
            ref_wav="config/irodori_refs/ずんだもん.wav",
            caption="明るく親しみやすく、自然に話す",
        )
        if not audio:
            raise RuntimeError("音声が生成されませんでした")
        Path("temp/irodori_smoke.wav").parent.mkdir(parents=True, exist_ok=True)
        Path("temp/irodori_smoke.wav").write_bytes(audio)
        print("temp/irodori_smoke.wav を生成しました")
    finally:
        await engine.cleanup()

asyncio.run(main())
'@ | venv\Scripts\python -
```

### 実モデル v4.1 smoke 実績

RTX 5090 環境で、モデル cache 済み・`num_steps=1` とし、現在のローカル環境にある `config/irodori_refs/ずんだもん.wav` を同じ clip のまま 2 本 `ref_wavs` に並べて確認しました。v4.1 Small の出力は次のとおりです（実行時の条件により秒数は変動します）。

| 条件 | 出力確認 |
| --- | --- |
| 参照音声 + caption | 48 kHz / mono / 約 4.44 秒 |
| caption-only（`no_ref=true`） | 48 kHz / mono / 約 1.72 秒 |

GPU がない環境では `IrodoriTTSEngine(use_gpu=False)` に変更できますが、0.8B checkpoint の CPU ロード・推論には相応のメモリと時間が必要です。

## トラブルシューティング

### `checkpoint download failed` / `offline`

初回合成には Hugging Face への HTTPS 接続が必要です。標準 Hugging Face cache の空き容量・書き込み権限、プロキシ、Hub の接続を確認してください。途中ファイルを消す場合は合成プロセスを停止してから cache の対象 checkpoint だけを削除します。

### `model` と codec の latent dimension mismatch

`hf_checkpoint` と `codec_repo` の組み合わせが不一致です。既定の v4.1 と `Aratako/Semantic-DACVAE-Japanese-32dim` を使うか、明示した v2/v3 checkpoint に対応する codec を指定してください。

### CUDA が使われない / 初回ロードが長い

`torch.cuda.is_available()`、CUDA 対応 torch、GPU メモリを確認します。利用できない場合は自動的に CPU へフォールバックします。モデルロード直後は GPU 転送と tokenizer 初期化があるため、初回だけ時間がかかります。

### `no_ref`、参照、caption のエラー

`no_ref: true` では `ref_wav` / `ref_wavs` / `ref_latent` / `ref_latents` を指定しないでください。参照を使う場合は `no_ref` を省略（または false）し、waveform と latent のどちらか一方だけを選びます。caption だけの合成は `no_ref: true` と caption の組み合わせです。

### 参照が見つからない

旧来の `voice_name` / `character_name` 自動検索は `config/irodori_refs/` を対象にします。GUI で登録した資産は、完全デフォルトでは `data/character_voice_assets/<character UUID>/`、明示的な `storage_root` / `AOITALK_DATA_DIR` ではその root 配下に保存されます。キャラクターの `voice_parameters.irodori_reference_assets` がパスと順序の正本です。GUI 資産を手で `config/irodori_refs` へ移動せず、キャラクター設定画面から再登録してください。

### PC スピーカー録音が使えない

この機能は Windows WASAPI render-loopback と `PyAudioWPatch` 専用です。マイク入力を代用する機能ではありません。Linux / WSL / macOS ではアップロード済み音声を使い、必要なら OS 側で録音したファイルを D&D してください。

### caption と参照音声で品質が不安定

v4.1 の公式制限事項どおり、矛盾する caption（例: 子どもの参照に低い男性声）は一方の条件が優先されたり、アーティファクトが出たりします。caption は感情・話し方・環境に寄せ、声の基礎特性は参照に合わせてください。
