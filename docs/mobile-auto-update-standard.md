# Mobile Auto Update Standard

AoiTalk mobile の自前配布APKと自動更新に関する標準。

## 基準

- `mobile/app.json` の `expo.version` を現在バージョンの唯一の基準にする。
- 同じversionのAPKを上書きしても、インストール済みアプリは更新を検知しない。
- 配布メタデータは公開用Publicリポジトリ `ttttdiva/AoiTalk` の `latest.json` を参照する。
- APK asset名は `aoitalk-mobile.apk` に統一する。
- APKビルドと公開は `scripts/build_apk.bat` または `scripts/build_apk.sh` を使う。
- Androidの更新導線は日本語表示にする。

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

1. `mobile/app.json` の `expo.version` をリリース版へ更新する。
2. `cd mobile && npm run typecheck` を通す。
3. リポジトリルートで `cmd /c scripts\build_apk.bat` を実行してAPKをビルドする。
4. 公開用Publicリポジトリ `ttttdiva/AoiTalk` のGitHub ReleaseにAPKをアップロードする。
5. 公開用Publicリポジトリの `latest.json` を同じversion、APK URL、notes、dateに更新する。
6. `gh release view v<version> --repo ttttdiva/AoiTalk` でReleaseを確認する。
7. `gh api repos/ttttdiva/AoiTalk/contents/latest.json` でメタデータを確認する。
8. 可能ならAndroid環境で、更新検出、Alert文言、ダウンロード開始までを確認する。

同じversionのReleaseが既に存在する場合、通常はversionを上げる。`ALLOW_SAME_VERSION_RELEASE=1` は、同一version上書きをユーザーが明示的に求めた場合だけ使う。
