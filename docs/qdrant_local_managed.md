# ローカル Qdrant Server の起動管理

通常の `main.py` 起動で、明示された Qdrant Server だけを AoiTalk の
ライフサイクルに結び付ける。初回インストール、image pull、index 移行、
OS サービス登録、Docker/WSL 起動、firewall 設定変更は行わない。
`--skip-services` 指定時は、この起動管理も実行されない。

## 有効化の前提と設定

1. 既存のローカル Docker Engine 28 以上が利用できることを確認する。
   Windows は既存 WSL ディストリビューション内の Docker を使う。
   接続先はどちらも `unix:///var/run/docker.sock` 固定で、remote context、
   Docker Desktop の named pipe、rootless の別 socket は対象外。
2. 公式 Qdrant 1.16.3 image の version と RepoDigest を別途検証し、
   同じ daemon に準備する。この機能は `--pull=never` でしか作成しない。
3. Server 用 storage を別途用意し、必要な index 移行・件数・検索検証を終える。
   embedded の `local_path` を Server storage として流用しない。
   storage は Docker ホスト内の既存絶対ディレクトリの実体パスを指定する。
   Windows の場合は Windows の `D:\...` ではなく、指定 WSL 内のパス。
4. PostgreSQL の既存設定更新経路で `rag.qdrant` の次の値をまとめて設定する。
   下記は説明例であり、digest/path をそのまま実行しない。

```yaml
rag:
  qdrant:
    mode: local-managed
    host: 127.0.0.1
    port: 16333
    local_path: null
    image: qdrant/qdrant:v1.16.3@sha256:<実際に検証した64桁digest>
    storage_path: /実際に検証した/server/storage
    wsl_distribution: Ubuntu-22.04  # Windowsのみ。既存の実名に合わせる
```

`config.yaml` を編集するだけでは DB 保存済み設定を切り替えない。
`collection_name` などの既存設定は維持する。新しい API key を生成・配置せず、
このモードでは `api_key` がある設定を拒否する。
host、port、image、storage_path の省略や矛盾は起動エラーとなる。
DB が読めない場合も、default/embedded と推測して起動を続けない。

Windows の WSL managed storage では、指定した WSL 内パスが `/mnt/<drive>/` の
DrvFS/9p bind mount になることがある。この filesystem は Qdrant が open 中の
segment directory を `.deleted` へ置換する rename を拒否するため、起動 helper は
Windows + `wsl_distribution` + `/mnt/<drive>/` storage のときだけ、owned endpointの
既知collectionへ `PATCH /collections/<name>` で
`optimizers_config.max_optimization_threads=0` を適用する。これはpoint/vector/payloadを
変更せず、既存collectionの永続optimizer設定だけを更新する。WSL内の`/home/...`
などext4 storageとnative Linuxのmanaged storageには適用せず、通常の optimizer を維持する。最適化を停止するため、
segment merge・vacuum・index rebuild の自動処理による性能改善は得られない。
既存 collection の件数・検索確認と、運用側で計画した再構築は別途必要になる。
PATCHが失敗した場合は起動を成功扱いにせず、既存collectionのoptimizer errorは通常再起動まで
残り得る。Qdrant 1.16.3ではoptimizer設定変更後も実行中UpdateHandlerの状態が残るため、
変更を反映した再起動後に`optimizer_status`と件数を再確認する。
新しく作成するAoiTalk EmbeddingGemma 2 collectionにも同じtyped optimizer設定を渡す。
`aoitalk_*__eg2_<16桁hex>` 以外のcollection、remote/external endpoint、embedded local
storageにはこの設定を推測適用しない。

## 起動・終了の確認

通常起動は DB/config 準備後、index client 作成前に `/readyz` と Server の
version を確認する。readiness が 30 秒で成立しなければ起動失敗とし、
この試行で作成した所有 container のみを片付ける。
起動後は既存の検索確認手順で collection 件数・代表検索を確認する。

HTTP のホスト公開は `127.0.0.1:<port>` のみ。Docker 転送のため container 内の
HTTP bind は `0.0.0.0:6333` とする。6334/gRPC、6335/cluster はホストへ公開せず、
cluster は無効にする。既存 Docker bridge 内の別 container からの到達性を
分離する新規 network/firewall 設定は行わない。

通常終了と起動途中の Ctrl+C/SystemExit は、実行ごとの所有 token と取得済み
container ID、image、mount、公開設定を再検証し、その container のみ停止する。
通常停止後は disposable container が自動削除され、bind mount の storage は残る。
停止・検証に失敗した場合は成功扱いにしない。

強制 kill、電源断、Docker daemon 障害では cleanup を保証できない。
次の AoiTalk 実行は残存 container を勝手に採用・停止しない。所有者と storage、
実際の稼働状態を operator が確認し、許可された通常停止で解消してから再起動する。
既存の不明な listener/container を kill する復旧処理はない。

## 既存モードとの互換

- `mode` 省略または `external`: endpoint の起動・停止・Docker 操作をしない
- 既存 `local_path` あり、`mode` 省略: 従来の embedded 動作を維持
- `local-managed` と `local_path` の併用: 曖昧なため拒否
- 同一 AoiTalk 実行内の同じ所有 server: 設定・readiness が一致する場合だけ再利用
- 実行中の設定切替: 既存 server を停止せずエラー。正常終了してから再起動する

設定・起動管理は backend 共通のため Web/Mobile の API 契約は変更しない。
index の正式切替と検索 QA は、この起動 helper の単体検証とは別に実施する。

## 参照

- [Qdrant 1.16.3 設定](https://github.com/qdrant/qdrant/blob/v1.16.3/config/config.yaml)
- [Qdrant 1.16.3 Dockerfile](https://github.com/qdrant/qdrant/blob/v1.16.3/Dockerfile)
- [Docker port publishing と 28.0.0 未満の注意](https://docs.docker.com/engine/network/port-publishing/)
- [Docker create](https://docs.docker.com/reference/cli/docker/container/create/)
