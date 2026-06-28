# Release Checklist

APK、EXE、installer、GitHub Release asset など公開用 artifact を作る merge/release では、この checklist と既存の README、docs、scripts を先に確認する。

## Common Gate

- 作業前と完了前に `git status --short --branch` を確認する。
- ユーザーが明示的に `releaseなし` / `APK不要` / `upload不要` と言わない限り、公開対象の build、upload、metadata 更新を省略しない。
- version、tag、asset 名、公開先リポジトリ、`latest.json` などの前提が不足している場合は、merge だけ先に進めない。
- 既存の release / publish / build script がある場合は、その script と引数を優先する。

## APK

- `scripts/check_mobile_release_gate.ps1` がある場合は先に実行し、`RELEASE_REQUIRED=True` なら release 対応を完了条件に含める。
- `app.json`、`mobile/app.json`、`android/`、`modules/*/android/` など mobile 関連差分を確認する。
- release version と versionCode を更新する。
- APK / AAB をビルドし、成果物のパスと version を確認する。
- GitHub Release に asset を upload する運用の場合は、公開先と tag を確認して upload する。
- 自動更新を使う場合は、公開用 `latest.json` を同じ version、APK URL、notes、date に更新する。

## EXE / Desktop

- desktop build script、Tauri / Electron / PyInstaller / installer 設定を確認する。
- EXE / installer をビルドし、成果物のパス、version、起動可否を確認する。
- GitHub Release や配布先へ upload する運用の場合は、tag、asset 名、公開先を確認して upload する。
- 生成物を開発リポジトリに残す必要がない場合は、追跡対象に含めない。

## Report

完了報告では、少なくとも次を明記する。

- mobile changed
- release required
- build
- upload
- metadata
- debug
