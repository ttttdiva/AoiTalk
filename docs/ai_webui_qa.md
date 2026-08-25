# AI WebUI QA 運用

AoiTalk Web版では、ユーザーが実際に触れば分かる実行時不具合を、人間へ渡す前にAI QAで止める。

## 目的

実装担当の「コード上は正しそう」「typecheck/build/testが通った」という自己判定を、WebUIの完成判定に使わない。

WebUIに関係する変更は、次の3層で検証する。

1. **実装担当**: 実装とターゲット検証を行う。
2. **独立AI WebUI QA**: 実装担当とは別のAI Agentが実ブラウザを操作し、対象ユーザーフローと実行時エラーを確認する。
3. **Playwright regression**: AI QAが発見した再現可能な不具合を、修正後に自動回帰テストへ固定する。

`team-goal` 使用時は、実装担当とは別の Luna Max を独立AI WebUI QAに使う。専用の名前付きcustom agentや追加Skillは必要ない。

小〜中規模変更では、その非実装Luna Maxが最終敵対的レビューも兼任してよい。トークン節約のためであり、diff reviewだけで実ブラウザQAを代替してよいという意味ではない。

## WebUI QAを必須にする変更

次のいずれかに当てはまる場合は、ユーザーが今回の依頼で明示的に `debugなし` と指定しない限り、独立AI WebUI QAを通す。

- `frontend/` のユーザー操作・表示・遷移・入力・保存・権限・ローディング・エラー処理を変更する。
- WebUIで再現したバグを修正する。
- APIやバックエンド変更によってWebUI上の挙動が変わる。
- 「普通に機能を使うだけ」で発生し得る実行時不具合を扱う。

純粋なドキュメント変更、テストだけの変更、ユーザー挙動に影響しない機械的リファクタは対象外にできる。

`debugなし` でもターゲット検証は省略しない。

## 標準フロー

```text
実装担当Luna Max
  ↓
対象範囲の unit / typecheck / lint / 必要な build
  ↓
実装担当とは別のLuna Maxへ実UI QAを委譲
  ↓
AI自身が実ブラウザで対象ユーザーフローを最初から最後まで操作
  ├─ PASS → 必要ならそのまま最終敵対的レビュー
  └─ FAIL → 再現手順と証拠を実装担当へ返す
                 ↓
              修正
                 ↓
             独立AI QA再確認
  ↓
対象Playwright regressionを確認/必要時追加
  ↓
最終差分review
  ↓
commit / push / CI
```

## AI WebUI QAの確認範囲

毎回アプリ全体を総当たりしない。変更されたユーザーフローと、その直近の隣接回帰を確認する。

例: タスクの日付変更なら、単にDate Pickerが開くことだけでは不足。

```text
ログイン
→ Tasksへ移動
→ 対象タスクを開く
→ 日付を変更
→ 保存結果を確認
→ 必要なら一覧へ戻る
→ 再度開く / reload
→ 値が保持されていることを確認
→ Console / Network / backend logを確認
```

QA担当は利用可能な browser / computer-use / Playwright MCP 等を使い、AI自身が画面状態を観察しながら操作する。

UI操作中は最低限、変更scopeに応じて次を確認する。

- 画面上の例外・エラー表示
- Browser Console の error / uncaught exception
- 失敗したNetwork request
- 保存・再読込後の状態
- 今回の変更に関係するローディング / エラー復帰
- アクセス可能ならbackendの新規 ERROR / Traceback

## FAIL時の返却情報

長い設計説明は不要。次だけを返す。

```text
判定: FAIL
再現手順: ...
期待結果: ...
実結果: ...
Console: ...
Network: ...
Backend log: ...
回帰テスト候補: ...
```

原因推測は追加情報として扱い、観測事実と混ぜない。

## Playwrightへの固定

AI QAが見つけた不具合で、安定して再現できるものは修正後に `frontend/e2e/` のPlaywrightテストへ固定する。

目的は、次回以降も同じ既知ケースをLLMに毎回考えさせないこと。

- **未知の不具合探索・人間的な操作判断**: 独立AI WebUI QA
- **既知の回帰確認**: Playwright

既存のE2Eで同じ回帰を十分に保証している場合は重複テストを追加しない。

## 完了判定

WebUI QA必須変更では、実装担当の自己申告だけで `COMPLETE` にしない。

- `PASS`: 独立AI WebUI QAが実ブラウザで対象フローを確認済み。
- `FAIL`: `INCOMPLETE`。修正して再QAする。
- browser tool / 認証 / 起動環境等のため実UI確認を実施できない: `INCOMPLETE_WEBUI_UNVERIFIED`。静的レビューやbuild成功を代替合格にしない。

CIは従来どおり `AGENTS.md` の完了状態に従う。WebUI QAはCIの代替ではなく、その前段の実行時品質ゲートである。
