# 保存領域の分類と整理方針

この文書は、AoiTalk が生成・保持するファイルを owner、用途、producer、run、保持期間で分類する。
`workspaces` はユーザーやアプリの正本ファイルを置く永続領域であり、汎用cache置き場として使わない。

## 分類表

| 用途・owner | 現producer | 推奨先 | 保持 | 互換・reader |
| --- | --- | --- | --- | --- |
| User Files / 個人チャット添付 | Explorer upload、`src/api/user/attachments/route.ts` | `workspaces/_users/user_<uuid>/...` を継続 | ユーザー所有の永続データ | `WorkspaceFileEntry.rel_path`、チャット履歴、rerun、searchが相対pathを参照。Explorer URLはworkspace相対pathを使う。移動時はreaderの解決互換が必要 |
| Project Files / チャット・タスク添付 | Explorer、Project upload、`src/api/routes/tasks/attachments_routes.py`、Project storage publisher | `workspaces/_projects/project_<uuid>/...` を継続 | Project所有の永続データ | Chatの`path` / `project_relative_path`、`TaskAttachment.file_path`、`RecordAttachment.file_path`を保持する。Task download URLはattachment IDを使う |
| Docs添付 | Docs upload / `knowledge_attachments.file_path` | `workspaces/_docs/attachments` を継続 | DB参照がある間保持 | `knowledge_attachments.file_path`が相対pathの正本。参照確認なしで移動しない |
| App source / owner App | `app_storage.ensure_app_workspace`、App source import/update | `workspaces/_apps/app_<uuid>` | Appの永続source | App IDとApp file APIが所有する。Files/Apps URL、Git revision、DB pathを維持する |
| App project instance / owner Project+App | `app_storage.ensure_app_instance`、App action runner | `workspaces/_app_instances/project_<uuid>/app_<uuid>` | run間も使うruntime state | instance、input/output/logsはProject/App単位で分ける。App sourceと混ぜない |
| App release artifacts / owner App+release | `app_release_service` | `workspaces/_app_artifacts/app_<uuid>/release_<uuid>` | immutable releaseとして保持 | Release DBの相対pathを維持する |
| App action run / owner run | `app_action_server_transport` | 既存の`workspaces/_app_action_runs`を維持 | journalは復旧用。run dirは完了後も現状自動削除されない | `server-actions.sqlite3`、run manifest、result pathを読むrecoveryがある。active run照合と参照棚卸しなしに移動しない |
| App action input/output files | `AppActionFileService` | 既存の`workspaces/_app_action_files`を維持 | input 1h / output 7dのアクセス期限。物理sweeperは未実装 | expiryはread/list時に確認される。現状の期限超過ファイルもユーザー採用済みoutputと分離してから扱う |
| Roleplay / DB管理生成Media | `generated_media_service` | `AOITALK_GENERATED_MEDIA_DIR`、未設定時`data/generated_media` | 成功行はowner/context bindingがある限り保持。failed/pendingは既定7日で整理 | DBの`relative_path`は`generated_media/<key>.<ext>`、公開URLは`/api/generated-media/{id}`。物理rootを変えてもDB pathとID URLを保つ |
| Gemini/ComfyUI tool出力 | `src/tools/image_generation.py`、`src/services/comfyui_service.py` | 正規media root。DB-backed生成の中間出力は`cache/generated_media_staging` | standalone tool出力はDiscord向けpathで提供。中間出力は1回のrun中だけ保持 | 既存の`[GENERATED_IMAGE:<path>]`はDiscord consumer向け。新規pathは一意化し、物理rootはmedia storage設定に従う |
| App source update preview | `AppSourceUpdateService.create_preview` | `cache/app_source_updates/<app>/<import>` | 2h preview。applyまたは期限切れで破棄 | 旧`workspaces/cache/app_source_updates`にある有効previewはreader fallbackで扱う。旧データ自体は移動しない |
| QA active run | `scripts/verification/ai_employee_qa.py::prepare` | `.local/qa-runs/aoitalk_test_employee_<uuid>` | DB/helper停止までのscratch | manifestはrun pathを固定する。cleanup後のevidenceは`artifacts/qa-runs/<run>`へ移す |
| QA/verification evidence | QA harness、検証script | 保持する報告は`artifacts/qa-runs`、一時ログは`.local`、運用ログは`logs` | artifactはreview後まで保持。scratchは作業終了時に削除 | manifest、明示的なdocs参照、run IDを確認してから扱う。既存の`D:/Dev/aoitalk_test_employee_*`参照は履歴として残す |
| 古い診断物・単発検証出力 | 個別verification/maintenance producer | producerが出す場所を`.local`、`logs`、`artifacts`の用途別に直す | 一時物はrun終了時削除。証跡はowner/用途を付けて保持 | root直下にある既存ファイルはこのphaseで移動しない。作成元・参照・サイズ等のmetadata dry-run後に候補を限定する |

## 互換と配置の不変条件

- Files/chatのpath参照とTask/Record attachmentのDB相対pathは現行値を保つ。既存API URLとDB pathを書き換える物理移動はしない。
- Generated MediaはDBの論理pathとID URLを保ち、物理rootだけを`generated_media_storage`で解決する。
- `_apps`、`_app_instances`、`_app_artifacts`、`_app_action_runs`、`_app_action_files`はそれぞれ所有・復旧・保持理由が異なる。`workspaces`全体を単一ownerや一律cacheとして扱わない。
- 認証設定、未送信の作業、DB、元原票、ユーザー採用済みmodel/artifactは初期化・削除しない。
- `.local`は今回の作業・QA runのscratch。cacheは再生成可能なruntime cache。`artifacts`は意図して保持する成果物。`logs`は運用・検証ログ。

## 旧データの監査

read-onlyのworkspace orphan一覧は次で出せる。通常起動経路ではなく、対象環境のworkspace rootを明示して実行する。

```powershell
python scripts/maintenance/cleanup_workspace_orphans.py --workspace-root "<AOITALK_WORKSPACES_DIR>"
```

このreportはDocs添付参照とProject/User存在に加え、`src/services/workspace_gc_references.py`の参照棚卸しを照合する。対象は次のとおり。

- Chat添付のselectorで取り出した`path`
- `AppArtifact`のfile path
- `AppJob` / `AppActionRun`の明示的なlog pathとrun identity
- `GeneratedMedia`のrelative path
- File Explorerのbookmark / launcher path
- external Files registryのroot
- 環境変数`AOITALK_EMPLOYEE_QA_MANIFEST`で明示した1つのQA manifest

次は現状の限界で、照合しない。

- 任意のChat message本文の走査
- App action journalの生データの走査
- AppJob / AppActionRunのinput・result payloadの走査
- QA manifest未指定時、および再帰的なQA探索

`--apply`はDocs添付とorphan Project/User rootを、`_recycle_bin/workspace_orphans/<batch>/payload`へ同volumeで移動しmanifestを記録する。上書きはせず、停止したworkspace writersの状態でだけ実行する。復元は同じ停止条件でbatch単位に行う。

```powershell
python scripts/maintenance/cleanup_workspace_orphans.py --apply --service-stopped --workspace-root "<AOITALK_WORKSPACES_DIR>"
python scripts/maintenance/cleanup_workspace_orphans.py --restore-batch "<batch-uuid>" --service-stopped --workspace-root "<AOITALK_WORKSPACES_DIR>"
```

`--apply`前にdry-run reportと全reader/DB参照を確認し、対象を限定する。削除候補はDocs添付とorphan Project/User rootだけで、App/run、Generated Media、外部Files rootは参照inventoryで保護するのみ。上記の未走査範囲に残る参照は検出できないため、実行前にreaderを確認する。このphaseではdry-runも`--apply`も実行せず、実データの移動は行っていない。

`workspaces` root自体を設定・存在確認する読取処理は副作用を持たせず、directory作成は明示的なwriterに限る。
