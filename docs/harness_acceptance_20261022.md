# ハーネス受入記録（2026-10-22 JST）

対象: 41 `main` `71a3c1a9b` 時点の正準 `/api/agent-tasks` ハーネス（`required_outputs` / `edit_targets`）の運用受入。
この文書は**実測した事実と既知の限界**だけを記録する。ハーネス全体の完全性や Hermes 全体のレビュー結果を主張するものではなく、割合などの数値評価は書かない。
仕様の正本は [local_agent_tasks.md](local_agent_tasks.md)（`required_outputs` / `edit_targets` 各節）。

時刻は UTC と JST（UTC+9）を併記する。ライブ実行は 2026-10-21 夜（UTC）に行われ、JST では 2026-10-22 の未明にあたる。

## 結論

実運用で使える経路は次の3つで、それぞれ1件以上の成功したライブ実行を確認した。

| 経路 | 確認した成功実績 | 根拠 |
| --- | --- | --- |
| Gateway Claude Code（Sonnet 5.5, medium） | 実装・統合の作業が成功 | Gateway job `71a1a646-238a-49dc-8eaa-9acbdd3d715d` が `4422424ca` を統合した。絞ったテスト（edit_scope 56、runtime guidance 23、required_outputs 52、invocation 123 passed と xfail 1）と `npm run build:production`（fingerprint 保存まで）が通り、main へ push してリモート SHA を確認した |
| OpenRouter DeepSeek V4.1 Flash | 正準モデルタスクが完了 | タスク `cb22c36f48d75c60924e1c433f1da476`。回帰テストを追加し、親が実差分を確認して採用 |
| Strata | 正準モデルタスクが完了 | タスク `ec192f7d9e1d5062b1d93f8f97d231f0`。成果物の作成と読み戻しを確認。ただし文字数の上限は満たさなかった（下記） |

Opus 5.5（high）は設計相談のみで、実装・統合は行っていない。

これとは別に、Strata の低価値フォローアップ（`035af9c9c07b5e51b86f5fb6ac1bc271`）は、親が取り消した作業で、3経路のどれにも数えない。完了ではなく `cancelled` で終わっている。

結論は「3経路それぞれで運用上の受入が確認できた」ことだけで、「ハーネスが完全」「偽の完了は起きない」とは言えない。ソース全体の体系的レビューはこのリポジトリの外で継続中。

## 検証した環境

- **配備ランタイム**: `4422424ca`。起動は正常（boot `d8e0f764`）。
- **80番アプリ（main `493b2c5`）**: 認証済みの helper が新しい `required_outputs` / `edit_targets` に対応していることを確認した。稼働中の Gateway の MCP スキーマは、明示的な再読込をしていない（下記「既知の限界」）。

## ライブ実行の記録

### Gateway Claude Code

- Gateway job `71a1a646-238a-49dc-8eaa-9acbdd3d715d`（Sonnet 5.5, medium）。`edit_targets` による編集 scope の固定・永続化を実装し、`4422424ca` として統合した。
- 検証: 絞ったテストと本番ビルドが通った（上表）。フルスイートは実行していない。
- 設計は Opus 5.5（high）への相談のみで、実装は Claude Code が行った。

### OpenRouter DeepSeek V4.1 Flash

- タスク `cb22c36f48d75c60924e1c433f1da476` / run `18d55b78`。2026-10-21 19:50:03 UTC（2026-10-22 04:50:03 JST）に正常完了。
- 結果: `tests/test_read_file_window_metadata.py` に新規の回帰ケース4件（3テスト）を追加し、`FINDINGS.md` を保存した。
- 検証: 親による独立実行で 35 passed（3.88 秒）、最終 main でも 35 passed。採用コミットは `71a3c1a9b`（リモートの SHA を親が確認済み）。
- 注記: DeepSeek が `FINDINGS.md` に書いた「他のファイルは読んでいない」は、実装ファイルを読んでいるため過大だった。この主張は採用していない。`FINDINGS.md` はリポジトリに載せていない。

### Strata

- タスク `ec192f7d9e1d5062b1d93f8f97d231f0` / run `90eb2562`。完了。
- 結果: `workspaces/strata-qa-20261021/ACCEPTANCE.md` に、要求された6項目の内容がすべて入っていることを、作成と読み戻しで確認した。
- 限界: 成果物は 679 文字で、要望の 600 文字以内を満たしていない。満たさなかったのは文字数の上限だけで、項目数の不備は確認されていない。`required_outputs` は文字数や内容の品質を検証しないため、この超過はハーネスでは検出されず、人の確認で分かった。

### 取り消した低価値フォローアップ（経路ではない）

- タスク `035af9c9c07b5e51b86f5fb6ac1bc271`。Strata の成果物を 626 文字まで短縮したが、7 分を超えたため、親が取り消した。
- 終端が `cancelled` であることを、2026-10-21 19:58 UTC（2026-10-22 04:58 JST）に確認した。成果物は保存し、完了としては扱っていない。

## 完了の確認方法

完了は、次の複数の確認を合わせて判断する。いずれか1つだけで品質を保証するものではない。

- タスクの終端状態（`completed` / `failed` / `cancelled` など）。
- 成功したツール receipt。
- `required_outputs` の検証（変更の有無、書込みの来歴）。
- 内容の独立した確認（親または人が成果物を読む）。

`failure.missing_required_outputs` は、`task_required_output_missing` で失敗した場合など、該当する失敗にだけ現れる項目で、成功を示す汎用の項目ではない。

## 失敗の経緯（簡潔に）

1. 初期のハーネスでは、必須成果物が未作成のまま最終回答だけで COMPLETED になり得た。`required_outputs` を完了契約とし、補正ターンを追加して是正した。
2. 受入のライブ実行で、goal の「…節だけを確認し」を、文章パーサーが「編集してよいのはこのファイルだけ」と誤読した。編集可能ファイルが読み取り専用のソースに束縛され、正しい保存先 `ACCEPTANCE.md` への作成が拒否された。実行時のガイダンスが scratch の probe を勧めていたため、さらに別の拒否も起きた。
3. `edit_targets` を追加した。admission で編集 scope を1回だけ確定・永続化し、実行時は固定値だけで判定する。`required_outputs` が scope の外なら Work/Run の作成前に 422 にする。対象に限定された run では scratch の probe を案内しない。過去の拒否記録は消さず、再試行もしない。

## 使い方の例

`POST /api/agent-tasks`（`Idempotency-Key` ヘッダーが必須）。`workspace` は承認済みの絶対パス。認証は既存のセッション/トークンをそのまま使う。以下は形の例で、認証情報や固有の値は含まない。

```json
{
  "agent_id": "<AgentのUUID>",
  "goal": "更新済み local_agent_tasks.md を確認し、短い日本語チェックリスト ACCEPTANCE.md を新規保存する。最後に読み戻して保存先を示す。",
  "workspace": "<承認済みworkspaceの絶対パス>",
  "mode": "mutate",
  "edit_targets": ["ACCEPTANCE.md"],
  "required_outputs": ["ACCEPTANCE.md"]
}
```

- `agent_id`: 必須。正準 API の直接呼び出しでも、Gateway の facade 経由の呼び出しでも、**常に明示する**（Agent の UUID）。
- `provider`: Gateway の facade で正準経路を選ぶ場合に限り、`provider` だけを省略する。`agent_id` を含む他の項目は、上のとおり渡す。
- `mode`: 書き込みを伴うなら `mutate`。成果物や編集対象の指定は `mutate` が前提。
- `edit_targets`: 書き換えてよいファイルを、workspace 相対の**完全一致**で列挙する（最大20件）。省略した場合は従来どおり文章から推論する。
- `required_outputs`: 完了に必要な成果物。`edit_targets` の範囲内でなければならず、書込み権限は与えない。
- 結果の確認は、上の「完了の確認方法」に従う。

## 既知の限界

- **ファイル対象の制限はコマンドの OS sandbox ではない。** 許可された `allow_commands` のコマンドが何を書くかは検査しない。コマンドで書込みを禁じたい場合は `allow_commands` を与えない。
- **`required_outputs` の保証の範囲。** 内容の digest が実行前と違うことと、書込みの来歴を確認するだけで、内容の品質（文字数、正確さ、項目の内容）は証明しない。実際に Strata の文字数超過は検出されなかった。成果物の確認は人が行う。
- **大文字小文字の比較。** パスの比較は全プラットフォームで大文字小文字を区別しない（実行時の比較と同じ実装）。大文字小文字だけが異なる別々のファイル名は、大文字小文字を区別するファイルシステム（Linux など）でも同じ対象として扱われうる。したがって、この Windows 向けに受け入れた実装は、Linux での大文字小文字を厳密に区別した隔離にはなっておらず、追加の安全性を主張するものではない。
- **旧タスクの scope。** `edit_scope` を持たない旧タスクは、保存済みの元の依頼から、読み込みのたびに決定的に再導出する（書き戻しはしない）。
- **`edit_targets=[]`。** 書込み対象なしを意味し、無制限ではない。コマンドを伴わない場合は admission で 422。コマンド経由のタスクは、変更系の receipt が得られなければ mutate の完了条件を満たさず失敗する。
- **非ストリームのプロバイダ呼び出し。** 1 ラウンド 138 秒を観測した。SDK の読み取りタイムアウトは 600 秒。
- **テスト範囲とクラウド CI。** ローカルの Windows で、絞ったテストと本番ビルドは通っている。フルスイートは実行していない（既知の xfail が 1 件）。広域確認は途中で中断している。クラウド CI による受入、およびフルスイートでの受入は**利用できない**。`ba84d9e94` の GitHub Actions [run 39667036146](https://github.com/ttttdiva/41_AoiTalk/actions/runs/39667036146) では、7 つのチェック job がすべて、ステップが 1 つも実行される前に 2〜3 秒で失敗した。backend job（check `119454692704`）の annotation は「The job was not started because recent account payments have failed or your spending limit needs to be increased. Please check the Billing & plans section in your settings」と述べている。これはアカウントの課金または利用上限に起因するインフラ側の阻害要因で、このコードのテスト失敗を示す証拠ではない。課金・上限・workflow・権限の変更と CI の再実行は行っていない。
- **稼働中 Gateway の MCP スキーマ。** 現在接続されている MCP カタログを確認したところ、`required_outputs` / `edit_targets` は露出していなかった。通常の認証済みで新しく起動した helper の呼び出しは、この2項目に対応している。Gateway の再起動と、呼び出し側のメタデータの更新は未実施で、保留中（このドキュメントの作業では実施しない）。
