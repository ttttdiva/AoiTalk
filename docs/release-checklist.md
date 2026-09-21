# Release / Push Checklist

共通の作業規約と完了状態は [AGENTS.md](../AGENTS.md) を参照する。通常のソースpush、Mobile APK公開、Public公開、Enterprise handoffは別の成果として確認する。

## 通常の変更

変更範囲の検証、対象なら [独立AI WebUI QA](ai_webui_qa.md)、最終差分レビューを行い、対象ブランチへcommit・pushする。push後にリモートのSHAと、そのコミットのGitHub Actionsを確認する。CIが利用不能の場合もPASSとは扱わない。

## Mobile APK

比較元は作業開始時または前回リリースのコミットSHAを記録し、比較先の変更をcommitしてからgateを実行する。push後の `origin/main` を比較元にすると差分が消えるため使用しない。

```powershell
.\scripts\check_mobile_release_gate.ps1 -Base $BaseCommit -Target HEAD
```

`RELEASE_REQUIRED=True` の場合、ユーザーが今回release / APK / upload不要を明示していない限り、以下を実施する。

1. `mobile/app.json` の `expo.version` と `expo.android.versionCode` を更新し、Mobileのターゲット検証を行う。
2. `scripts/build_apk.bat` または `scripts/build_apk.sh` でAPKを生成し、公開先 `ttttdiva/AoiTalk` のGitHub Releaseへアップロードする。
3. 公開リポジトリの `latest.json` を同じversion・URL・notes・dateへ更新し、Releaseとmetadataのリモート実体を確認する。

詳細は [Mobile自動更新手順](mobile-auto-update-standard.md) を参照する。Mobileに関係しない作業ではAPK releaseを発生させない。

## Public OSS

入口は `Publish.bat`、詳細は [公開手順](public_publish.md)。`-ValidateOnly` で公開対象を検証できる。配布元は取得したremote `main` の固定snapshotで、起動元の未commit差分ではない。公開実装は `scripts/publish_public.ps1` に集約されている。

## Enterprise handoff

入口は `Enterprise.bat`、手順の正本はリポジトリ直下の `README.enterprise.md`。取得したremote `main` から `scripts/build_enterprise_handoff.ps1` がsanitized-source handoffを生成する。

WindowsでのZIP生成・source検証を、production imageや稼働環境の検証成功と扱わない。対象サーバーでのbuild、適用、起動、HTTPS・永続化確認は `README.enterprise.md` と `deploy/enterprise/` に従う。

## 完了報告

commit SHA・push先、ターゲット検証、CI、対象QAの結果を明記する。Mobile releaseやPublic / Enterprise配布を行った場合は、その生成物・公開・リモート検証の結果も報告する。実行していない検証をPASSにしない。
