# llama.cpp / GGUF ローカルモデルを AoiTalk で使う

AoiTalk の `openai_compatible_local` から、所有プロセスとして汎用の
`llama-server` を起動し、ローカル GGUF モデルを OpenAI 互換 API として使う手順です。
Muse Glimmer、Qwen3.8、Gemma 4、Dark Scarlett は同じ `llama_cpp` runtime を共有します。Ollama、
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
| `qwen3.8-27b` | `Qwen3.8-27B-UD-Q4_K_XL.gguf` + `mmproj-F16.gguf` | UD-Q4_K_XL | **32768（AoiTalk の tested default）** | **262144** | b7990 以上。reasoning/tool parser は b10227 以上 | `--jinja`、reasoning / tool calling、**image 対応**。managed runtime が `--mmproj` を付与 |
| `qwen3.8-27b-heretic-uncensored` | `Qwen3.8-27B-Heretic-Q4_K_M.gguf` | Q4_K_M | **32768（AoiTalk の tested default）** | **262144** | b7990 以上。reasoning/tool parser は b10227 以上 | `--jinja`、reasoning / tool calling。media は非対応 |
| `qwen3.8-flash-next` | `Qwen3.8-Flash-Next-UD-IQ4_XS` 3-shard | UD-IQ4_XS | **16384** | **262144** | llama.cpp b10660 以上 | `--jinja`、text-only |
| `qwen3.8-flash-next-uncensored` | `Qwen3.8-Flash-Next-Uncensored-IQ4_XS` 3-shard | IQ4_XS | **16384** | **262144** | llama.cpp b10660 以上 | `--jinja`、text-only。mmproj / vision / MTP なし |
| `gemma-4-26b-a4b-it-qat-q4-0` | `gemma-4-26B_q4_0-it.gguf` | QAT Q4_0 | **32768** | **262144** | b8637 以上。reasoning/tool parser は [b8665](https://github.com/ggml-org/llama.cpp/releases/tag/b8665) 以上 | `--jinja`、reasoning / tool calling。media は非対応 |
| `dark-scarlett-27b-v2.0` | `Dark-Scarlett-27B-v2.0-Q4_K_M.gguf` + `MMPROJ-Dark-Scarlett-27B-v2.0-Q8_0.gguf` | Q4_K_M | **32768** | **262144** | **b10227 以上** | `--jinja`、restricted optional、**image 対応**。managed runtime が `--mmproj` を付与 |
| `dark-scarlett-26b-a4b-v1.0` | `Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf` | Q4_K_M | **32768** | **262144** | 未宣言 | `--jinja`、restricted optional / text-only。reasoning / tool / media は非対応 |

Qwen3.8 の b7990 は qwen3.5 系（GGUF architecture `qwen35`）をロードできる最小の
profile 値です。実際に reasoning や tool calling を使うときは、Qwen 用 specialized
parser が入った b10227 以上を使ってください。AoiTalk は環境ごとのメモリに合わせて
`context_size` を変更できますが、32768 を 262144 と取り違えないでください。後者は
モデルの native metadata であり、常に起動する値ではありません。

## 2. llama.cpp の準備

AoiTalk の既定 managed storage は repository-local の Git 非追跡領域です。runtime は
`<repository>/.aoitalk-local-llm/runtime/runtimes/llama_cpp/current`、llama.cpp の
managed model は `<repository>/.aoitalk-local-llm/models/llama_cpp` に分離されます。
Settings の `model_root` から任意の外部 directory を指定できます。この端末固有の
absolute path は documentation や tracked config に記録しません。QA helper / logs は
repository の既存の ignored な QA 領域へ置きます。

1. AoiTalk の通常セットアップ（[セットアップガイド](setup_guide.md)）を完了します。
2. [llama.cpp の公式 Releases](https://github.com/ggml-org/llama.cpp/releases) から
   OS と GPU backend に合う `llama-server` を取得します。
   - Windows: `llama-server.exe`
   - Linux: `llama-server`
   - Qwen3.8 の reasoning/tool calling: **b10227 以上**
   - Gemma 4 の reasoning/tool calling: **b8665 以上**（GGUF ロードのみなら b8637 以上）
   - Muse Glimmer: **b10353 以上**
   - Dark Scarlett 27B v2.0: **b10227 以上**（AoiTalkのconservative profile floor。b10437でmain+mmprojの画像入力を実測確認）
   - Qwen3.8-Flash-Next / Uncensored: **b10660 以上**
現行 llama.cpp の multimodal 契約では、local file は `-m model.gguf --mmproj file.gguf` で
本体と projector を指定し、`llama-server` の OpenAI-compatible `/chat/completions` が画像入力を
受けます。AoiTalk の managed profile はこの `--mmproj` を generic auxiliary artifact から組み立てます。

3. 登録済みの trusted profile は、`POST /api/llm/local-runtime/prepare`（または
   `ManagedLocalRuntimeManager.start_prepare()`）で公式 runtime と GGUF を自動準備します。
   `local-model` や手動 `model_path` の外部サーバー接続は従来どおり手動管理です。
4. 手動運用する場合は `PATH` に置くか、AoiTalk の `executable`（または
   `LLAMA_CPP_EXECUTABLE`）へ絶対パスを指定します。

managed installer は通常 `/releases/latest` の stable を優先します。profile の minimum
（Flash-Next は b10660）を満たさない、または対応 platform asset がない場合だけ、
組み込み GitHub provider が `/releases?per_page=30` の prerelease を含む一覧から
`bNNNNN` を比較し、official binary（Windows CUDA は matching cudart も必須）が揃う
候補を選びます。任意 URL や第三者 binary へ fallback せず、`release_provider` を注入した
テストでは network の recent provider を呼びません。

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

Hugging Face の標準 cache または Settings で指定した model root を使います。AoiTalk は
cache の場所を固定せず、既存の `HF_HOME` / `HF_HUB_CACHE` 設定を尊重します。managed
download は既定の ignored model root（または Settings の override）へ保存し、GGUF の
実体を Git に追加しないでください。

```text
python -m pip install -U huggingface_hub
hf download unsloth/Qwen3.8-27B-GGUF `
  --include "Qwen3.8-27B-UD-Q4_K_XL.gguf" `
  --local-dir "<model-root>/qwen3.8-27b"
```

MTP を使う場合は、target と同じ保存先へ公式 sidecar も配置します。対象は
[ggml-org/Qwen3.8-27B-GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF) の
`mtp-Qwen3.8-27B-Q4_0.gguf` です。

```text
hf download ggml-org/Qwen3.8-27B-GGUF mtp-Qwen3.8-27B-Q4_0.gguf --local-dir "<model-root>/qwen3.8-27b"
```

Linux では PowerShell の行継続記号（バッククォート）を \ に置き換えてください。ダウンロード
後は `model_path` に絶対パスを指定します。GGUF は大きいため Git に add/commit しない
でください。

Unsloth repository は基礎モデルを「native vision-language model」と説明し、
`mmproj-BF16.gguf` / `mmproj-F16.gguf` を同じ repository で配布しています。AoiTalk は
`mmproj-F16.gguf` を required auxiliary artifact として管理し、revision
`4ca720788d1e01f1bff70c033e0d0028fd02e502`、size `927607488` bytes、SHA-256
`cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e` を profile に固定します。
managed prepare は main GGUF と mmproj の両方を検証して同じ local directory に配置し、
llama-server 起動時に `--mmproj <path>` を generic profile metadata から追加します。

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
  --local-dir "<model-root>/qwen3.8-27b-heretic"
```

Linux では PowerShell の行継続記号（バッククォート）を \ に置き換えてください。ダウンロード
後は `model_path` に絶対パスを指定します。GGUF は大きいため Git に add/commit しない
でください。

### OrcaRouter Uncensored IQ4_XS（profile: `qwen3.8-flash-next-uncensored`）

対象 repository は [orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF](https://huggingface.co/orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF) です。
Hugging Face の repository root にある IQ4_XS の 3 shard を、次の順序・サイズで使用します。

```text
Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00001-of-00003.gguf  44,766,155,936 bytes
Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00002-of-00003.gguf  44,735,995,008 bytes
Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00003-of-00003.gguf   7,971,004,256 bytes
total                                                        97,473,155,200 bytes
```

この profile には `gguf_repository_subdir` を設定しません。AoiTalk の trusted managed
prepare が上記 3 ファイルを `hf_hub_download` で取得し、`.partial` staging から検証済み
directory へ原子的に切り替えます。repository が gated の場合は、事前に Hugging Face
アカウントで access 承認と `hf auth login` を完了してください。承認できない場合は、
別 repository や別量子化へ置き換えず、prepare を失敗として扱います。

起動 default は `--ctx-size 16384`、`--n-gpu-layers auto`、`--jinja` です。native context
metadata は 262144 ですが、既定値ではありません。text-only のため mmproj、vision flag、
MTP artifact/flag は今回管理しません。`--model` には primary（`00001`）だけを指定し、
残り 2 shard は同じ directory に置きます。

managed runtime root の既定値は `<repository>/.aoitalk-local-llm/runtime` です。
AoiTalk はその配下の `runtimes/llama_cpp/current` を llama-server の managed install
として扱い、約 97.47 GB のGGUF artifact と一時領域は、分離された `model_root`
（既定値: `<repository>/.aoitalk-local-llm/models/llama_cpp`）へ保存します。
容量や既存モデルを優先する場合は Settings の model root に外部 directory を指定してください。

## 3.5 Dark Scarlett を使う場合

Dark Scarlett は ReadyArt が公開する GGUF を、既存の `llama_cpp` managed
profile として利用します。AoiTalk は重みをリポジトリへ同梱せず、下記の正確な
repository / filename の組み合わせだけをダウンロード対象として扱います。別名の
GGUF、別 repository、互換性未確認の mmproj / MTP artifact を代用しないでください。

### Dark Scarlett 27B v2.0（profile: `dark-scarlett-27b-v2.0`）

- model card: [ReadyArt/Dark-Scarlett-27B-v2.0](https://huggingface.co/ReadyArt/Dark-Scarlett-27B-v2.0)
- GGUF repository: [ReadyArt/Dark-Scarlett-27B-v2.0-GGUF](https://huggingface.co/ReadyArt/Dark-Scarlett-27B-v2.0-GGUF)
- exact main artifact: `Dark-Scarlett-27B-v2.0-Q4_K_M.gguf`
- exact vision artifact: `MMPROJ-Dark-Scarlett-27B-v2.0-Q8_0.gguf`
- immutable provenance: revision `5aa0350de88b22bfbb717de29e02f9666718c8ad`
  - main: size `16810714336` bytes、SHA-256 `464c09dd5fb42bd8b8e61b8d3c6e368c75d1089a3cf5ed01bd47ac0c9b46739b`
  - mmproj: size `629247040` bytes、SHA-256 `08af092b6bf0e29ef907170e2e0ef8c284333779962fcf7dac4a05a759056637`
  - gated `false`、card license metadata `apache-2.0`
- source config は `Qwen3_5ForConditionalGeneration`、`language_model_only: false`、
  `vision_config`、`max_position_embeddings: 262144` を宣言します。

```powershell
hf download ReadyArt/Dark-Scarlett-27B-v2.0-GGUF `
  Dark-Scarlett-27B-v2.0-Q4_K_M.gguf `
  MMPROJ-Dark-Scarlett-27B-v2.0-Q8_0.gguf `
  --revision 5aa0350de88b22bfbb717de29e02f9666718c8ad `
  --local-dir "<model-root>/dark-scarlett-27b-v2.0"
```

### Dark Scarlett 26B A4B v1.0（profile: `dark-scarlett-26b-a4b-v1.0`）

- model card: [ReadyArt/Dark-Scarlett-v1.0-26B-A4B](https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B)
- GGUF repository: [ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF](https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF)
- exact artifact: `Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf`
- source config の `native_context_size` は `262144`。AoiTalk の起動 default はメモリを
  考慮して `32768` です。HF の Xet/SHA 表示は provenance の記録にとどめ、managed
  trust contract には追加しません。

```powershell
hf download ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF `
  Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf `
  --local-dir "<model-root>/dark-scarlett-26b-a4b-v1.0"
```

27B v2.0 は `--jinja` に加えて required mmproj を `--mmproj` で渡す image-capable
profileです。reasoning / tool calling / audio は引き続き無効です。26B A4B v1.0 は
互換性を確認できる mmproj を profile に持たないため text-only のままです。
`--spec-type`、`--spec-draft-model` などの MTP 引数は Dark Scarlett には追加しません。
ダウンロード後は `model_path` に primary GGUF の絶対パスを指定し、`model_alias` は
profile ID と完全一致させてください。managed prepare は registry の repository / revision /
filename / size / SHA-256 を照合し、spoof された artifact contract は拒否します。
GGUF の実体（重み）は Git に add/commit せず、managed model root にだけ置いてください。

両モデルは成人向けロールプレイを想定した restricted optional profile です。
Enterprise の default / required model や自動 routing candidateにはせず、operator/user
が明示的に選択した場合だけ使用します。27B のカード/API metadataはApache-2.0を示しますが、
AoiTalkは商用利用承認を表明しません。26Bのsource cardにはpersonal/non-profit/non-commercial
の追加Usage Agreementがあるため、こちらも商用利用可能とは扱いません。どちらのweightsも
AoiTalk repository、Public publish、Enterprise handoffへ同梱・再配布しません。

## 4. AoiTalk の UI 設定

「設定 → LLM」で provider に **`openai_compatible_local`**、model に通常版
**`qwen3.8-27b`**、Heretic 版 **`qwen3.8-27b-heretic-uncensored`**、または
Flash-Next aligned 版 **`qwen3.8-flash-next`**、Dark Scarlett 27B v2.0
**`dark-scarlett-27b-v2.0`**、Dark Scarlett 26B A4B v1.0
**`dark-scarlett-26b-a4b-v1.0`** を選択します。
表示名は「Qwen3.8-27B 通常版 UD-Q4_K_XL」と「Qwen3.8-27B Heretic Abliterated
Uncensored Q4_K_M」、および「Qwen3.8-Flash-Next Uncensored IQ4_XS」で区別します。
Dark Scarlett は「Dark Scarlett 27B v2.0 Q4_K_M」と「Dark Scarlett 26B-A4B v1.0
Q4_K_M」で区別します。aligned `qwen3.8-flash-next` と OrcaRouter 版 `qwen3.8-flash-next-uncensored` は
別 profile / served alias です。各 Qwen profile の `served_alias` も同じ文字列です。

| UI 項目 | 保存キー | Qwen の profile/default | 説明 |
| --- | --- | --- | --- |
| 実行ファイル | `executable` | 空欄（`PATH`） | `llama-server(.exe)`。見つからない場合は絶対パス。 |
| モデルパス | `model_path` | **必須（managed起動）** | 上記 GGUF の絶対パス。HF cache の場所も可。 |
| モデルroot | `model_root` / `LLAMA_CPP_MODEL_ROOT` | `<repository>/.aoitalk-local-llm/models/llama_cpp` | 手動配置した profile の正確な GGUF filename を探す directory。Settings の model root override は trusted managed profile の保存先にもなります。外部 `local-model` / 任意手動 path は自動取得しません。 |
| runtime root | `openai_compatible_local.runtime_root` | `<repository>/.aoitalk-local-llm/runtime` | manager が `runtimes/llama_cpp/current` に llama-server executable を管理します。モデルrootとは分離します。 |
| served alias | `model_alias` | `qwen3.8-27b`、`qwen3.8-27b-heretic-uncensored`、`qwen3.8-flash-next`、`qwen3.8-flash-next-uncensored`、`dark-scarlett-27b-v2.0`、または `dark-scarlett-26b-a4b-v1.0` | `/v1/models` の `data[].id` と完全一致。profile 管理対象では編集不可。 |
| host / port | `host` / `port` | `127.0.0.1` / `8080` | Base URL は `http://127.0.0.1:8080/v1`。 |
| context | `context_size` | **32768（Qwen3.8-27B／Dark Scarlett）／16384（Flash-Next）** | tested default。262144 native は必要な場合だけ、メモリを確認して指定。 |
| GPU offload | `gpu_layers` | `999（Qwen3.8-27B／Dark Scarlett）／auto（Flash-Next）` | `--n-gpu-layers`。VRAM 不足時は下げ、CPU のみなら 0。 |
| MTP / Multi-Token Prediction | `mtp_enabled` | Qwen3.8-27B は **ON**、Flash-Next は **OFF**、Dark Scarlett は **非対応** | Qwen の既存契約だけが MTP artifact を扱います。Dark Scarlett には MTP flags を追加しません。 |
| 追加引数 | `extra_args` | `[]`（profile の required/default は `--jinja`） | `--model`、`--alias`、`--host`、`--port`、`--ctx-size`、`--n-gpu-layers` は重複指定しない。`--jinja` も重複させない。 |
| auto-start | `auto_start` | `true` | 選択 profile の `llama-server` を AoiTalk が所有・起動。 |
| readiness timeout | `readiness_timeout` | `180` 秒 | `/v1/models` に alias が現れるまで。`readiness_timeout_seconds` も可。 |

`base_url` は host/port から自動反映されます。手動起動した外部サーバーへ接続する
場合は `auto_start=false` とし、同じポートで AoiTalk が別プロセスを起動しないように
します。

### Qwen3.8-27B の MTP / Multi-Token Prediction

Qwen3.8-27B managed profile の MTP は `extra_args` ではなく、専用の runtime 設定です。
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

OrcaRouter 版を選択した場合は alias が
`qwen3.8-flash-next-uncensored`、primary は
`Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00001-of-00003.gguf`、context は 16384 になります。
managed prepare が完了するまで起動せず、3 shard が揃っていることを確認します。

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
- 通常版 `qwen3.8-27b` は Unsloth 配布の `mmproj-F16.gguf` を trusted auxiliary artifact
  として管理し、`media.image=true` です。画像添付時は main text LLM がそのまま
  OpenAI-compatible multimodal request を受け、managed llama-server は `--mmproj` を使います。
- `qwen3.8-27b-heretic-uncensored` の配布 repository には現在 mmproj と RVN 系 vision
  variant も存在しますが、AoiTalk が選択している legacy
  `Qwen3.8-27B-Heretic-Q4_K_M.gguf` とその mmproj の互換性を一次情報から確定できていません。
  この profile は推測で sidecar を流用せず `media.image=false` のままです。

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
& "<runtime-root>/llama-server.exe" `
  --model "<model-root>/qwen3.8-27b-heretic/Qwen3.8-27B-Heretic-Q4_K_M.gguf" `
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
- **画像入力をしたい**: `qwen3.8-27b` または `dark-scarlett-27b-v2.0` を選ぶと、
  Settings の画像認識を「言語モデルと同じ」のまま利用できます。profile の required
  mmproj が欠けている場合は managed prepare が ready 扱いにせず、llama-server 起動時にも
  fail closed します。画像非対応 profile を継承している場合、AoiTalk は画像 payload を
  その text-only model へ送らず、設定画面で画像対応モデルを選ぶよう明示します。

## 9. 一次情報と判断根拠

値を変更するときは、次の一次情報を再確認し、確認できない値を profile に追加しない
でください。

| 根拠 | 確認した内容（短い原文） |
| --- | --- |
| [unsloth HF README](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF) | 原文: `a native vision-language model that understands images and videos` / `Vision-Language Understanding: Native support for image and video understanding`。`UD-Q4_K_XL` と mmproj 配布も確認。 |
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
| [ReadyArt Dark Scarlett 27B GGUF API](https://huggingface.co/api/models/ReadyArt/Dark-Scarlett-27B-v2.0-GGUF?blobs=true) / [source config](https://huggingface.co/ReadyArt/Dark-Scarlett-27B-v2.0/raw/main/config.json) | revision `5aa0350...`、main Q4_K_M と専用 `MMPROJ-Dark-Scarlett-27B-v2.0-Q8_0.gguf` の exact size/SHA-256。source config は `language_model_only: false` と `vision_config` を宣言。 |
| [ReadyArt Dark Scarlett 26B Q4_K_M](https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF/blob/main/Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf) / [source card](https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B/blob/main/README.md) / [config](https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B/blob/main/config.json) | `Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf`、表示サイズ16.8 GB、Xet hash `60db66f9182a4f005ec34363fa29daca83559daeccf319660cea6859edefe6a0`、SHA-256 `ad9bd7a894fe7323bb152c78ea4c571e094a3bfb991464fc893c5301957eed92`、`max_position_embeddings=262144`、personal/non-profit/non-commercial usage language。 |
| [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) / [multimodal docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md) | 原文: `-mm, --mmproj FILE` は `path to a multimodal projector file`。multimodal docs は `-m model.gguf` と `--mmproj file.gguf` の組み合わせ、および OpenAI-compatible `/chat/completions` を明記。 |
| [llama.cpp function calling docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md) | server 側の function-calling と chat template の扱い。AoiTalk 側で parser を推測しない根拠。 |

通常版 Qwen3.8 と Dark Scarlett 27B v2.0 は exact mmproj を profile metadata で管理し、
image capability を有効化します。Heretic legacy Q4_K_M、Dark Scarlett 26B、Flash-Next
などは、対象 GGUF と sidecar の互換性が確定した場合だけ同様に有効化してください。
model ID の文字列判定や別モデルの mmproj 推測流用は禁止です。

## 10. Qwen3.8-Flash-Next / Uncensored IQ4_XS 正式対応

[ggml-org/llama.cpp PR #27742](https://github.com/ggml-org/llama.cpp/pull/27742)
（model: add Qwen3.8-Flash-Next (qwen4exp)）は 2026-08-27 に merge 済みです。
merge commit は
`6c84c7d5d8833c6e0df69628f75a0f599797934e`
です。

公式 release [b10660](https://github.com/ggml-org/llama.cpp/releases/tag/b10660) が
qwen4exp / Qwen3.8-Flash-Next 対応を含む最初の build です。AoiTalk は
llama.cpp **b10660 以上**を要求します。特定の commit SHA には固定せず、
b10660 より新しい official build も利用できます。

aligned profile のモデルは [unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
の `UD-IQ4_XS/` 配下にある次の 3 shard です。

| shard | 概算サイズ |
| --- | ---: |
| `Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf` | 約 10.9 MB |
| `Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf` | 約 49.8 GB |
| `Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf` | 約 43.8 GB |

3 shard 合計は約 93.7 GB です。`model_path` には
`Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf` を primary として指定し、
残り 2 shard も basename を変えずに **同一 directory** へ配置します。3 file 全てが
必須です。

AoiTalk profile の起動 context default は **16384**、native context は **262144** です。
`gpu_layers` default は `auto` で、argv には `--n-gpu-layers auto` と
`--jinja` が含まれます。固定の `--n-gpu-layers 999` は使用しません。

実機検証では次を確認し、証跡を残します。

1. 使用した official build の `--version` と `--list-devices` を記録する。
2. primary shard を `--model` に指定し、同一 directory の 3 shard で load を確認する。
3. `--ctx-size 16384`、`--n-gpu-layers auto`、`--jinja` の起動条件を確認する。
4. 証跡は `artifacts/qa/<run-id>/`、AoiTalk の server log は
   `logs/models/llama_cpp.log` に残す。
5. 検証終了後は起動した `llama-server` process を停止する。

upstream が vision をサポートしていても、AoiTalk の現行 profile は mmproj を管理せず
vision を有効化しません。context の引き上げも行いません。Flash-Next の MTP は、次の
暫定 embedded 契約に限って扱います。

### Qwen3.8-Flash-Next MTP / NextN（PR #27836 暫定対応）

`qwen3.8-flash-next` の通常推論契約は引き続き公式 llama.cpp **b10660 以上**です。
profile 最上位の `minimum_llama_cpp_build=10660` は変更せず、最上位へ PR の commit
pin を追加しません。2026-08-28 時点で qwen4exp の NextN/MTP 対応は
[llama.cpp PR #27836](https://github.com/ggml-org/llama.cpp/pull/27836) がまだ
open/draft のため、MTP だけが次の exact commit を要求します。

```text
1d8de7c1b0c7d2febf8f983174d8e6a711e2b1af
```

MTP は既定 OFF です。ON にしても、次の全条件が揃わない場合は元の 3-shard base を
通常モードで起動し、MTP だけを unavailable / unsupported_build として扱います。

1. 元の 3-shard（`qwen3.8-flash-next\main`）に reference head を graft して生成した
   exact 4-shard variant が `qwen3.8-flash-next\mtp` に完全に存在する。4 件の variant
   自体は同じ directory に配置し、元の 3-shard は変更しない。
2. `llama-server` が上記 PR commit と互換である。
3. CLI が `--spec-type draft-mtp` を advertise する。

derived variant のファイル名は次の 4 件に固定されます。元の 3-shard は変更せず、
この variant を通常の Hugging Face managed download として取得することもありません。

```text
Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00001-of-00004.gguf
Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00002-of-00004.gguf
Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00003-of-00004.gguf
Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00004-of-00004.gguf
```

variant が ready のときだけ起動 argv は次の形になります。

```text
--model <...-MTP-00001-of-00004.gguf>
--spec-type draft-mtp
```

embedded 方式なので `--spec-draft-model` は付けません。`--spec-draft-n-max` は
profile/API の設定にせず、必要な比較は `scripts/prepare_qwen38_flash_next_mtp.py`
で準備した実環境の benchmark だけで行います。別 fork / 別 PR 向けの MTP head を
この target と混ぜないでください（標準 Qwen head を OrcaRouter の uncensored target
へ流用することも禁止です）。

variant の準備は、base primary を指定して次の deterministic helper を使います。外部の
PR source checkout にある `gguf-py` を使い、reference repository の immutable revision・
head SHA-256・graft script SHA-256を検証してから実行します。Windows では POSIX の
`cp --reflink` を呼ばず、安全な通常コピーへ置換します。

```powershell
venv\Scripts\python.exe scripts\prepare_qwen38_flash_next_mtp.py `
  --base-primary "<model-root>/qwen3.8-flash-next/main/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" `
  --source-root "<llama-cpp-source-root>/pr27836" `
  --reference-cache "<qa-model-root>/qwen3.8-flash-next-mtp-qa/reference"
```

helper の完了後、4 件の GGUF metadata と `graft verification` の PASS、元 3-shard の
SHA-256 不変を確認してから MTP を ON にします。準備した巨大 artifact は repository
外の QA volume に置き、`artifacts/qa/<run-id>/` には version、argv、ログ、benchmark
結果だけを保存してください。

現行 reference の固定値は次のとおりです（upstream が merge/release されたら再確認
して更新します）。

```text
HF revision: 159500b54d79ffc008400c60ba1024bf745042ee
head: mtp-Qwen3.8-Flash-Next-Q8_0.gguf
head size: 4,135,893,152 bytes
head SHA256: ee87df0ecae89d759758667e9a1012d09299806f7805efc266eb5fe84773d4c9
graft-mtp-shard.py SHA256: 431ee3ff35306a3710f49f00140d4346b157a5acb5089c53d55218e1ffb88898
```
