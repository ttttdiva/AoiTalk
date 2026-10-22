# Hemmingway-1 managed llama.cpp setup

AoiTalk は `Altworld/Hemmingway-1` を既存の `openai_compatible_local` + managed
`llama_cpp` 経路で扱います。専用 engine/provider や専用 downloader はありません。model
catalog、WebUI、managed download、`llama-server` の起動 argv は
`LLAMA_CPP_MODEL_PROFILES["hemmingway-1"]` を共通の正本として参照します。

## 固定された artifact contract

| 項目 | 値 |
| --- | --- |
| profile ID / served alias | `hemmingway-1` |
| upstream model | `Altworld/Hemmingway-1` |
| GGUF repository | `bartowski/Altworld_Hemmingway-1-GGUF` |
| pinned revision | `259605e26a47f2dcc8731ae1aa7305f8047d2c6d` |
| filename | `Altworld_Hemmingway-1-Q4_K_M.gguf` |
| quantization | `Q4_K_M` |
| exact size | `17,442,400,320` bytes |
| SHA-256 | `c029ba6cc24bc022a65d00e4d81796ff0bd678e371a36c9a7d1119a0acc7085e` |
| upstream weights license | CC BY-NC 4.0（商用利用は別途合意が必要） |

`source_revision`、filename、size、SHA-256 は Hugging Face の artifact metadata
から固定した値です。managed prepare は caller が渡した repository/path を信用せず、registry
の contract と一致する artifact だけを取得・検証します。

ライセンスについては、固定したGGUF revisionのmodel cardに古い `apache-2.0` metadataが
残っていますが、元モデルの現行READMEと`LICENSE`は **CC BY-NC 4.0** を明示しています。
AoiTalkでは安全側に元モデルの現行LICENSEを表示し、非商用利用を前提とします。商用利用には
Altworldとの別途合意が必要です。

## モデル特性と AoiTalk default

- 27B parameters、Qwen3.8-27B 派生、text-only。
- native context は `262144`。AoiTalk の起動 default は、通常利用時のメモリ消費を抑える
  `32768`。
- reasoning と native tool calling を有効化し、`reasoning_effort` は
  `low` / `medium` / `xhigh`（default `xhigh`）を profile metadata から渡す。
- image/audio は非対応。画像・音声payloadをこのprofileへ送らない。
- English-first の文章作成fine-tuneであり、日本語は実環境QAの対象にするが、日本語特化
  モデルとしては表示しない。

## llama.cpp と MTP

独立した版別runtimeの準備と、既存の外部exe指定による可逆な切替は
[runtime更新手順](llama_cpp_runtime_update.md)を参照する。

GGUF 配布元は llama.cpp `b10964` で量子化し、MTP layers が同じ GGUF に含まれること、
利用時に `--spec-type draft-mtp` を付けることを明記しています。そのため AoiTalk は
Hemmingway-1 profile にだけ次の保守的な条件を設定します。

- `minimum_llama_cpp_build = 10964`
- `reasoning_tools_minimum_llama_cpp_build = 10964`
- MTP `minimum_llama_cpp_build = 10964`
- MTP mode は `embedded`、`embedded_in_primary = true`
- MTP default は ON

別のMTP sidecar、任意のdraft model path、`--spec-draft-model` は使いません。MTPをOFFに
した場合、またはruntime capability/buildを確認できない場合は、MTP flagsを外して同じ
primary GGUFを通常推論で起動します。`--jinja` はprofile-owned required/default argとして
重複なく1回だけ追加されます。

`b10964` は AoiTalk 全体の最低buildではなく、このprofileのartifact/runtime contractです。
他のmanaged profileはそれぞれの既存minimumを維持します。

## managed download と保存先

WebUIの「準備／ダウンロード」は `ManagedLocalRuntimeManager.start_prepare()` を呼び、次の
既存経路だけを使用します。

```text
WebUI
  -> POST /api/llm/local-runtime/prepare
  -> ManagedLocalRuntimeManager
  -> hf_hub_download(repo_id + pinned revision + exact filename)
  -> <model-root>/hemmingway-1
  -> size/SHA-256 verification
  -> managed llama-server
```

modelはGit非追跡のmanaged model rootに置きます。継続運用端末の保存先は
[ローカルLLMストレージ運用規約](local_llm_storage_policy.md)に従い、prepare前に外部rootを
確定します。repository直下や`.local`へ17GBの一時コピーを作らず、staging directoryから
同一volume上のatomic renameで確定します。既存の`HF_HOME` / `HF_HUB_CACHE`設定は変更しません。
GGUFをGit add、Release asset、Enterprise handoffへ含めないでください。

## WebUIでの利用

1. 「設定 → LLM」でproviderに `openai_compatible_local` を選ぶ。
2. modelに **Hemmingway-1 27B Q4_K_M**（ID `hemmingway-1`）を選ぶ。
3. runtime panelでexact GGUF、pinned revision、minimum build、text-only capability、
   `primary GGUF内蔵` MTPを確認する。
4. 準備／ダウンロードを実行する。完了後、runtime/model statusが`ready`になることを確認する。
5. 保存後、通常のチャットから会話する。served aliasはprofile管理対象で編集不可。

VRAM不足時は`gpu_layers`を下げるか`0`にしてCPU推論へ切り替えられます。native contextを
そのまま起動defaultへ引き上げず、必要な場合だけメモリを確認して`context_size`を変更します。

## 実推論の確認項目

managed prepare後は少なくとも次を確認します。

- `/v1/models` に `hemmingway-1` が現れる。
- 日本語promptへ日本語で応答できる。
- OpenAI-compatible `tools` payloadから`message.tool_calls`を返せる。
- MTP ONではargvに`--spec-type draft-mtp`があり、`--spec-draft-model`はない。
- MTP OFFではMTP関連argvがない。
- modelを停止・再起動してもprofile別設定が別modelへ漏れない。

## 一次情報

- [Altworld/Hemmingway-1](https://huggingface.co/Altworld/Hemmingway-1): base model、license、
  27B、262144 context、English-first writing fine-tune、現行licenseはCC BY-NC 4.0。
- [bartowski/Altworld_Hemmingway-1-GGUF](https://huggingface.co/bartowski/Altworld_Hemmingway-1-GGUF):
  Q4_K_M artifact、llama.cpp b10964、MTP layers内蔵、`--spec-type draft-mtp`。
- [GGUF API metadata](https://huggingface.co/api/models/bartowski/Altworld_Hemmingway-1-GGUF?blobs=true):
  pinned revision、exact byte size、LFS SHA-256、qwen35 architecture、262144 context。
