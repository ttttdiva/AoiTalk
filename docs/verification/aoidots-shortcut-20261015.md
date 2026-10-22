# AoiDots Alt+Shift+D 検証記録（2026-10-15）

## 判定

**INCOMPLETE_WEBUI_UNVERIFIED**。ターゲット検証と独立静的レビューは実施済み。実ブラウザでの対象フロー確認は起動制約により未実施。本番環境への適用・本番動作確認は行っていない。commit / push の結果は本記録の対象外。

## 対象変更

- `frontend/src/components/aoidots/use-aoidots-shortcut.ts`
- `frontend/src/components/aoidots/aoidots-panel.tsx`
- `frontend/src/components/aoidots/__tests__/use-aoidots-shortcut.test.tsx`

既存 shell の `toggleAoiDots` と開閉状態を利用し、Alt+Shift+D で開閉する。キーボード操作で開いた場合だけ、準備完了後の入力欄に一度フォーカスする。パネルに参照を接続し、起動ボタンに `aria-keyshortcuts="Alt+Shift+D"` を付与する。

## 実施済み検証

- 実装担当による対象 Vitest：15件 PASS。保存された結果も対象テストファイルの `failed: false` を示す。
- 実装担当による hook の TypeScript 型検査、対象 ESLint：PASS。アプリ全体の build / 型検査を意味しない。
- 実装担当とは別のQA担当が、保存された差分・hook・パネル接続・テスト内容を静的レビュー。新しい確定不具合は検出していない。

15件のテストは jsdom 上の React ハーネスであり、実ブラウザや完全なアプリ起動の検証ではない。対象は次の通り。

- 開く、入力欄へのフォーカス、入力欄から閉じる
- マウス操作との状態共有、通常の開閉・再描画でのフォーカス非強制
- キーリピート、IME変換中、keyCode 229、Ctrl / Meta併用、必要修飾キー欠落、別キーの除外（8ケース）
- 処理済みイベントの除外、印字文字が異なる配列での物理 KeyD
- 入力欄の準備待ち後の一度限りのフォーカス
- 待機中に別の場所へ移ったフォーカスの尊重
- 準備前に閉じた場合の遅延フォーカス取消

## 実ブラウザQAのブロッカー

dot VM の `/usr/bin/chromium` と Playwright を使用し、実 hook を読み込む隔離ハーネスで起動を試みた。通常実行と承認された昇格再試行の両方で、Chromium はページ操作前に次のエラーで終了した。

```text
FATAL:chrome/browser/process_singleton_posix.cc:297
Check failed: . socket() failed: Operation not permitted (1)
```

アクセス制約を迂回する操作は行っていない。ユーザーのブラウザ・Windows・Gatewayには接続していない。QA用に起動したViteは停止し、その生成キャッシュは削除済み。

## 未検証・再開時の確認

許可されたブラウザ実行環境で、実際のパネル開閉、入力欄フォーカス、長押し時の再トグル抑制、閉じる操作との整合性、準備待ち中のフォーカス移動を確認する。関連する Console / Network エラーも確認する。本記録を実UI PASSや本番適用済みの根拠として扱わない。
