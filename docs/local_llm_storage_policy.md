# ローカルLLMストレージ運用規約

この文書は、AoiTalk が管理する llama.cpp / GGUF モデルの保存先と移設手順の正本です。
モデル本体、mmproj、MTP、分割GGUF、download stagingを、ソースcheckoutや容量を温存する
ドライブへ誤配置しないことを目的とします。

## 現行Windows開発端末の保存先

| 用途 | 正式な物理パス | 運用 |
| --- | --- | --- |
| llama.cppモデルroot | `<configured-model-root>` | 通常は必ずここを使う |
| 代替model root | `E:\AI\models\Cold_E\llm` | Fを使えない理由と空き容量を確認し、利用者が明示した場合だけ使う |
| AoiTalk checkout内 | `<repository>/.aoitalk-local-llm/models` | GGUF等の重みを置かない |
| 検証済みruntime cache | `<repository>/cache/local-llm-runtime` | Strata等の固定revision runtime専用。モデルを置かない |

驕主悉縺ｮ繝峨Λ繧､繝門挨alias繧・unction path縺ｯ縲∬ｨｭ螳壽ｸ医∩縺ｮphysical model root縺ｸ隗｣豎ｺ縺励※縺九ｉ菴ｿ逕ｨ縺励∪縺吶Ａnmodel root縺ｫ縺ｯalias邨檎罰縺ｮpath縺ｧ縺ｯ縺ｪ縺上∬ｨｭ螳壽ｸ医∩縺ｮphysical root繧呈欠螳壹＠縺ｾ縺吶Ａn迚ｩ逅・・鄂ｮ蜈医→遨ｺ縺榊ｮｹ驥上′譖匁乂縺ｫ縺ｪ繧九％縺ｨ縺ｫ蜉縺医、oiTalk縺ｮpath resolver縺ｯ
symlink/junction繧貞性繧root繧貞ｮ牙・荳頑拠蜷ｦ縺励∪縺・

llama-server等のmanaged runtime binaryはモデルとは別領域です。既定の
`<repository>/.aoitalk-local-llm/runtime`を使えます。検証済みの固定revision runtimeを
復元する場合に限り、専用の`<repository>/cache/local-llm-runtime`とそのruntime配下を使えます。
`cache`直下の他の場所やruntime cache内へモデルを置かず、モデルrootへ混在させません。
runtimeも外部化するときは、保存先を別途決めて
`AOITALK_LOCAL_LLM_RUNTIME_ROOT`で指定します。

## 必須設定

この端末の追跡しない`.env`には次を保持します。

```dotenv
LLAMA_CPP_MODEL_ROOT=<configured-model-root>
AOITALK_REQUIRE_EXTERNAL_LLM_MODEL_ROOT=true
```

Settingsの`model_root`も同じ物理パスへ保存します。環境変数はDB設定より優先されるため、
実効値の確認では入力欄だけでなく、`GET /api/llm/llama-cpp/model-root`が返す
`model_root`と`model_root_source`を確認します。

`AOITALK_REQUIRE_EXTERNAL_LLM_MODEL_ROOT=true`の端末では、環境変数・DB設定・明示overrideの
いずれから解決しても、repository配下のmodel rootを拒否します。model rootが空に戻った
場合もrepository-local defaultへfallbackせず、ダウンロード前にエラーで停止します。
このエラーを回避するためにguardを無効化せず、外部rootを復旧します。

## ダウンロード前の確認

managed prepare、`hf download`、手動コピーの前に次を確認します。

1. 実効model rootが上記Fの物理パスである。
2. 保存先ドライブに、完成artifactと`.partial` stagingの両方を置ける空き容量がある。
3. 同名モデルの既存GGUFが別rootにないか確認し、重複downloadを避ける。
4. `<repository>/.aoitalk-local-llm/models`にGGUFが増えていない。
5. runtime rootとmodel rootを同じdirectoryにしない。

サイズが不明なモデルは先に配布元metadataを確認します。「一時的だから」という理由で
`D:\Dev`、repository直下、`.local`、`temp`へ重みや全量コピーを作りません。

## 安全な移設手順

異なるvolume間では直接moveせず、次の順序を守ります。

1. destinationと同じvolume上の一時directoryへコピーする。
2. source/destinationのfile count、byte size、SHA-256を照合する。
3. 照合済みdirectoryをdestinationの最終名へ切り替える。
4. `.env`とDBのmodel rootをdestinationへ更新する。登録profileのDBに絶対`model_path`が
   残る場合はdestinationへ更新する。コード側も、外部root設定後はrepository-local旧root上の
   登録profile pathを引き継がず、新root内から再解決する。
5. 利用中の`llama-server`を停止し、新rootから同じprofileを起動する。
6. process command line、`/v1/models`、実リクエストで新rootとserved aliasを確認する。
7. ここまで成功してからsourceを削除する。

起動中GGUFを先に削除しません。コピー失敗、hash不一致、起動失敗のいずれかがあれば、
sourceを残したまま切り替えを中断します。Dark Scarlettのように既にdestinationに同一artifactが
ある場合も、ファイル名やサイズだけで重複と判断せずSHA-256一致を確認します。

## 変更時の記録

ドライブ構成や正式rootを変更した場合は、この文書、`.env.sample`の説明、対象端末の`.env`、
SettingsのDB値を同じ作業で更新します。実体のGGUF、hash manifest、個人環境の`.env`はGitへ
追加しません。
