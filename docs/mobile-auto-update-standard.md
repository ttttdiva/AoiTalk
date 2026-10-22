# Mobile Auto Update Standard

AoiTalk mobile の自前配布APKと自動更新に関する標準。

## 基準

- `mobile/app.json` の `expo.version` を現在バージョンの唯一の基準にする。
- 同じversionのAPKを上書きしても、インストール済みアプリは更新を検知しない。
- 配布メタデータは公開用Publicリポジトリ `ttttdiva/AoiTalk` の `latest.json` を参照する。
- APK asset名は `aoitalk-mobile.apk` に統一する。
- APKビルドと公開は `scripts/build_apk.bat` または `scripts/build_apk.sh` を使う。
- Androidの更新導線は日本語表示にする。
- Windows の `scripts/build_apk.bat` は安定した配布対象の `arm64-v8a` を既定 ABI とする。追加 ABI が必要な場合だけ、ビルド前に `REACT_NATIVE_ARCHITECTURES`（例: `arm64-v8a,x86_64`）を明示する。
- `scripts/build_apk.bat -PublishOnly` / `scripts/build_apk.sh --publish-only` は、同じversionの既存APKを再buildせず公開する明示経路である。隣接する immutable manifest と別ファイルのQA承認markerが無い場合は停止する。

### Windows APK launcher

Windowsではリポジトリルートから次を通常入口として実行する。

```bat
cmd /d /c scripts\build_apk.bat
```

通常実行は公開を行わないbuild-onlyである。launcherは設定済みの`origin`から実際の`origin/main`をfetchして固定snapshotを作り、snapshotの`mobile/app.json`のversion/versionCodeを使ってAPKとimmutable manifestを生成する。起動checkoutの未commit変更、別branch、index、untracked fileはbuild sourceに使わず、元のGit状態を保持する。

一時領域・Gradle cache・npm cache・snapshotは`.local\at`、`.local\ag`、`.local\an`、`.local\as`とlockで自動管理し、成功・失敗後に所有範囲だけをcleanupする。通常の`venv\Scripts\python.exe`、検出可能なMicrosoft JDK 17、Android SDKを再利用するため、通常手順で`TEMP`、`GRADLE_USER_HOME`、`npm_config_cache`などを手動設定しない。Gradleは固定の9.1.0-binと公式SHA-256を使う。venv、JDK/SDK、依存取得、GitHubのorigin/main fetchなどが利用できない場合は停止する。

release gateのbaseを指定しない場合、first-parent上でtargetよりversionとversionCodeがともに低い直前の異versioncommitを自動選択する。同じversion/versionCodeの追随commitはスキップする。履歴が不正、rollback、候補なし、または増分が不完全な場合は停止するため、明示的な`-ReleaseBase <commit>`（`-Base <commit>` alias）または`AOITALK_MOBILE_RELEASE_BASE`を指定して再実行する。`-ReleaseBase`指定は同名環境変数より優先する。

## latest.json

公開用Publicリポジトリの `latest.json` は次の形にする。

```json
{
  "mobile": {
    "version": "0.1.0",
    "url": "https://github.com/ttttdiva/AoiTalk/releases/download/v0.1.0/aoitalk-mobile.apk",
    "notes": "更新内容",
    "date": "YYYY-MM-DD"
  }
}
```

## 更新チェック

モバイルアプリ側は以下を満たす。

- 現在versionは `Constants.expoConfig?.version` または同等の値から読む。
- `latest.json` はキャッシュを避けて取得する。
- `mobile.version` が現在より新しい場合だけ更新ありと扱う。
- セマンティックバージョンは `major.minor.patch` の数値比較にする。
- 通信失敗、JSON不備、メタデータ未公開はクラッシュさせず、更新なしとして扱う。
- 更新Alert、ダウンロード開始、失敗、フォールバック表示は日本語にする。

ユーザー表示文言の標準:

- タイトル: `アプリの更新があります`
- キャンセル: `後で`
- 実行: `更新する`
- ダウンロード開始: `ダウンロード開始`
- 開始説明: `通知バーにダウンロードの進捗が表示されます。\n完了後、通知をタップしてインストールしてください。`
- 失敗: `ダウンロードに失敗`
- フォールバック: `ブラウザで開く`

## Android native installer

APKインストーラーは以下を満たす。

- `android.permission.REQUEST_INSTALL_PACKAGES` を含める。
- Android `DownloadManager` を使い、通知バーから完了後にインストールできる形にする。
- 既存の同名APKを削除してからenqueueする。
- ファイル名は `aoitalk-mobile-update.apk` のように固定する。
- MIME type は `application/vnd.android.package-archive` にする。
- 通知は `VISIBILITY_VISIBLE_NOTIFY_COMPLETED` にする。

通知文言の標準:

- title: `AoiTalk 更新`
- description: `APKをダウンロードしています...`

## Release と merge

Mobile APK を公開する場合は次を同時に満たす。

公開対象のversion/versionCode変更と必要なMobile修正をcommitし、意図したrelease commitが`origin/main`に存在する状態にしてからbuildする。通常はbase指定を手動で準備せず、launcherがfixed `origin/main` snapshot上で自動選択する。build成功時のimmutable manifestは解決したbaseと固定targetのMobile release gate結果を保持する。

1. `mobile/app.json` の `expo.version` と `expo.android.versionCode` をリリース版へ更新し、対象commitを`origin/main`へ反映する。
2. `cd mobile && npm run typecheck` を通す。
3. リポジトリルートで `cmd /d /c scripts\build_apk.bat` を実行し、ローカルの検証済みAPKを生成する。この実行では公開しない。
4. 実機でdata-preserveを含む必要なQAを行い、APKとsource/version/versionCode/証明書に一致する別ファイルのQA承認markerを作成する。build処理はmarkerを自動生成しない。
5. QA済みartifactを公開するときだけ `cmd /d /c scripts\build_apk.bat -PublishOnly` を明示実行する。artifact、immutable manifest、QA承認markerの3ファイルをfixed snapshotへ取り込み、canonical verifier、source chain、署名、installer、release gate、QA承認を再照合した後にRelease uploadと`latest.json`更新を行う。
6. `gh release view v<version> --repo ttttdiva/AoiTalk` でReleaseを確認する。
7. `gh api repos/ttttdiva/AoiTalk/contents/latest.json` でメタデータを確認する。
8. 可能ならAndroid環境で、更新検出、Alert文言、ダウンロード開始までを確認する。

### 既存artifactの再利用

build成功時に `aoitalk-mobile.apk.manifest.json` をAPKの隣へ生成する。manifestはAPKのbytes/SHA-256、version/versionCode/package、build時の固定 `origin/main` revision、tracked Mobile inputのworking-tree raw hash、release gate、installer検証、v2署名証明書を保持する。GitのEOL filterでblobが正規化されるため、publish-only検証はblob raw hashを再計算して比較せず、build revisionのancestor関係、compiled Mobile inputのGit差分なし、current fixed inputのclean status、manifestとcurrent working-tree raw hash一致を連鎖検証する。EOLだけが変わった場合もmanifest raw hashと一致しないため停止する。publish-only検証ではbuild source revisionを現在HEADへ上書きせず、現在のfixed `origin/main` がbuild revisionの後続であり、compiled Mobile inputsが同一であることを検証する。`aoitalk-mobile.apk.qa-approved.json` は別のQA承認markerであり、build処理は自動生成しない。markerは `schema=aoitalk-mobile-qa-approval/v1`、`approved=true`、artifact SHA-256、version/versionCode、build source revision、証明書SHA-256を持つ。

publish-onlyはcanonical verifierを再実行し、manifestとAPK・現在の固定source・release gate・署名・installerを再照合したうえで、別QA markerのartifact hash/version/source/certificateが一致した場合だけRelease uploadと`latest.json`更新へ進む。manifest、QA marker、release gate、署名のいずれかが欠落または不一致ならfail closedする。既存artifactへ後付けでsource保証を作らないため、対応manifestが無いAPKは通常のcanonical build-onlyで再生成し、hashが変わった場合は実機のdata-preserve QAをやり直す。`AOITALK_BUILD_ONLY=1` と `-PublishOnly` は併用できない。

同じversionのReleaseが既に存在する場合、通常はversionを上げる。`ALLOW_SAME_VERSION_RELEASE=1` は、同一version上書きをユーザーが明示的に求めた場合だけ使う。

### main統合前のQA branch-build

候補branchを実機検証する場合だけ、通常のbuild entryに次の明示設定を渡す。QAでは自動base選択を使わず、`AOITALK_MOBILE_RELEASE_BASE` に比較元の固定commitを指定する。候補は現在checkoutしている `main` 以外のbranchで、元checkoutがfull-clean、`AOITALK_MOBILE_QA_BRANCH`と`AOITALK_MOBILE_QA_SOURCE_REVISION`が明示され、`origin`上の同名branchと完全SHAが一致している必要がある。remote tracking refだけの一致では通さない。

```powershell
$env:AOITALK_BUILD_ONLY = "1"
$env:AOITALK_MOBILE_QA_BUILD = "1"
$env:AOITALK_MOBILE_QA_BRANCH = (git symbolic-ref --short HEAD)
$env:AOITALK_MOBILE_QA_SOURCE_REVISION = (git rev-parse HEAD)
$env:AOITALK_MOBILE_RELEASE_BASE = "<base-commit>"
.\scripts\build_apk.bat
```

Bashでも同名の環境変数を明示して `scripts/build_apk.sh` を使う。QA modeは`AOITALK_MOBILE_QA_BUILD=1`、`AOITALK_BUILD_ONLY=1`、branch/source revision/baseの明示が必須で、publish-onlyとの混用を拒否する。version/release gate、既存APK verifier、v2署名検証は維持する。

QA artifactのmanifestは `schema=aoitalk-mobile-qa-build/v1`、`build_mode=qa`、`publishable=false`。実際のsource SHA・candidate branch・照合したremote SHAと、取得済みの実際の `origin/main` refを別々に保存する。APK検証記録の `verification.installer.source_revision` も同じsource SHAを必須とする。これはbuild sourceの記録であり、APK内からSHAを抽出したという意味ではない。

QA artifactはrelease承認markerを付けられず、markerが存在する場合はQA build/invalidateを拒否する。外部からmarkerを付けてもpublish検証はQA schemaを拒否する。main統合後の公開用artifactは、QA用環境変数を解除して通常release modeで再buildし、必要な実機QAと別のrelease承認markerを用意する。release modeの `HEAD=origin/main` 要件は変わらない。
