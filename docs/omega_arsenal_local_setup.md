# Omega Arsenal Final を AoiTalk で使う

AoiTalkの既存 `openai_compatible_local` / managed `llama_cpp` 経路に、ReadyArtの
Omega Arsenal Final GGUFを2種類登録しています。専用provider、別downloader、既定モデルの
切替はありません。profile ID、served alias、artifact固定情報は
`LLAMA_CPP_MODEL_PROFILES` が正本です。

## 登録profile

| 優先順 | profile ID / served alias | GGUF | 固定revision | サイズ | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| 初回候補 | `omega-arsenal-31b-final` | `Omega-Arsenal-31B-Final-Q4_K_M.gguf` | `b1decda3e339b3abc527df8910a64a6b642b63c0` | 18,687,064,640 bytes | `c981b6020da8598b81aa819f5f9c1d355768f2824d66907213f5774ae94d92f5` |
| 任意 | `omega-arsenal-26b-a4b-final` | `Omega-Arsenal-26B-A4B-Final.i1-Q4_K_M_hb16.gguf` | `15337f996a9a5b729da068fcd50f2b2f3677f04d` | 17,666,860,576 bytes | `b7ac800aa1cc690535f700028974b37d8eb61dca270682d8a59544797b4bdec8` |

どちらも `gemma4`、native context 262144、AoiTalk起動default 32768、stock
`llama.cpp`、`--jinja` 必須です。reasoning、tool calling、image、audioはprofile上で
無効です。mmprojとMTP artifactはありません。モデル固有のminimum llama.cpp buildは
配布元資料に根拠がないため設定していません。OpenAI-compatible Chat Completionsを使います。

初回は31BのQ4_K_Mを候補にしています。26B A4B版は任意で、今回ダウンロードしていません。
両方ともカタログから選択できますが、選択可能であることはartifactの準備完了を意味しません。

## 保存先と利用手順

モデル本体は [ローカルLLMストレージ運用規約](local_llm_storage_policy.md) に従い、物理パス
`F:\AI\models\Cold_F\llm` 配下へ保存します。準備前に実効model rootと空き容量を確認します。
この端末では18.7 GBのartifactに加えてdownload staging用の空き容量も必要です。Dドライブや
repository内にはGGUFを置きません。

1. このprofileを含むAoiTalkソースを通常の起動手順で読み込んだ後、設定のLLMでproviderを
   `openai_compatible_local` にし、`Omega Arsenal Final 31B Q4_K_M` を選択します。
2. model rootが上記の物理パスになっていることを確認し、既存の「準備／ダウンロード」操作を
   実行します。managed managerはprofileに固定されたrepository、revision、filenameを使い、
   完了時にsizeとSHA-256を検証します。準備完了前のモデルはreadyとして扱われません。
3. 41でmodel準備とruntime ensureが成功した後、86 Novel Graph Studioの共有ローカルLLM一覧を
   更新します。86は41の既存shared coordinatorからprofileとserved modelを取得します。

ソース統合だけでは、artifact、runtime、実際のgenerationが検証済みとはみなせません。
41の現在のlocal-runtime statusを別途確認してください。

## 推奨sampling値

Omega 2 profileは信頼済みprofile metadataの `apply_sampling_defaults=true` で明示opt-inし、
配布元推奨の `temperature=1.0` と `top_p=0.92` を使います。Omegaをreasoning/always-thinking扱い
には変更しません。requestでtemperatureを省略した場合は1.0を送り、callerが指定したtemperature
（例: 0.7）はその値を保ちます。`top_p` は `extra_body` へ入れ、設定済みの明示 `extra_body.top_p`
があればその値を優先します。

managed llama.cppのprofile起動defaultにも `--temp 1.0 --top-p 0.92` を追加しています。通常の
起動plannerでは `extra_args` が未設定のときにprofile defaultsを使い、明示されたextra argsが
ある場合はその指定を優先します。shared snapshotは `--temp` と `--temperature` の両方を
同一optionとして認識し、`--temp=value` / `--temperature=value` 形式も含めてoperatorの明示値を
優先します。これによりprofile defaultsは重複追加されず、`--jinja` は引き続き必須です。

## ライセンス

各GGUF repositoryのmetadataにはApache-2.0とありますが、同revisionの `LICENSE.txt` とREADMEは
18歳以上、個人利用、非商用利用に限定する条件を記載しています。profileとUI noticeにはこの
制限を表示しています。利用前に配布元の原文を確認してください。AoiTalkのPublic / Enterprise
配布物にはモデル重みを含めません。

## 一次情報

- [31B GGUF repository](https://huggingface.co/ReadyArt/Omega-Arsenal-31B-Final-GGUF)
- [31B pinned tree metadata](https://huggingface.co/api/models/ReadyArt/Omega-Arsenal-31B-Final-GGUF/tree/b1decda3e339b3abc527df8910a64a6b642b63c0)
- [31B LICENSE.txt](https://huggingface.co/ReadyArt/Omega-Arsenal-31B-Final-GGUF/blob/b1decda3e339b3abc527df8910a64a6b642b63c0/LICENSE.txt)
- [26B A4B GGUF repository](https://huggingface.co/ReadyArt/Omega-Arsenal-26B-A4B-Final-GGUF)
- [26B A4B pinned tree metadata](https://huggingface.co/api/models/ReadyArt/Omega-Arsenal-26B-A4B-Final-GGUF/tree/15337f996a9a5b729da068fcd50f2b2f3677f04d)
- [26B A4B LICENSE.txt](https://huggingface.co/ReadyArt/Omega-Arsenal-26B-A4B-Final-GGUF/blob/15337f996a9a5b729da068fcd50f2b2f3677f04d/LICENSE.txt)
