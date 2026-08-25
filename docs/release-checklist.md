# Release / Push Checklist

この文書は通常の `main` push、Mobile release、public publish、Enterprise handoff の入口をまとめます。細かな契約をここへ複製せず、それぞれの script / canonical runbook を正本にします。

## 0. 最初に確認するもの

- `AGENTS.md`
- `CLAUDE.md`
- `git status --short --branch`
- 今回の対象差分

通常のリポジトリ作業は現在の checkout（通常 `main`）へ直接 commit / push します。ユーザーが branch / PR を明示した場合だけその運用へ切り替えます。

## 1. 通常のコード / docs 変更

1. 対象差分だけを変更する。
2. 変更範囲に必要な targeted verification を行う。
3. WebUI のユーザー挙動を変更した場合は [ai_webui_qa.md](ai_webui_qa.md) の独立 AI browser QA を通す。
4. 最終差分を確認する。
5. commit → current branch（通常 `main`）へ push。
6. `scripts/wait_ci.ps1` で push commit の GitHub Actions を確認する。

CI status の定義は `AGENTS.md` / `CLAUDE.md` を正本とします。billing / quota 等で CI 自体が起動不能な場合を PASS と書き換えません。

## 2. Mobile 変更 / release gate

merge/release の前に、存在する `scripts/check_mobile_release_gate.ps1` を先に実行します。

`RELEASE_REQUIRED=True` なら、ユーザーが今回明示的に release / APK / upload 不要と指定していない限り、次を完了条件に含めます。

1. `mobile/app.json` の release version を確認 / 必要なら更新
2. mobile target verification
3. APK build（`scripts/build_apk.bat` または platform 対応 script）
4. public `ttttdiva/AoiTalk` GitHub Release へ `aoitalk-mobile.apk` を upload
5. public repo の `latest.json` を同じ version / URL / notes / date へ更新
6. Release と `latest.json` の remote 実体を確認

詳細は [mobile-auto-update-standard.md](mobile-auto-update-standard.md) を正本とします。

Mobile に差分がない通常 docs/backend/frontend 作業では APK release を発生させません。

## 3. Public publish

開発 repo から public `ttttdiva/AoiTalk` を更新するときだけ [public_publish.md](public_publish.md) と `scripts/publish_public.ps1` を使います。

まず validation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\publish_public.ps1 -ValidateOnly
```

実 publish は source checkout が clean で、公開対象 commit が確定してから行います。public tree の exclusion / secret scan / navigation defaults / `latest.json` preservation は script が正本です。

## 4. Enterprise handoff

Enterprise 配布の人間向け正本は **`README.enterprise.md` だけ**です。生成入口は `scripts/build_enterprise_handoff.ps1` です。

この checklist に model revision、file count、SHA256、Docker image digest を複製しません。それらは handoff builder が生成・検証する manifest と `README.enterprise.md` / deployment scripts の現行値を使います。値を二重管理すると release 時に古い checklist が勝ってしまうためです。

基本入口:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_enterprise_handoff.ps1
```

対象 PC での checksum / manifest / model download / atomic activation / container startup / diagnose / HTTPS smoke は `README.enterprise.md` の順序に従います。

## 5. 完了報告

少なくとも次を明記します。

- commit SHA / push 先
- targeted verification 結果
- CI 結果
- WebUI QA required / result
- mobile changed: yes/no
- release required: yes/no
- APK build/upload/latest.json: required の場合のみ結果
- Enterprise/public publish を実施した場合は生成物 / remote verification

実行していない検証を PASS と報告しません。
