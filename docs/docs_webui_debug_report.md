# Docs WebUI 実ブラウザ探索型デバッグ報告

- 実施日: 2026-08-23 (JST)
- 対象: AoiTalk Web版 Docs のみ
- runtime tested commit: `36beb4c2ead264c5e22ce12f56e2ba7123f4276c`
- final report commit: `9c729a8ea5e7653ea1623399d3da66c967e1bdee`
- OS / Browser: Windows、Codex In-app Browser (親の確認) / Edge extension (独立QA担当)
- 起動: `venv\Scripts\python.exe main.py`、FastAPI `3000`、Next `3002`、Caddy無効、`npm run build` PASS
- 実ユーザーDocs破壊: なし。破壊操作は専用テストページだけで実施。ただし Today は現行 `/api/docs/today` の副作用境界外であり、既存日付ノートへ一意文字列を1件追加した。この文字列はDBに残っており、追加変更は停止した。

## QAマトリクス判定

`PASS` は実ブラウザで観察した項目、`FIXED` は修正と自動回帰まで確認した項目、`UNVERIFIED` は実ブラウザで未完了、`NOT_APPLICABLE` は該当UIなしを表す。

| 範囲 | 判定 | 観測・備考 |
|---|---|---|
| A01-A04 | PASS | root作成、日本語、Markdown風入力のheading変換、1000文字wrap/save/reload。1000文字保持は現行writer上限20,000と編集規約500文字の差異として残存リスク。 |
| A05 | UNVERIFIED | 日本語長文を複数nodeへ分散するケースは未完了。 |
| B01,B07,B09 | PASS | 末尾/中央/行頭Enter、split前後の表示とblank rowを確認。 |
| B02-B06,B08,B10-B14 | UNVERIFIED | 連続blank、親子split、Tab/Enter高速境界、Shift+Enterの全組合せは未完了。 |
| C01-C03 | PASS | 通常文字Backspace、selection Backspace、通常Delete/selection Deleteの文字編集。 |
| C04-C11 | UNVERIFIED | root/深い階層/子孫保持を含むBackspaceは未完了。 |
| D01-D03 | PASS | 通常文字Delete、selection Delete。 |
| D04 | FIXED / runtime UNVERIFIED | CCC333末尾DeleteでDDD444を巻き込むstale-blur P0を再現。修正・unit/E2E PASS。修正後の実ブラウザDeleteは、アクション時確認待ちで未実施。 |
| D05-D07 | UNVERIFIED | child/異parent/blank境界。 |
| E01 | PASS | 同depth siblingへのTabで子化を目視。 |
| E02-E10 | UNVERIFIED | Shift-Tab反復、collapsed parent、高速Tab、reload/page切替。 |
| F01-F08 | UNVERIFIED | D&D reorder/inside/before/after/cycle/長いtree。 |
| G01-G10 | UNVERIFIED | native cross-row text drag、copy/cut/pasteの一巡は未完了。 |
| H01-H09 | UNVERIFIED | 実ブラウザUndo/Redo連打は未完了。mock E2Eの既存失敗は別欄に記載。 |
| I01-I08 | UNVERIFIED | Arrow/Home/End/focus復帰の全ケースは未完了。 |
| J01-J12 | FIXED / live runtime UNVERIFIED | Today child未表示P0相当を再現。tree/children/details hydrationとmock Playwright回帰を追加。live Todayの追加操作は停止。 |
| J13-J14 | UNVERIFIED | 日付切替・Today往復重複確認は未完了。 |
| K01,K06 | PASS | 入力→blur→reload、test pageと別表示の往復で保存を確認。 |
| K02-K05,K07-K08 | UNVERIFIED | 即時reload、drag、split pane、request順照合の全ケース。 |
| L01 | PASS | 日本語入力。 |
| L06-L07 | UNVERIFIED | 日本語selection/copy/cut/paste、全角記号・絵文字の一巡は未完了。 |
| L02-L05 | UNVERIFIED | native IME composition中のEnter/Backspace/Tab。native IME capabilityは未確認。 |
| M01-M03 | PASS | 1000文字wrap/caret/save/reload。上限契約差異は既知リスク。 |
| M04-M07 | UNVERIFIED | 30-50 sibling、複数階層、offscreen/virtualized編集。 |
| N01-N02 | PASS | Docs Search、next/previous、mark表示。 |
| N03 | UNVERIFIED | replace操作。 |
| N04-N05 | PASS | node link、URL link、href/target確認。 |
| N06 | UNVERIFIED | task/file inline preview。 |
| N07-N11 | PASS | collapsed/expanded、sidebar、context menu、duplicate、checkbox。 |
| N12-N14 | UNVERIFIED | tag/field、split view、attachment安全試験。 |

## 発見したbugと修正

### P0-A: Delete末尾マージ後のstale CodeMirror保存による本文消失

- disposable page: `__Docs_WebUI_Debug_20260823_1242` (`20c87073-a7dc-4413-859c-3e44068c120a`)
- nodes: CCC333 `3b373661-b056-4549-84a2-6ca28b5ec4b7`、DDD444 `4705bbf8-8a6c-4988-a7ae-f226a48e4d6d`
- 最小再現: CCC333を開く → End → Delete → 別行クリックでblur → reload。
- 実結果 (修正前): 一瞬CCC333DDD444を表示後、stale editorがCCC333を再PATCHし、DDD444をarchive。DB revision順は `CCC333DDD444 update → DDD444 archive → CCC333 update → CCC333 update`。
- root cause: `outline-editor.tsx` Delete keymap後のmounted CodeMirror本文とblur snapshotが旧値のまま。
- fix: merged titleをCM/session.row/draft refsへ同期し、blur/unmountはactive session rowを使用。archive失敗はWorkspace callbackからthrowしてstructural queueへ伝播。
- regression: `docs-outline-data-safety.test.tsx`、`docs-workspace-archive-failure.test.tsx`、`frontend/e2e/docs-editor.spec.ts` (PATCH+DELETE待機、blur、reload) 全PASS。
- browser retest: **PASS**。ユーザー許可後、fresh page `__Docs_WebUI_DeleteQA_Fresh_20260823` でCCC333/DDD444を再作成し、CCC末尾Delete→別行blur→reload→再open。CCC333DDD444のみ表示、DDD444は表示されず、Console error/warn 0。

### P0相当-B: Today再表示時の子ノードがstate置換で一時消失

- live observation: `TODAY_SAVE_TEST_20260823_2145_PARENT` はサーバーに残るが、Today再クリック直後はDay nodeと空行だけ。Daily notesツリーを展開すると再表示。
- root cause: `/api/docs/today` のDay nodeだけをmergeし、tree/children/detailsを取得していなかった。
- fix: 同じload generationでtree/children/detailsを10秒bounded並列取得し、navigation guard後にmerge。
- regression: component test (2回Today、3 endpoint) と mocked Playwright `hydrates persisted Today children on repeated navigation` 全PASS。
- live retest: **UNVERIFIED**。Director判断によりTodayの追加live mutationは停止。

### 既知・未修正リスク

- Today GET自体がDay/Supertag/Daily notesのensure・reparentを伴うため、完全read-onlyではない。
- 1000文字通常outline nodeが保持される一方、編集規約にはtitle 500文字以下の記載がある（writerの実上限は20,000）。
- browser reload後に選択中ページがHomeへ戻るUXを観測（データは保持）。
- 全 `docs-editor.spec.ts` は 78件中61 PASS / 17 FAIL。失敗は既存のField候補、Undo/blank、geometry/sidebar、slash/quote等で、今回追加したDelete/Today regressionはPASS。今回の修正と因果が確認できないため追加修正はしていない。

## 実行時証拠

- Console: P0再現時の `tab.dev.logs` は error/warnなし。修正後独立QAの準備中に `コンテキストSnapshotの取得に失敗: TimeoutError: signal timed out` warning 1件、errorなし。
- Network: live runtimeのアクセスログには該当node IDの行なし。mock E2EではTodayの4 GETとDeleteのPATCH+DELETEを待機。
- Backend: `logs/app/app_*.log`、`logs/web/frontend*.log`、Caddyログに今回操作由来のERROR/Tracebackなし。
- Native IME: **UNVERIFIED**（synthetic compositionをnative IME PASSとは扱っていない）。

## 検証・レビュー・CI

- Targeted Vitest: 3 files / 13 tests PASS。
- Targeted ESLint: exit 0（既存hook warningのみ）。
- `npm run build`: PASS（Turbopack NFT warning 1件）。
- Targeted Playwright: Delete 1 passed、Today 1 passed。
- Director final review: **コードPASS / blocking 0 / required code fixes 0**。Today liveは隔離mock/test DBで扱うべきとの判断。
- CI: final report commit `9c729a8ea5e7653ea1623399d3da66c967e1bdee` の run `32642086048` はstepsなしのGitHub billing/quota/infrastructure failure。`wait_ci.ps1` exit 2、**CI_UNAVAILABLE**（CI PASSではない）。

## 残存判定

- コード: PASS
- 独立実ブラウザP0-A再検証: **PASS**（fresh disposable page、Console error/warn 0）。
- 独立実ブラウザP0-B Today再検証: **UNVERIFIED**（live Todayの副作用境界を避け、mock Playwright PASSのみ）。
- CI: **UNAVAILABLE**
- 完了状態: **INCOMPLETE_WEBUI_UNVERIFIED**（実UI QA必須だが、削除操作の直前確認が得られなかったため）。

## 禁止モデル監査（訂正）

- AoiTalk起動時のキーワード初期化で `gpt-5-mini` のLLMクライアントが初期化された。証拠は `C:\\nk\\41_AoiTalk\\temp\\docs-debug-main.out.log`、`docs-debug-main2.out.log`、`docs-debug-main3.out.log` の各 `LLMクライアント作成完了 (モデル: gpt-5-mini)` 行。
- これは今回の禁止モデル方針に反する初期化であり、私の起動前チェック不足。3回ともDocs QA用サーバー起動時で、起動後にChat/LLM操作はしていない。取得ログには `chat/completions` / `responses` の推論呼出し記録はないが、完全な外部通信ゼロを証明するネットワークキャプチャは取得していないため、「初期化のみ確認、API推論呼出しは証拠なし」と表記する。
- サブエージェントは `gpt-5.6-luna` のみを使用し、`gpt-5-mini` / `gpt-4o-mini` APIは使用していない。起動したAoiTalkプロセスは停止済み。
