# Owned browser driver / Jev loop

`driver.cjs` は既存の Edge controller と、専用 Page に注入した Jev の連続操作ループを実行する。
通常 Edge とメディア準備のコマンドは維持する。Jev／typing の鍵は同梱せず、認証済み Bridge 経由でサーバーへ問い合わせる。

Jev は `jkudish/jev-browser` 0.8.5、revision `d8f1ce0c6d09a8cbe43110ecbe884ecb842083ec` に固定する。
`jev-browser.patch` が上流との差分、`jev-provenance.json` が bundle・patch・依存 notice の checksum と生成環境を記録する。
`dedicated-manifest.json` は同梱コードと controller を固定する。ライセンス原文は `JEV-THIRD-PARTY-NOTICES.txt`。

`jev_run` は既存 run/tab/context を使い、task は16000字、max_steps は1〜60、max_seconds は5〜900。
completion_text/completion_url は任意のヒントとして受理する。モデルの done は外部成果の成立証明ではなく、サーバーの fresh DOM 検証が成立を決める。
`jev_ask` は state/questions/model、`jev_type` は elementDescription/url だけを渡す。
propose/commit gate は同じ Page の既存 material snapshot を添付し、確定 click は既存 controller の atomic guard へ渡す。
承認待ち中は操作 deadline を止める。reply は非同期通路で一回だけ処理し、取消／OFF／切断では専用 Job を停止する。
ログイン handoff の間も同じ run の上流履歴を維持する。正常終了・取消・別runには引き継がない。
各モデル送信直前にも認証状態を確認する。固定tabから開いたpopupは自動採用せず、操作前に停止する。
host入力生成の失敗ではsearchの推測入力・Enterも実行しない。成功した空文字は欄を空にできる。

## 再生成

一時 source と検証出力はリポジトリ内 `.local/` に置き、作業後に削除する。
固定 revision を checkout し、このディレクトリの patch を `git apply` する。
上流の固定 package-lock を使って `npm ci --ignore-scripts`、TypeScript build を実行する。
Playwright は Bridge の既存 1.63.0、Node は24.21.0を使用する。
esbuild 0.28.2 で `dist/library.js --bundle --preserve-symlinks --platform=node --format=cjs` を生成し、
`--external:playwright --external:playwright-core` を指定する。
先頭の shim は `playwright`／`playwright-core` の require を `JEV_PLAYWRIGHT_PACKAGE` へマップする。
esbuild metafile で実際に bundle へ入った依存を列挙し、各原文 notice と上流 LICENSE を保存する。
npm が原文を省いた `@ai-sdk/*` は同一 Vercel monorepo の Apache notice を使い、Apache 2.0 全文も含める。
bundle・patch・notice・provenance・driver の checksum と dedicated manifest を更新する。

Bridge build はこのディレクトリの7ファイルをすべて同梱し、EXE内のファイル集合と正本との byte 一致を検証する。
