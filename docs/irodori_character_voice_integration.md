# キャラクター音声と Irodori-TTS v3 / v4.1

キャラクター設定画面の Irodori-TTS 部分は、モデル selector と参照音声資産を管理し、`character.voice_parameters` を合成条件へ渡すための統合です。既定 checkpoint は `Aratako/Irodori-TTS-v4.1-Small` ですが、Irodori-TTS v3 VoiceDesign (`Aratako/Irodori-TTS-600M-v3-VoiceDesign`) と既存の v2/v3 checkpoint の互換経路も維持しています。モデル・runtime・依存の詳細は [irodori_tts.md](irodori_tts.md) を参照してください。

## 依存と実行時互換

公式 Irodori-TTS upstream の基準 [`pyproject.toml`](https://github.com/Aratako/Irodori-TTS/blob/8224dafb46d0aba89209a8f905f1cb7e3299d9c1/pyproject.toml) は `transformers>=5.12.1,<6` / `sentencepiece<0.2`（`>=0.1.99,<0.2`）ですが、AoiTalk は MioTTS/yomi との共存を優先し、`transformers>=4.57.6,<6` / `sentencepiece>=0.2.1,<0.3` を採用しています。MioTTS/yomi-linter も同時に入れる環境では、それらの `<5` 制約との交差で transformers は 4.57.6 以上 5 未満になります。

v4.1 の ModernBERT checkpoint をこの transformers 4.x 経路でも読むため、vendored [`src/vendor/irodori_tts/model.py`](../src/vendor/irodori_tts/model.py) は `no_init_weights` を 4.x の `transformers.modeling_utils` からも import できるようにし、`rope_parameters` の `full_attention` / `sliding_attention` の `rope_theta` を 4.x の `global_rope_theta` / `local_rope_theta` へ変換します。v3 VoiceDesign を含む既存 checkpoint の互換経路はこの shim の追加後も削除していません。

なお、`.[irodori,miotts,yomi-linter]` 全体の `pip install --dry-run` は、既存の `openai` / `mem0ai` 競合で失敗します。したがってこの統合ページでは全 extra の dry-run 成功を前提にせず、Irodori の依存・runtime import（`tests/test_irodori_vendor.py`）と実モデル smoke を個別に確認します。`irodori` extra は DACVAE を含みません。setup.bat/setup.sh が DACVAE 固定 commit `414c20785fc3a28373073ea8ef7a1316eeeaca6e` と `descript-audiotools==0.7.2` を `--no-deps` で導入し、必要な runtime imports を明示 install します。`descript-audiotools` の `protobuf<3.20` と AoiTalk/mem0 の `protobuf>=5.29.6` を衝突させず、既存 protobuf を維持するためです。Hugging Face モデル取得は標準 cache 解決規則に従い、AoiTalk は `HF_HOME` / `HF_HUB_CACHE` を上書きしません。依存の詳細、初回取得/cache、失敗時の切り分けは [irodori_tts.md](irodori_tts.md#公式-upstream-と-aoi-talk-の依存レンジ) を参照してください。

## データの正本と永続化

### キャラクター行

ECC のキャラクター作成・更新 API は次の音声フィールドを受け付けます。

```json
{
  "voice_engine": "irodori_tts",
  "voice_name": "ずんだもん",
  "voice_id": "",
  "speaker_id": null,
  "voice_parameters": {
    "irodori_model": "v4.1-small",
    "caption": "明るく親しみやすく、自然に話す",
    "irodori_reference_assets": []
  }
}
```

`characters.voice_parameters`（JSON）が Irodori 条件と参照資産メタデータの正本です。専用の参照音声テーブルは作りません。`TTSManager` はこの JSON を通常のキャラクター `voice.parameters` へ変換し、次の値を runtime へ渡します。

| キー | 型 | 意味 |
| --- | --- | --- |
| `caption` | string | 声質・感情・話し方の説明。参照音声と併用可能 |
| `irodori_model` | `v4.1-small` / `v3-voice-design` | 人間向けモデル選択。未指定は v4.1 Small |
| `no_ref` | boolean | 参照を使わないことの明示。caption だけの Voice Design に使用 |
| `ref_wav` / `ref_latent` | string | 既存互換の単一 waveform / latent パス |
| `ref_wavs` / `ref_latents` | string[] | 順序付きの複数参照。waveform と latent は混在不可 |
| `irodori_reference_assets` | object[] | GUI 資産の ID・パス・長さ・順序を含む metadata 配列 |
| `seconds` | number | 固定出力秒数。通常は省略して v4.1 の duration predictor を使う |
| `duration_scale` | number | 予測出力時間の倍率 |
| `min_seconds` / `max_seconds` | number | runtime の推定出力時間の下限・上限 |
| `max_ref_seconds` | number または null | 参照上限。null は checkpoint metadata（v4.1 は 120 秒） |

`irodori_model` から具体的な checkpoint への解決は `src/tts/irodori_config.py` に集約しています。明示した `hf_checkpoint`（ローカル path を含む）または旧 `voice_design_checkpoint` は互換性のため selector より優先されます。たとえば `Aratako/Irodori-TTS-600M-v3-VoiceDesign` を指定した設定は v4.1 へ強制変更されません。旧単一参照キーも v3 互換のため削除していません。

### 音声ファイル

GUI でアップロードまたは PC スピーカー録音した音声は、サービスが decode・mono 化・PCM16 WAV 化してから保存します。`storage_root` と `AOITALK_DATA_DIR` のどちらも指定しない完全デフォルト（リポジトリの `data`）では、保存先は次の形式です。

```text
data/character_voice_assets/<character UUID>/<asset UUID>.wav
```

`voice_parameters.irodori_reference_assets` の各要素は、少なくとも次の metadata を持ちます（順序がそのまま合成順です）。

```json
{
  "id": "asset UUID",
  "display_name": "ずんだもん.wav",
  "relative_path": "data/character_voice_assets/<character UUID>/<asset UUID>.wav",
  "duration_seconds": 3.843333,
  "sample_rate": 44100,
  "channels": 1,
  "size_bytes": 123456,
  "source": "upload",
  "sha256": "...",
  "created_at": "2026-08-13T00:00:00+00:00"
}
```

明示的な `storage_root` または `AOITALK_DATA_DIR` を使う場合も、保存先は `<storage_root>/character_voice_assets/<character UUID>/` に固定されます。この場合、metadata のフィールド名は互換のため `relative_path` のままですが、containment 検証済みの canonical absolute path を保存・受理します。どちらのモードでも、解決後のパスが設定済み storage root とキャラクター固有ディレクトリの内側にあり、`.wav` であることを検証します（任意の外部パスは受理しません）。サービスは各ファイルを 120 秒以内、登録済み資産の**合計**を 120 秒以内に制限します。v4.1 の品質面では、同じ話者の短くきれいな複数 clip を合計 **約 30 秒**から試すことを推奨します。

## GUI 操作

Irodori 設定はキャラクター編集ダイアログの「音声」タブに表示されます。新規キャラクターは、先に保存して ID を発行してから次の操作を行います。

1. **D&D / ファイル選択**: 参照音声欄へ backend の安定契約である WAV / FLAC / OGG をドロップするか、クリックして複数選択します。サーバー側で decode し、mono / PCM16 WAV に正規化して保存します。
2. **一覧・再生**: 登録済み資産を長さ・サイズ付きで表示し、再生ボタンで API の WAV を取得して試聴します。
3. **削除**: ごみ箱ボタンで metadata とファイルを削除します。metadata が正本で、ファイル削除に失敗してもキャラクターからは参照されません。
4. **順序変更**: 上へ / 下へボタンで順序を変えると、`PATCH .../voice-assets/order` が `asset_ids` の新しい順序を保存します。Irodori はこの順序で clip を連結します。
5. **PC スピーカー録音**: 「PCスピーカー出力を録音」で WASAPI render-loopback デバイスを選び、録音開始 / 録音停止を操作します。これはマイクではなく、Windows で再生中の**システム出力**を録音します。完了した WAV は自動的に資産一覧へ追加されます。
6. **試聴**: 試聴テキストを入力し、「生成して試聴」を押すと、現在の参照音声（順序を含む）と caption で合成した WAV を再生できます。caption 欄は API の任意上書きにも対応します。

参照音声が 0 件でも caption + `no_ref` で Voice Design を試聴できます。caption は参照の「誰の声か」ではなく、「どう話すか」を書く欄です。

## API 一覧

以下は FastAPI の実パスです。ECC 認証が必要です。Next.js の設定画面から呼ぶ場合は、先頭に `/api/python-proxy` を付けた同じ suffix（例: `/api/python-proxy/characters/manage/...`）になります。

### キャラクター CRUD（音声フィールドを含む）

| method | path | body / 結果 |
| --- | --- | --- |
| `POST` | `/api/characters/manage` | `CreateCharacterRequest`。`voice_engine`、`voice_name`、`voice_id`、`speaker_id`、`voice_parameters` を任意指定。201 と `character` |
| `PUT` | `/api/characters/manage/{character_id}` | `UpdateCharacterRequest`。上記音声フィールドを部分更新。200 と `character` |
| `GET` | `/api/characters/manage/{character_id}` | ID または slug でキャラクター（`voice_parameters` を含む）を取得 |

### 参照音声資産

prefix は `/api/characters/manage/{character_id}/voice-assets` です。

| method | suffix | body / 結果 |
| --- | --- | --- |
| `GET` | `` | `{"success": true, "assets": [...], "total_duration_seconds": n, "max_duration_seconds": 120}` |
| `POST` | `` | multipart `file`（必須）、`display_name`（任意）。正規化して 201 と `asset` を返す |
| `GET` | `/{asset_id}` | `audio/wav` 本体（互換 endpoint） |
| `GET` | `/{asset_id}/content` | `audio/wav` 本体。GUI はまずこちらを試す |
| `DELETE` | `/{asset_id}` | metadata から外し、ファイルを削除。削除した `asset` |
| `PATCH` | `/order` | JSON `{"asset_ids": ["id-1", "id-2"]}`。登録済み ID 全件を指定し、順序を保存 |

### PC スピーカー（WASAPI render-loopback）

同じ prefix に続けます。

| method | suffix | body / 結果 |
| --- | --- | --- |
| `GET` | `/devices` | 利用可能な render-loopback デバイス（`id`、`index`、`name`、`sample_rate`、`is_default`） |
| `POST` | `/capture/start` | JSON `{"device_id": "..."}`（省略で既定出力）。旧 client の `device_index` も受理。201 と `capture_id` |
| `POST` | `/capture/{capture_id}/stop` | 録音停止し、WAV を正規化して `capture` と `asset` を返す |
| `GET` | `/capture/{capture_id}` | 録音状態を取得。ready になれば資産登録を確定する場合がある |

録音は 1 件ずつ、最大 120 秒です。`pyaudiowpatch` が提供する loopback デバイスだけを列挙し、マイクデバイスへフォールバックしません。

### Irodori 試聴

| method | path | body / 結果 |
| --- | --- | --- |
| `POST` | `/api/characters/manage/{character_id}/voice-assets/preview` | JSON `{"text": "読み上げ文", "caption": "任意の上書き", "irodori_model": "v3-voice-design"}`。`text` は必須。selector は未保存の編集中 override として任意指定でき、`audio/wav` を返す |

試聴 endpoint はキャラクターの `voice_parameters.irodori_reference_assets` を ID 順に解決し、`caption`（body の値が優先）と selector（body の値が優先）を同じ Irodori engine へ渡します。既存 live engine の checkpoint が要求値と異なる場合は再利用せず、preview 専用 engine を遅延初期化します。重いモデルロードは request worker thread で実行します。

## 通常の合成経路

各発話の `TTSManager.synthesize()` は `character_name` がある場合、ECC DB の音声設定を best-effort で再取得して in-memory のキャラクター snapshot を更新します。GUI 保存後の caption や参照資産の変更も、同じ実行中のキャラクターの次の発話から即時反映されます。DB の timeout・障害・一時的な取得失敗時は最後の cached snapshot を保持し、既存の engine で合成を継続します。DB の `voice_engine` が `irodori_tts` を選択した場合は、既存 engine を再利用し、未登録なら Irodori engine を lazy create/register して current engine を切り替えます。Irodori 以外でも、preferred engine が通常の voice-chat lifecycle に登録済みなら次の発話でその engine へ切り替えます。明示された未登録の非 Irodori engine については、current が Irodori のとき誤った声を出さないよう current engine を解除して fail-closed（「No TTS engine available」経路）にします。別 engine が既に active の場合は、その lifecycle を壊して強制切替しません。

1. 更新済み（または DB 障害時は cached）のキャラクター `voice_parameters` を読む。
2. `irodori_reference_assets` を保存順に `relative_path` から解決する（明示した `ref_wavs` / `ref_wav` があればそちらを優先）。
3. `TTSManager` が selector を checkpoint へ解決し、`caption`、`no_ref`、duration などと一緒に一致する `IrodoriTTSEngine.synthesize()` を呼ぶ。
4. engine が解決済み checkpoint から `RuntimeKey.checkpoint` を作り、`SamplingRequest` とともに vendored `src/vendor/irodori_tts/inference_runtime.py` へ渡す。runtime lease が推論中の旧 runtime unload と競合しないよう切替を直列化する。
5. runtime が各参照 clip を encode、順序どおり latent を連結し、v4.1 の text/reference/caption 条件で合成して WAV bytes を返す。

参照を使わない text-only は `no_ref=True`、caption-only は `no_ref=True` + caption、参照 + caption は `ref_wavs`（または旧 `ref_wav`）+ caption です。`seconds` を指定しない場合は v4.1 duration predictor が出力時間を推定します。v3 を明示した場合も同じ TTSManager の旧単一参照経路を使えるため、既存キャラクターを作り直す必要はありません。

## 既存のずんだもん WAV を登録する例

現在のローカル環境に [`config/irodori_refs/ずんだもん.wav`](../config/irodori_refs/ずんだもん.wav)（約 3.84 秒）が存在する場合は、次のいずれかでローカル smoke に利用できます。この WAV は `*.wav` の gitignore 対象であり、clone に必ず含まれる追跡資産ではありません。ファイルがない環境では、自分で用意した参照音声へパスを置き換えてください。

### GUI から登録（推奨）

1. キャラクターを一度保存する。
2. 音声タブの参照音声欄へ `config/irodori_refs/ずんだもん.wav` を D&D（またはクリックして選択）する。
3. 一覧で再生して確認し、必要なら caption を入力する。
4. 「生成して試聴」で text + 参照 + caption の経路を確認する。

アップロード後の正本は、完全デフォルトでは `data/character_voice_assets/...`、明示的な `storage_root` / `AOITALK_DATA_DIR` では設定 root 配下の canonical absolute path と、`voice_parameters.irodori_reference_assets` です。元ファイルの場所を DB に直接書き換えないでください。

### 旧単一参照の直接 smoke

GUI を使わず adapter の互換経路を確認するだけなら、`irodori_tts.md` のスクリプトで次を指定します。

```python
audio = await engine.synthesize(
    "こんにちは、ずんだもんです。",
    ref_wav="config/irodori_refs/ずんだもん.wav",
    caption="明るく親しみやすく話す",
)
```

この `ref_wav` は v3 VoiceDesign を含む既存設定との互換用です。GUI の複数資産を使う通常経路では `irodori_reference_assets` → `ref_wavs` が正本になります。

## Windows / 非 Windows の制約

- Irodori 合成は Windows、Linux、WSL、macOS（CPU/MPS）で動かせます。device が利用できない場合は CPU へフォールバックします。
- PC スピーカー録音だけは **Windows + WASAPI + PyAudioWPatch** が必須です。Linux / WSL / macOS の capture endpoint は 503 を返すため、音声ファイルをアップロードしてください。
- Windows で `PyAudioWPatch` が入っていても、出力デバイスが loopback として列挙されない場合は OS のサウンド設定を確認し、画面のデバイス更新を押してください。
- モデルは日本語入力を前提とします。checkpoint の配布条件・MIT ライセンス・音声の本人同意などは [v4.1 model card](https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small) の制限事項に従ってください。

## トラブルシューティング

| 症状 | 確認事項 |
| --- | --- |
| 参照音声が 120 秒超で登録できない | 各ファイルと `total_duration_seconds` の合計を確認。不要な asset を削除するか、短い clip に分ける |
| 順序変更後に元へ戻る | `PATCH /voice-assets/order` へ登録済み ID を**全件**、希望順で送る。DB の `voice_parameters` が正本 |
| preview が「エンジン利用不可」 | Irodori extra と torch/torchaudio を venv に入れ、ログの初回 checkpoint/codec 取得を確認 |
| `no_ref` の競合エラー | `no_ref: true` と `ref_wav*` / `ref_latent*` を同時指定しない。caption-only は no_ref + caption |
| `reference audio not found` | GUI 資産は `voice_parameters.irodori_reference_assets` の ID と、設定済み storage root 配下の実ファイル（完全デフォルトは `data/...`、custom root は metadata の canonical absolute path）を確認。旧設定は `config/irodori_refs` の `voice_name` / `character_name` 検索を確認 |
| 初回だけ非常に遅い / cache が大きい | v4.1 model 約 3.06 GB + tokenizer + codec 約 430 MB。初回は取得・ロード・GPU 転送を待つ。cache とディスク空きを確認 |
| caption 併用で音質が不安定 | 参照と矛盾しない caption（感情・話し方中心）に短く書き換える |
| PC スピーカー録音が失敗 | マイクではなく出力 loopback を選択しているか確認。Windows 以外は upload を使う |
