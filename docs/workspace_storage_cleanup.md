# ワークスペース保存領域の監査

`workspaces` はキャッシュではなく、DBと対応する永続ファイル領域です。保存先は用途ごとに分かれています。

- `_docs/attachments`: `knowledge_attachments.file_path` が正本のDocs添付
- `_projects/project_<uuid>`: Project共有領域。Project行と同じUUIDを持つ
- `_users/user_<uuid>`: ユーザー個人領域。User行と同じUUIDを持つ
- `_apps` / `_app_instances`: Appの正本とProject別実行状態。Docs添付やProject共有領域とは別のライフサイクル

未参照データの確認は、リポジトリルートで次を実行します。既定は読み取り専用です。

```powershell
python scripts/maintenance/cleanup_workspace_orphans.py
```

JSONを確認してから、Docs添付の未参照ファイル、DBに存在しないProject/Userの直下ディレクトリ、Docs添付内の空ディレクトリを削除する場合だけ`--apply`を付けます。

```powershell
python scripts/maintenance/cleanup_workspace_orphans.py --apply --service-stopped
```

この監査は、Projectの`attachments`やAppのworkspaceを一律に未参照扱いしません。これらは会話・タスク・App実行など別の台帳で管理されるためです。`--apply`は通常のファイル作成・削除処理と競合しないよう、サービスを停止した状態で`--service-stopped`を付けて実行してください。監査だけのdry-runはDBをread-only transactionで参照し、workspace rootを作成しません。
