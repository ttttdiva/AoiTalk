# 時間記録の削除・同期契約

## 稼働中タイマーと削除

`time_entries` の一意制約 `ix_time_entries_active_user` は、`ended_at IS NULL AND deleted_at IS NULL` の行だけを対象とする。ユーザーごとの稼働中タイマーは最大1件で、削除済み記録は枠を占有しない。

- 単独記録・タスクツリー・案件の削除は、未終了の時間記録を削除時点の deployment-zone 時刻で終了し、同一 transaction で tombstone 化する。既に終了した記録の時刻と元の開始時刻は維持する
- タスクの復元で現在のタイマーを停止したり、削除済みタイマーを再開したりしない
- 旧版の未終了 tombstone をタスクごと復元する場合は、その削除 batch に記録された UTC `deleted_at` を deployment-zone に変換し、終了時刻として使う。開始より前になる不整合があれば、データを変更せず409（`time_entry_restore_invalid_interval`）で中止する。既存の終了時刻は変更しない
- 古い記録を復元する際は、その記録の作成時と現在の `AOITALK_TIMEZONE` 設定が同一であることを確認する。既存の naive timestamp だけから過去の設定変更を判定できない

## 導入と既存データ

`20261002_0001_live_active_timer_index` は index の条件だけを変更する。削除済み工数、開始・終了時刻、note、metadata は一切書き換えない。既存の未終了 tombstone が残っていても、migration 完了後は新しいタイマーを開始できる。

通常のローカル更新時は、DBバックアップを確保してアプリを停止したうえで、既存の環境設定と root `venv` を使い `python scripts/init_db_schema.py` を実行する。この正規ルートは `alembic upgrade head` と head 検証を実施する。Windowsでは `venv\Scripts\python.exe scripts\init_db_schema.py`、Linuxでは `venv/bin/python scripts/init_db_schema.py`。`create_all`、`stamp head`、DB初期化による代用はしない。

index は PostgreSQL の通常の migration transaction 内で再作成されるため、その間の書き込みはロック待ちになり得る。稼働中アプリとの並行 migration は避ける。ダウングレード時に旧制約では共存できない未終了行がある場合は、記録を改変して合わせず明示エラーで止める。履歴を削除せず、更新版の維持または個別に確認した記録の修正を選ぶ。

旧版の同期に由来する時刻の9時間ずれや誤停止は、既存行だけでは正しい時刻や発生経路を確実に特定できない。この migration は推測補正しない。修復する場合は、元端末の記録など信頼できる根拠と照合して対象・正しい時刻を確定する。

## Native同期

- `started_at` / `ended_at` は通常の timer API と同じ parser を通す。offset付き入力は瞬間として deployment-zone に変換し、naive入力は従来の壁時計値を維持する。`base_updated_at` / `updated_at` 等の同期mutation clockはUTCのまま
- 終了済みの履歴 create は現在のタイマーに触れない
- 未終了 create が既存タイマーより新しい場合だけ、その開始時刻で既存タイマーを終了して切り替える。新規INSERT前に終了をflushし、稼働中一意制約を守る
- 既存タイマー以下の開始時刻を持つ未終了 create は409 conflictで拒否し、現在の記録を変更しない
- 既存UUIDへのcreate replay・update・deleteは、payloadではなく保存済みのtask/projectに対するwrite権限を最初に確認する。競合レスポンスのentityも、返却時にread権限を再確認する
- Nativeは既存の競合再試行契約を維持する。拒否した未終了createはoutboxに残り、端末で停止すればended_atが同じcreateへ合流して履歴として再送できる。サーバー側の新しいタイマーが先に停止した場合、次回の再送時に未終了createが受理される可能性があるため、競合は「利用者が解決するまで永久に拒否する」契約ではない
