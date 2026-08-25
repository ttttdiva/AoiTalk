# llama.cpp / GGUF ローカルモデルを AoiTalk で使う

AoiTalk の `openai_compatible_local` から、所有プロセスとして汎用の
`llama-server` を起動し、ローカル GGUF モデルを OpenAI 互換 API として使う手順です。
Muse Glimmer、Qwen3.8、Gemma 4 は同じ `llama_cpp` runtime を共有します。Ollama、
第三者製の OpenAI 互換サーバーはこの手順の対象外です。

## 1. プロファイルと runtime

モデル固有値は backend の model-profile registry（`LLAMA_CPP_MODEL_PROFILES` と
`llama_cpp_model_profile()`）に一元化されています。AoiTalk の UI、catalog、API、起動
argv は同じ profile metadata を参照するため、`llama.cpp` で動く通常の新規 GGUF は
profile を 1 件追加するだけで登録できます。既存の `local-model` は、AoiTalk が起動
せず、入力した Base URL の外部サーバーへ接続する従来の動作を保ちます。

選択値は profile の `id` と `served_alias` をそのまま使ってください。ファイル名や
表示ラベルを model ID にしないでください。

| profile ID / served alias | GGUF | quantization | 起動 context | native context | llama.cpp build | 固有要件・capability |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `muse-glimmer-30b` | `muse-glimmer-30B-kquant-17gb.gguf` | 公式 k-quant 17GB（公式 repo に literal `Q4_K_M` はない） | 131072 | profile に記載なし | b10353 以上 | `--jinja`、text-only |
| `qwen3.8-27b` | `Qwen3.8-27B-UD-Q4_K_XL.gguf` | UD-Q4_K_XL | **32768（AoiTalk の tested default）** | **262144** | b7990 以上。reasoning/tool parser は b10227 以上 | `--jinja`、reasoning / tool calling。media は非対応 |
| `qwen3.8-27b-heretic-uncensored` | `Qwen3.8-27B-Heretic-Q4_K_M.gguf` | Q4_K_M | **32768（AoiTalk の tested default）** | **262144** | b7990 以上。reasoning/tool parser は b10227 以上 | `--jinja`、reasoning / tool calling。media は非対応 |
| `gemma-4-26b-a4b-it-qat-q4-0` | `gemma-4-26B_q4_0-it.gguf` | QAT Q4_0 | **32768** | **262144** | b8637 以上。reasoning/tool parser は [b8665](https://github.com/ggml-org/llama.cpp/releases/tag/b8665) 以上 | `--jinja`、reasoning / tool calling。media は非対応 |

Qwen3.8 の b7990 は qwen3.5 系（GGUF architecture `qwen35`）をロードできる最小の
profile 値です。実際に reasoning や tool calling を使うときは、Qwen 用 specialized
parser が入った b10227 以上を使ってください。AoiTalk は環境ごとのメモリに合わせて
`context_size` を変更できますが、32768 を 262144 と取り違えないでください。後者は
モデルの native metadata であり、常に起動する値ではありません。

## 2. llama.cpp の準備

1. AoiTalk の通常セットアップ（[セットアップガイド](setup_guide.md)）を完了します。
2. [llama.cpp の公式 Releases](https://github.com/ggml-org/llama.cpp/releases) から
   OS と GPU backend に合う `llama-server` を取得します。
   - Windows: `llama-server.exe`
   - Linux: `llama-server`
   - Qwen3.8 の reasoning/tool calling: **b10227 以上**
   - Gemma 4 の reasoning/tool calling: **b8665 以上**（GGUF ロードのみなら b8637 以上）
   - Muse Glimmer: **b10353 以上**
3. `PATH` に置くか、AoiTalk の `executable`（または `LLAMA_CPP_EXECUTABLE`）へ絶対
   パスを指定します。

```text
# Windows PowerShell
& "C:\path\to\llama-server.exe" --version

# Linux
/path/to/llama-server --version
```

`--version` の出力に `bNNNNN` または `build NNNNN` がある場合、その番号を確認します。
build 番号を報告しない古い/独自ビルドを、profile の minimum build を満たすと推測して
使用しないでください。AoiTalk は managed profile の起動前に確認します。

## 3. Qwen3.8 GGUF の入手

### 通常版 Unsloth UD-Q4_K_XL（profile: `qwen3.8-27b`）

対象 repository は [unsloth/Qwen3.8-27B-GGUF](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF) です。
UD-Q4_K_XL の正確なファイル名は次の **1 ファイル**です（Hugging Face Files/API で確認）。

```text
Qwen3.8-27B-UD-Q4_K_XL.gguf
```

Hugging Face の標準 cache またはユーザーが指定する任意の場所を使います。AoiTalk
repository 配下へ強制保存したり、別名のファイルへ置き換えたりしないでください。

```text
python -m pip install -U huggingface_hub
hf download unsloth/Qwen3.8-27B-GGUF `
  --include "Qwen3.8-27B-UD-Q4_K_XL.gguf" `
  --local-dir "$HOME/AoiTalk-models/qwen3.8-27b"
```

MTP を使う場合は、target と同じ保存先へ公式 sidecar も配置します。対象は
[ggml-org/Qwen3.8-27B-GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF) の
`mtp-Qwen3.8-27B-Q4_0.gguf` です。

```text
hf download ggml-org/Qwen3.8-27B-GGUF mtp-Qwen3.8-27B-Q4_0.gguf --local-dir "$HOME/AoiTalk-models/qwen3.8-27b"
```

Linux では PowerShell の行継続記号（バッククォート）を \ に置き換えてください。ダウンロード
後は `model_path` に絶対パスを指定します。GGUF は大きいため Git に add/commit しない
でください。

unsloth repository には `mmproj-BF16.gguf` と `mmproj-F16.gguf` もありますが、AoiTalk
は Gemma 4 と同様に mmproj を管理しません。vision を有効化せず、`--mmproj` を argv に
追加しません。

### Heretic Q4_K_M（profile: `qwen3.8-27b-heretic-uncensored`）

対象 repository は [0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF](https://huggingface.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF) です。
Q4_K_M の正確なファイル名は次の **1 ファイル**です。

```text
Qwen3.8-27B-Heretic-Q4_K_M.gguf
```

Hugging Face の標準 cache またはユーザーが指定する任意の場所を使います。AoiTalk
repository 配下へ強制保存したり、別名のファイルへ置き換えたりしないでください。

```text
python -m pip install -U huggingface_hub
hf download 0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF `
  --include "Qwen3.8-27B-Heretic-Q4_K_M.gguf" `
  --local-dir "$HOME/AoiTalk-models/qwen3.8-27b-heretic"
```

Linux では PowerShell の行継続記号（バッククォート）を \ に置き換えてください。ダウンロード
後は `model_path` に絶対パスを指定します。GGUF は大きいため Git に add/commit しない
でください。

## 4. AoiTalk の UI 設定

「設定 → LLM」で provider に **`openai_compatible_local`**、model に通常版
**`qwen3.8-27b`** または Heretic 版 **`qwen3.8-27b-heretic-uncensored`** を選択します。
表示名は「Qwen3.8-27B 通常版 UD-Q4_K_XL」と「Qwen3.8-27B Heretic Abliterated
Uncensored Q4_K_M」で区別します。各 Qwen profile の `served_alias` も同じ文字列です。

| UI 項目 | 保存キー | Qwen の profile/default | 説明 |
| --- | --- | --- | --- |
| 実行ファイル | `executable` | 空欄（`PATH`） | `llama-server(.exe)`。見つからない場合は絶対パス。 |
| モデルパス | `model_path` | **必須（managed起動）** | 上記 GGUF の絶対パス。HF cache の場所も可。 |
| 自動検出root | `model_root` / `LLAMA_CPP_MODEL_ROOT` | 任意 | `D:\\AI\\models\\Hot\\llm` など、profileの正確なGGUF filenameを探すディレクトリ。既存profile_runtimeの保存先もfallbackとして使います。AoiTalkはGGUFを自動ダウンロードしません。 |
| served alias | `model_alias` | `qwen3.8-27b` または `qwen3.8-27b-heretic-uncensored` | `/v1/models` の `data[].id` と完全一致。profile 管理対象では編集不可。 |
| host / port | `host` / `port` | `127.0.0.1` / `8080` | Base URL は `http://127.0.0.1:8080/v1`。 |
| context | `context_size` | **32768** | tested default。262144 native は必要な場合だけ、メモリを確認して指定。 |
| GPU offload | `gpu_layers` | `999` | `--n-gpu-layers`。VRAM 不足時は下げ、CPU のみなら 0。 |
| MTP / Multi-Token Prediction | `mtp_enabled` | Qwen3.8 は **ON** | 利用可能な場合だけ MTP を有効化。利用できない場合も本体の起動は継続。 |
| 追加引数 | `extra_args` | `[]`（profile の required/default は `--jinja`） | `--model`、`--alias`、`--host`、`--port`、`--ctx-size`、`--n-gpu-layers` は重複指定しない。`--jinja` も重複させない。 |
| auto-start | `auto_start` | `true` | 選択 profile の `llama-server` を AoiTalk が所有・起動。 |
| readiness timeout | `readiness_timeout` | `180` 秒 | `/v1/models` に alias が現れるまで。`readiness_timeout_seconds` も可。 |

`base_url` は host/port から自動反映されます。手動起動した外部サーバーへ接続する
場合は `auto_start=false` とし、同じポートで AoiTalk が別プロセスを起動しないように
します。

### Qwen3.8 の MTP / Multi-Token Prediction

Qwen3.8 managed profile の MTP は `extra_args` ではなく、専用の runtime 設定です。
新規設定と保存値がない既存設定では ON になります。UI で OFF にして保存した値は、
再読込・再起動後も維持され、別の profile へ持ち越されません。

通常版 `qwen3.8-27b` は、Unsloth の UD-Q4_K_XL target と、同じ保存先に置いた
`ggml-org/Qwen3.8-27B-GGUF` の公式 `mtp-Qwen3.8-27B-Q4_0.gguf` sidecar を
companion として扱います。この組み合わせは llama-server b10437 で互換性を実測確認済み
です。sidecar が exact filename で見つかり、runtime が利用条件を満たした場合だけ、
AoiTalk は managed argv に MTP flags を追加します。利用者が別 MTP GGUF を推測で
結び付けたり、`--spec-draft-model` を手動で追加したりしません。

Heretic `qwen3.8-27b-heretic-uncensored` は配布元が **NO-NEXTN** と明記しており、
現行 profile には互換性を確認できた companion MTP GGUF がありません。トグルの既定値は
ON のままでも、MTP を解決できない状態として表示し、`--spec-type` などを付けずに本体を
通常モードで起動します。公式 Qwen 用 MTP artifact を Heretic に流用できるとはみなしません。

別 artifact が必要な構成で artifact を解決できない場合も、失われるのは MTP による
高速化だけです。本体の起動を失敗させず、UI/runtime の状態に理由を表示します。AoiTalk
は MTP artifact を自動ダウンロードせず、Hugging Face の cache や保存先も変更しません。

`--spec-type draft-mtp`、`--spec-draft-model`、`--spec-draft-hf`、`--spec-draft-n-max` は runtime が管理する
引数です。`extra_args` へ入力せず、MTP を OFF または compatibility 未確認にしたときは
MTP 関連 argv を一切追加しないでください。`--jinja` は従来どおり profile の required/default arg として 1 回だけ
指定し、reasoning / tool calling の parser 契約を上書きしません。

YAML を直接編集する場合（通常版の例。Heretic 版は `qwen3.8-27b-heretic-uncensored`
と `Qwen3.8-27B-Heretic-Q4_K_M.gguf` に置き換えます）:

```yaml
llm_provider: openai_compatible_local
llm_model: qwen3.8-27b
openai_compatible_local:
  base_url: http://127.0.0.1:8080/v1
  model: qwen3.8-27b
  api_key: dummy
  llama_cpp:
    executable: "<絶対パス>/llama-server[.exe]"
    model_path: "<絶対パス>/Qwen3.8-27B-UD-Q4_K_XL.gguf"
    model_alias: qwen3.8-27b
    host: 127.0.0.1
    port: 8080
    context_size: 32768
    gpu_layers: 999
    mtp_enabled: true
    extra_args: []
    auto_start: true
    readiness_timeout: 180
```

主な環境変数は `LLAMA_CPP_EXECUTABLE`（別名 `LLAMA_SERVER_EXE`）、
`LLAMA_CPP_MODEL_PATH`、`LLAMA_CPP_MODEL_ALIAS`、`LLAMA_CPP_HOST`、
`LLAMA_CPP_PORT`、`LLAMA_CPP_CONTEXT_SIZE`、`LLAMA_CPP_GPU_LAYERS`、
`LLAMA_CPP_EXTRA_ARGS`、`LLAMA_CPP_AUTO_START`、`LLAMA_CPP_READINESS_TIMEOUT` です。
環境変数は保存済み UI 値より優先されます。設定変更後は AoiTalk と llama-server を
再起動してください。

## 5. 起動・readiness・通常チャット

Qwen profile が選択され、`model_path` が存在し、`auto_start=true` の場合、AoiTalk は
shell を経由せず、profile metadata から次の argv を構築します。

```text
llama-server(.exe) --model <model_path> \
  --alias qwen3.8-27b \
  --host 127.0.0.1 --port 8080 --ctx-size 32768 \
  --n-gpu-layers 999 --jinja
```

ログは `logs/models/llama_cpp.log`、準備完了条件は次です。

```text
curl http://127.0.0.1:8080/v1/models
```

レスポンスの `data[].id` に、次の文字列が **完全一致**で現れる必要があります。

```json
{"id":"qwen3.8-27b"}
```

Windows PowerShell では次も使えます。

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models | ConvertTo-Json -Depth 5
```

OpenAI 互換 API の通常チャット（AoiTalk と同じ endpoint）:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="dummy")
response = client.chat.completions.create(
    model="qwen3.8-27b",
    messages=[{"role": "user", "content": "こんにちは。短く自己紹介してください。"}],
)
print(response.choices[0].message)
```

## 6. reasoning / tool calling / media の扱い

- 対象 GGUF の chat template は thinking を既定で有効にし、`<think>...</think>` と
  `<tool_call><function=...` 形式を生成します。Qwen profile は `supports_reasoning`
  と `supports_tools` を宣言しています。
- llama.cpp **b10227** の Qwen specialized parser（reasoning の終端と tool-call の
  区切りを扱う）を使います。`--jinja` は profile の required/default arg なので、
  `extra_args` に重複して書きません。
- AoiTalk から `--reasoning-format`、`--chat-parser`、独自の正規表現や reasoning
  フィールド変換を追加しないでください。parser/format は対象モデルと使用する
  llama.cpp build で検証できた場合だけ profile metadata へ記載し、推測値を入れません。
- Qwen の基礎モデル README/config は image-text-to-text と vision encoder を説明しますが、
  **AoiTalk の Qwen profile は mmproj を管理せず vision を有効化しません**。unsloth
  repository には `mmproj-BF16.gguf` / `mmproj-F16.gguf` がありますが、Gemma 4 と同様に
  `--mmproj` を argv に追加しません。Heretic repository の Files は GGUF 1 ファイル
  （README と metadata を除く）だけで、vision tensor や `mmproj` がありません。この
  AoiTalk profile の media capability は image/audio とも `false` です。画像・動画を
  通常チャットへ送らないでください。

## 7. Muse Glimmer を使う場合

既存 profile `muse-glimmer-30b` は同じ runtime を使用します。

1. [公式 Muse GGUF repository](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF)
   から `muse-glimmer-30B-kquant-17gb.gguf` を取得します。公式 repo に literal
   `Q4_K_M` はないため、別名の Q4/BF16/Safetensors を標準入力にしません。
2. llama.cpp b10353 以上を用意します（古い版は `unknown model architecture:
   muse-glimmer` になります）。
3. UI の model は `muse-glimmer-30b`、`context_size` は 131072、`model_alias` は
   `muse-glimmer-30b` にします。Muse profile も `--jinja` を必要とします。

Ollama は別 runtime です。Muse/Qwen/Gemma の GGUF path や alias を流用しないで
ください。

## 8. 手動起動・競合・トラブルシューティング

### 手動起動

`auto_start=false` にしてから、profile と同じ alias/context/`--jinja` で起動します。

```powershell
& "C:\path\to\llama-server.exe" `
  --model "C:\Users\<ユーザー名>\AoiTalk-models\qwen3.8-27b-heretic\Qwen3.8-27B-Heretic-Q4_K_M.gguf" `
  --alias qwen3.8-27b-heretic-uncensored --host 127.0.0.1 --port 8080 `
  --ctx-size 32768 --n-gpu-layers 999 --jinja
```

### 同一 port に別モデルがいる

AoiTalk が所有していない外部プロセスは kill しません。`/v1/models` の ID が選択
profile と異なる場合は、外部 server を自分で停止するか、host/port と Base URL を
別値に変更してください。AoiTalk が所有する以前の llama-server だけが hot switch
で停止対象です。

### よくあるエラー

- **`llama-server(.exe) not found`**: PATH または `executable` を確認します。
- **build が古い**: Qwen の通常ロードは b7990、reasoning/tool は b10227、Muse は
  b10353 が必要です。`--version` の値を確認して公式 release に更新します。
- **`model_path` が見つからない**: 通常版は `Qwen3.8-27B-UD-Q4_K_XL.gguf`、Heretic 版は
  `Qwen3.8-27B-Heretic-Q4_K_M.gguf` と大文字小文字を含めて照合し、絶対パスを指定します。
- **alias/readiness timeout**: `/v1/models` の ID が profile ID と完全一致するか、
  `logs/models/llama_cpp.log` の model load/VRAM/DLL エラーを確認します。
- **VRAM 不足**: `gpu_layers` を 999 から下げ、必要なら context を 32768 未満へ下げます。
  native 262144 を無条件に指定しません。
- **tool/reasoning が本文へ混ざる**: b10227 以上、`--jinja` が 1 回だけ、`extra_args`
  に chat template/parser/reasoning の上書きがないことを確認します。
- **画像入力をしたい**: 今回の GGUF profile は media 非対応です。vision tensor/mmproj
  を含む別モデルと profile を用意するまで送信しません。

## 9. 一次情報と判断根拠

値を変更するときは、次の一次情報を再確認し、確認できない値を profile に追加しない
でください。

| 根拠 | 確認した内容（短い原文） |
| --- | --- |
| [unsloth HF README](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF) | Unsloth Dynamic V3.0、`UD-Q4_K_XL`、mmproj ファイルの存在。 |
| [unsloth HF Files/API](https://huggingface.co/api/models/unsloth/Qwen3.8-27B-GGUF) | 実ファイル `Qwen3.8-27B-UD-Q4_K_XL.gguf`、architecture `qwen35`、`context_length: 262144`、chat template。 |
| [Unsloth Qwen3.8 実行ガイド](https://unsloth.ai/docs/models/qwen3.8) | `hf download ... --include "*UD-Q4_K_XL*"` と `Qwen3.8-27B-UD-Q4_K_XL.gguf` を標準 llama.cpp で実行。IQ1_XXXS 専用 branch は 2.4T 向け。 |
| [ggml-org Qwen3.8-27B-GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF) | 公式 sidecar `mtp-Qwen3.8-27B-Q4_0.gguf`。UD-Q4_K_XL target との互換性を llama-server b10437 で実測確認。 |
| [対象 HF README](https://huggingface.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF) | `Q4_K_M` / `Qwen3.8-27B-Heretic-Q4_K_M.gguf`、使用例の `-ngl 999 -c 32768 --jinja`、検証済み status。 |
| [対象 HF API metadata](https://huggingface.co/api/models/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF) | GGUF architecture `qwen35`、`context_length: 262144`、chat template、Files の一覧。 |
| [Qwen3.8 base README](https://huggingface.co/Qwen/Qwen3.8-27B) | `Context Length: 262,144 natively`、thinking mode と `reasoning_effort` の説明。 |
| [Qwen3.8 base config](https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json) | `model_type: qwen3_5`、`max_position_embeddings: 262144`、vision config（基礎モデル側）。 |
| [llama.cpp qwen3.5 support commit `fc0fe40`](https://github.com/ggml-org/llama.cpp/commit/fc0fe40) / [release b7990](https://github.com/ggml-org/llama.cpp/releases/tag/b7990) | release notes: `models : support qwen3.5 series`。Qwen 系 GGUF architecture のロード根拠。 |
| [llama.cpp Qwen parser commit `f5919bf`](https://github.com/ggml-org/llama.cpp/commit/f5919bf) / [release b10227](https://github.com/ggml-org/llama.cpp/releases/tag/b10227) | release notes: `chat : add qwen3 specialized parser`。reasoning/tool の parser 最低 build。 |
| [llama.cpp release b8637](https://github.com/ggml-org/llama.cpp/releases/tag/b8637) / [PR #21309](https://github.com/ggml-org/llama.cpp/pull/21309) | release notes: `model, mtmd: fix gguf conversion for audio/vision mmproj (#21309)`。Gemma profile のロード minimum build 根拠。 |
| [llama.cpp release b8665](https://github.com/ggml-org/llama.cpp/releases/tag/b8665) / [PR #21418](https://github.com/ggml-org/llama.cpp/pull/21418) | release notes: `common : add gemma 4 specialized parser`（`emit JSON from Gemma4 tool call AST`、`add custom template to support interleaved thinking`）。reasoning/tool parser 最低 build。PR #21418 の merge SHA は `b8635075f...` で build 8637 ではない。 |
| [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) | `--alias`、`--jinja`、OpenAI 互換 `/v1/models`/chat endpoint の仕様。 |
| [llama.cpp function calling docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md) | server 側の function-calling と chat template の扱い。AoiTalk 側で parser を推測しない根拠。 |

Qwen base repository が vision-language を説明していても、AoiTalk の Qwen profile は
mmproj を管理せず vision を有効化しません。通常版 unsloth repository には mmproj が
ありますが argv へ追加しません。Heretic repository については、対象 GGUF repository
の実ファイルに vision tensor/mmproj がないという観測を優先します。GGUF の capability
を変更する場合は、対象ファイルと実際に使用する llama.cpp build で再検証してください。
