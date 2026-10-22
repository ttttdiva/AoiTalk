# MediaOps Action 素材パッケージ v1

DLsite / FANZA の Action 詳細から「素材を確認・再読込」を選び、取得可能な素材と manifest を ZIP で保存する。ダウンロードは読み取りだけであり、承認、Attempt、Receipt、投稿数、投稿・審査・販売状態を変更しない。

## 収録対象と凍結版

- typed payload の `deliverable_package_ref` が ArtifactVersion の UUID または `artifact:<UUID>` である場合だけ解決する
- Artifact は Action と同じ owner / project に属し、SHA-256 が Action の `artifact_hashes` に固定されている必要がある。既存の認可済み content 読み取りで実 bytes の SHA-256 / size を再検証する
- hash に一致する Artifact を検索して対応付けない。GenerationOutput UUID、外部 `ast_` / `outv_`、URL、その他の opaque 参照は取得せず、用途と順序を残して未解決とする
- Action ID / action_version / payload_hash、source revision / hash を毎回検証する。改訂済み source や改変は 409、scope 不一致は 403。権限を失った素材を部分欠落に隠さない
- manifest は閉じた typed payload と source 情報だけを収録する。connection、credentials、実行キー、storage_ref、Artifact provenance は含めない

## HTTP 契約

1. `GET /api/operations/actions/{action_id}/package`
   - 必須 query: `expected_action_version`、`expected_payload_hash`
   - 応答: `manifest`、その canonical JSON の `manifest_sha256`、安全な ZIP `filename`
2. `GET /api/operations/actions/{action_id}/package/content`
   - 上記 query に `expected_manifest_sha256` を加える
   - preview と取得時の manifest が変われば 409。`application/zip` と `X-MediaOps-Manifest-SHA256` が一致した応答だけを Web が保存する

どちらも認証・read ACL を要求し、`private, no-store` で返す。ZIP 応答は attachment / nosniff / sandbox CSP を付ける。共有 OpenAPI が契約の正本で、Web / Mobile の型は通常の生成手順で更新する。

## manifest と ZIP

`schema_version` は `mediaops.action-package.v1`。`action`、`source`、`payload`、`assets`、`status`、`reasons`、`receipt`、`limits` を持つ。

各 asset は 1 始まりの ordinal と role を持つ。収録できたものは `included` とし、Artifact ID、filename、archive_path、extension、MIME、size_bytes、sha256 を保持する。未解決・欠落・Action に未固定の hash・容量超過は `unavailable` と具体的な reason を残す。参照数と ZIP 内ファイル数は別の数として表示する。

`partial` は完成パッケージを意味しない。`verified` も実体検証の結果だけであり、品質、権利、公開承認、外部成功の保証ではない。

ZIP は `manifest.json` と `assets/<安全なbasename>` で構成する。絶対パス、パス区切り、制御文字、Windows 予約名、名前の衝突を拒否する。ZIP 内の素材 archive は展開しない。ZIP_STORED、固定日時 1980-01-01、固定順序を使い、同じ凍結 Action / 素材なら同じ bytes になる。

上限は次の通り。

- 素材実体の合計: 25 MiB。超過素材は読み込まず、partial reason に記録する
- manifest: 1 MiB。参照件数: 100 件。超過は 409
- 圧縮は行わないため圧縮前後の素材サイズは同じ。ZIP の framing は別途加わる
- Web proxy は正確な GET `operations/actions/{id}/package/content` だけ 27 MiB まで受け付ける。既存 Artifact は 26 MiB、その他の Operations 応答は 8 MiB のまま
- 実体が大きなファイルへ差し替わっていても、既存 content reader は登録 size + 1 byte だけ読み、SHA / size 不一致を拒否する

## 手動結果と未対応範囲

manifest の receipt は Action ID / action_version / payload_hash を保持する。復帰先の絶対 URL や basePath は推測しない。Web は選択中 Action 内に既存 Manual attempt と Receipt を表示し、外部管理画面での操作後に人が結果を記録する。

GenerationOutput と ArtifactVersion の正式 binding、25 MiB 超素材の配布経路、媒体別ファイル仕様・品質・権利の受入は未対応。Native Mobile の現行 `operations.execution` product contract は command-center / work の read-only 表示であり、この変更に Native の package 保存画面は含まれない。実ブラウザ・実 provider 投稿・実ユーザーデータによる受入とは分けて検証結果を報告する。
