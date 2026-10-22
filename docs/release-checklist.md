# Release / Push Checklist

共通の作業規約と完了状態は AGENTS.md を参照する。通常のソースpush、Mobile APK公開、Public公開、Enterprise handoffは別の成果として確認する。

## 通常の変更

変更範囲の検証、対象なら [独立AI WebUI QA](ai_webui_qa.md)、最終差分レビューを行い、対象ブランチへcommit・pushする。push後にリモートのSHAを確認する。GitHub Actions / CIの完了は待たず、成否や利用可否を完了条件にしない。必要なローカル検証・対象QA・以下の配布整合性確認は維持する。

## Mobile APK

公開対象の変更をcommitし、意図したrelease commitが `origin/main` に反映されてから固定snapshot buildを行う。Windows launcherは `origin/main` をfetchし、通常はfirst-parentのversion/versionCodeがともに低いbaseを自動選択する。候補が不正、rollback、または見つからない場合だけ `-ReleaseBase <commit>`（`-Base` alias）または `AOITALK_MOBILE_RELEASE_BASE` を明示する。

launcherがfixed snapshot上でrelease gateを実行するため、通常は手動実行しない。個別に確認する場合だけ、同じbase/target SHAを明示して次を使う。

```powershell
.\scripts\check_mobile_release_gate.ps1 -Base $BaseCommit -Target $TargetCommit
```

`RELEASE_REQUIRED=True` の場合、ユーザーが今回release / APK / upload不要を明示していない限り、以下を実施する。

1. `mobile/app.json` の `expo.version` と `expo.android.versionCode` を更新し、対象commitを `origin/main` へ反映してMobileのターゲット検証を行う。
2. Windowsではリポジトリルートから `cmd /d /c scripts\build_apk.bat` を実行して、公開を伴わないローカル検証済みAPKを生成する。必要なvenv/JDK17/Android SDK、依存取得、Git fetchが利用できない場合は停止する。
3. 実機QAを行い、APK・source/version/versionCode・証明書に一致する別QA承認markerを用意する。
4. QA済みartifactを公開するときだけ `cmd /d /c scripts\build_apk.bat -PublishOnly` を実行し、Releaseと `latest.json` を更新する。通常buildは自動uploadしない。
5. 公開先 `ttttdiva/AoiTalk` のReleaseとmetadataのリモート実体を確認する。

既存の実機QA済みAPKを再buildせず公開する場合だけ、`scripts/build_apk.bat -PublishOnly` または
`scripts/build_apk.sh --publish-only` を使う。APK隣接manifest、別QA承認marker、署名・installer・固定source
revision・Mobile release gateの全照合が必須で、欠落や不一致はfail closedする。対応manifestが無い既存APKには
source保証を後付けせず、canonical build-onlyで再生成してからQAを更新する。

詳細は [Mobile自動更新手順](mobile-auto-update-standard.md) を参照する。Mobileに関係しない作業ではAPK releaseを発生させない。

## Public OSS

入口は `Publish.bat`、詳細は 公開手順。配布元は取得した `origin/main` の固定snapshotで、起動元の未commit差分ではない。公開実装は `scripts/publish_public.ps1` に集約されている。Portable PC Bridge を含む正式配布では、レビュー済み notice bundle を `-PcBridgeDistributionNotices` で絶対パス指定し、manifest が helper / Windows runtime の hash を固定している場合は対応する `-PcBridgeAgentBrowserBundle` / `-PcBridgeWindowsRuntimeRedist` も指定する。

```bat
.\Publish.bat -ValidateOnly -PcBridgeDistributionNotices "D:\release-inputs\reviewed-notices" -PcBridgeAgentBrowserBundle "D:\release-inputs\agent-browser"
```

notice bundle の不足・不整合は正式 builder の配布 gate で fail closed とし、既存候補や source 内の notice を自動承認しない。

## Enterprise handoff

入口は `Enterprise.bat`、通常の正式コマンドは `.\Enterprise.bat`、手順の正本はリポジトリ直下の `README.enterprise.md`。取得した `origin/main` から `scripts/build_enterprise_handoff.ps1` がsanitized-source handoffを生成する。Enterprise更新用に追加のnotice bundleを同梱せず、そのreview/inventory manifestも生成条件にしない。必要な固定helper / Windows runtimeはREADMEの任意入力を絶対パスで指定する。既定のWindows handoff生成はDocker/BuildKit を要求せず、WSL producerや大容量依存取得も実行しない。対象Linux上のproduction buildは別途必須である。

完全なrestricted-network companionが実要件として確認された場合だけ `Enterprise.bat -Offline` を明示する。既に検証済みの入力closureを再利用する場合は `-OfflineInputRoot` を使う。Enterpriseの機能範囲と配布方式の判断根拠は [Enterprise scope and distribution](enterprise_scope_and_distribution.md) を正本とする。

WindowsでのZIP生成・source検証を、production imageや稼働環境の検証成功と扱わない。対象サーバーでのbuild、適用、起動、HTTPS・永続化確認は `README.enterprise.md` と `deploy/enterprise/` に従う。

## 完了報告

commit SHA・push先・リモートSHA確認、ローカルのターゲット検証、対象QAの結果を明記する。CI結果の取得・待機は不要で、参照する場合だけ既知の結果を補足し、未確認をPASSと扱わない。Mobile releaseやPublic / Enterprise配布を行った場合は、その生成物・公開・リモート検証の結果も報告する。実行していない検証をPASSにしない。
