# Operations media asset の結合契約

担当: backend は `media_asset_service.py` / `media_asset_routes.py` / `media_asset_schemas.py` / `media_assets.py` と対象 tests。publisher の `media_provider_execution.py` は別担当。

- `GET /api/operations/media/assets/capabilities` (`media_asset_capabilities`): 上限と MIME。
- `GET /api/operations/media/assets?persona_id=<UUID>` (`media_list_assets`): persona scope の登録済み asset。
- `GET /api/operations/media/asset-sources?persona_id=<UUID>` (`media_list_asset_sources`): 既存 GeneratedMedia / Files の画像・動画 / GenerationOutput の ID と metadata。
  一部の許可済み storage が offline / busy の場合も、利用可能な候補を `200` / 従来の JSON 配列で返す。その場合だけ `X-Media-Asset-Sources-Partial: true` と `X-Media-Asset-Sources-Warning: storage_unavailable` を付ける。UI は「一部の保存先を取得できませんでした」と注意を表示する。Web proxy はこの2 headerを転送する必要がある。未許可 root は列挙せず、権限・unsafe path エラーは部分結果として隠さない。選択した offline 素材の登録は `503` で拒否し、別の利用可能 root の登録は妨げない。
  offline / busy の登録拒否は共通5xx boundaryでも `detail.category=storage_unavailable` と `detail.code=storage_unavailable|storage_busy` を維持し、固定の日本語案内だけを返す。Web client はこの code から案内を表示し、返された自由文・path・endpoint を表示しない。未定義の503は従来の汎用案内、権限403は従来の拒否を維持する。
- `POST /api/operations/media/assets/register` (`media_register_asset`): JSON `{persona_id, content_item_id?, expected_content_item_hash?, source_kind: generated_media|library_file|generation_output, source_id, expected_sha256?}`。`Idempotency-Key` 必須。path / URL / secret を受け付けない。
- `POST /api/operations/media/assets/upload` (`media_upload_asset`): raw binary stream。query は `persona_id`, `content_item_id?`, `expected_content_item_hash?`。実 MIME の `Content-Type` と `Idempotency-Key` 必須、`X-Asset-SHA256` 任意。JSON body / base64 / multipart を使わない。
- `GET /api/operations/media/assets/{generation_output_id}` (`media_get_asset`): immutable receipt。
- `GET /api/operations/media/assets/{generation_output_id}/preview?expected_sha256=<SHA>` (`media_preview_asset`): 認証、owner/project ACL、実 bytes の検証、Range / 206 / 416、nosniff、private no-store。

receipt は `asset_revision_id`, `generation_output_id`, `persona_id`, `content_item_id`, `owner_user_id`, `project_id`, `source_kind`, `source_id`, `sha256`, `mime_type`, `size_bytes`, `revision_hash`, `preview_url`, `created_at`。revision は変更しない。ContentVariant の media / media_asset / thumbnail には `generation_output_id` を渡し、`generation_output_refs` は `{generation_output_id, sha256}` を pin する。登録だけでは QA / rights / 公開承認を付与しない。

内部 reader:

```python
from src.services.media_asset_service import MediaAssetService

asset = await MediaAssetService().materialize_for_provider(
    session, generation_output_id,
    owner_user_id=revision.owner_user_id,
    project_id=revision.project_id,
    expected_sha256=pinned_sha256,
    target=provider_temporary_path,
)
# asset.path / asset.sha256 / asset.mime_type / asset.size_bytes
# asset は内部オブジェクト。path を receipt / log / API に出さない。
```

reader の引数と戻り値は維持する。`GenerationOutput.byte_binding_state` の `managed` marker を byte ledger と別に記録する。`None` は存在・owner/project・pin SHA を再確認した `external` / run 付き / ledger のない output に限定する。登録済み asset の ledger 欠落・改変・scope mismatch、output 自体の削除は fail closed。`None` の場合のみ既存 public HTTPS reader を利用できる。private bytes を公開 URL として捏造しない。既存 public HTTPS SSRF guard を変更しない。

保存は既存 `generated_media_storage` / `StorageRoot` を利用する。画像 PNG/JPEG/WebP/GIF は25 MiB、動画 MP4/WebM/QuickTime は512 MiB。upload の request stream を上限付きで読むため16 MiB JSON bodyへ詰めない。SVG / HTML / 任意 filesystem path は対象外。

private Studio の取得は server 設定 `AOITALK_GENERATION_STUDIO_ASSET_ORIGIN` の完全一致 origin と、server 設定の Studio 認証情報に限定する。ユーザー入力 URL への token 送信、redirect、localhost を許す一般 SSRF guard 変更は行わない。保存済み run の `/api/v1/jobs/{run_id}` から asset/version/SHA が一致した `content_url` を読む。Studio 取得は GET のみ。

Studio 認証情報は Preset 取得・run 状態取得・生成物登録・素材取込で共通の供給元（`src/services/generation_studio_auth.py`）から得る。

- `AOITALK_GENERATION_STUDIO_BEARER_TOKEN`（別名 `GENERATION_STUDIO_BEARER_TOKEN`）: 固定 Bearer。設定されていれば常にこれを使い、再発行しない。
- `AOITALK_GENERATION_STUDIO_BOOTSTRAP_TOKEN`: 73 の `bst_` bootstrap token。固定 Bearer が未設定のときだけ使い、`POST /api/v1/auth/session` で15分の access token を発行する。Workspace の 73 Project ID が session の既定 Project と異なる場合は `POST /api/v1/session/project` で切り替える。access token はプロセス内メモリに (origin, Project) 単位でだけ保持し、期限の60秒前、または 73 が401を返したときに1回だけ再発行して再試行する。73 の refresh token は保持しない。
- どちらも未設定なら Studio 連携は利用不可のまま。token・bootstrap token はログ・例外・API 応答に出さない。

Preset 一覧（`GET /api/operations/media/automations/generation-studio/{workspace_id}/presets`）は、73 から取得できない場合に空配列を返さず 503 と理由コード（`studio_auth_failed` / `studio_unconfigured` / `studio_unavailable`）を返す。空配列は 73 が Preset 0件と応答した場合だけ。素材取込は認証失敗を `asset_studio_auth_failed`、その他の取得不能を `asset_studio_unavailable` とする。

Web 結合: `python-proxy` の当該 upload は binary request stream を上限付きで転送し、当該 preview は response を buffer せず Range / If-Match を転送する。既存 Operations proxy の通常 JSON body 上限・8 MiB response buffer をこれらの byte routes に流用しない。Mobile は同じ FastAPI API を認証済み direct base URL から利用する。body metadata の URL/path 入力は不要。

Library source は既存 Files の `_users/user_<owner UUID>`、当該 `_projects/project_<project UUID>`、明示登録された Files storage の実ファイルを列挙する。source ID は root identity / owner / project / 論理相対名から server が導出する UUID。register は ID のみ受け付け、同じ ACL と storage を再検証して解決する。任意 absolute path の admin bypass は設けない。`KnowledgeDocument.content_hash` は抽出 text の hash であり byte SHA として使用しない。Files 一覧の `sha256` は未計測なら null、登録 receipt の SHA は実 bytes から算出する。登録は source bytes の独立した immutable snapshot を作るので、元ファイルを編集・削除しても既に承認した bytes がすり替わらない。
