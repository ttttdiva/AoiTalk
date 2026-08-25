# 料金カタログとコスト計算

「設定 → トークン使用量 / コストダッシュボード」で表示される金額の仕組みと、料金改定時の運用手順。

## 金額の種類

ダッシュボードとAPIは次を区別する。混同すると数字が合わなくなる。

| 種類 | 意味 |
|---|---|
| **定価換算 (list cost)** | 割引や無料枠を考慮しない、公式単価どおりの金額。記録時に確定して保存する。 |
| **推定請求額 (estimated billed cost)** | OpenAIデータ共有無料枠など、AoiTalkが推定できる割引を反映した金額。保存せず、集計のたびに計算する。 |
| **プロバイダ報告額 (provider reported cost)** | OpenRouterなどがレスポンスで返した実際の請求額。取得できた場合はこれが実額。 |
| **削減額 (savings)** | 定価換算 − 推定請求額。 |

推定請求額を保存しないのは、無料枠の割り当てが**他ユーザーを含む同じ請求スコープの履歴順序**に依存するためである。過去のリクエストが1件増えるだけで、後続リクエストが有料になったり無料になったりする。保存すると即座に古くなるので、定価だけを固定し、割引は都度計算する。

## 料金区分 (pricing status)

| 区分 | 表示 | 意味 |
|---|---|---|
| `priced` | 定価計算 | カタログの料金ルールで計算した |
| `provider_reported` | プロバイダ報告額 | プロバイダが返した実額を使った |
| `free_incentive` | 無料枠適用 | OpenAIデータ共有無料枠で無料になった |
| `subscription` | サブスクリプション / クォータ制 | CLI系。API従量課金ではない |
| `local` | API従量課金なし | ローカル推論。電力・GPU・機器費用は未算入 |
| `unknown` | 料金未登録 | カタログに料金が無い。**$0ではなく未確定** |

未知モデルを $0 として扱わないため、集計には `unpriced_request_count` / `pricing_coverage_percent` / 部分集計フラグが付く。ダッシュボードに「料金未登録」が出ていたら、その分だけ合計が過小である。

CLI系 (`codex-cli` / `claude-cli` / `antigravity-cli` / `grok-cli`) とローカル系 (`ollama` / `sglang` / `openai_compatible_local`) はコスト欄を `$0` ではなく `—` と表示する。

## 料金カタログ

料金表は `config/pricing_catalog.json` がバージョン管理された正本。アプリ起動時にDBの `pricing_rules` / `pricing_model_aliases` へ同期される。

各ルールは `provider` / `canonical_model` / `aliases` / `pricing_kind` / `rates` / 閾値ルール / `effective_from` / `effective_to` / `source` / `catalog_version` を持つ。金額はすべて**文字列**で書く（floatの丸め誤差を避けるため。計算は `Decimal` で行う）。

対応している料金形態:

- **`flat_token`** — 入力/キャッシュ読み/キャッシュ書き/出力の単価が一定。
- **`tiered_token`** — 入力トークン数の段によって単価が変わる（Gemini Pro系の200,000境界など）。該当段の単価が**リクエスト全体**に適用される。
- **長文倍率 (`long_context`)** — 入力が閾値を超えると、リクエスト全体の入力単価と出力単価に倍率がかかる（GPT-5.6系の272,000超で入力2倍・出力1.5倍）。超過分だけではない点に注意。
- **`provider_reported`** — レスポンスの実額を使う（OpenRouter）。
- **ツール料金 (`tool_rates`)** — トークンに依存しない実額。Kimi Web Searchの1回0.004 USDなど。

### 料金を改定するとき

1. `config/pricing_catalog.json` を編集する。
   - **既存ルールの `rates` を書き換えない。** 過去のコストが変質する。
   - 新しい `effective_from` を持つ**新しいルールを追加**する。同じモデルの直前のルールは、同期時に自動で `effective_to` が入って閉じられる。
   - トップレベルの `catalog_version` を上げる。
2. アプリを再起動する。または管理者としてダッシュボードの「料金表を更新」を押す。
3. ダッシュボードの「料金カタログ版」が上がったことを確認する。

同期は idempotent なので、同じ `catalog_version` で何度実行しても変化しない。カタログから消したモデルの行も、過去分の計算のために削除されず `is_active = false` になるだけ。

### 自動更新

- **OpenRouter** — 起動時および24時間TTLで公式 Models API (`/api/v1/models`) から取り込む。ただし通常のOpenRouter利用ではレスポンスの `usage.cost` が実額として優先されるため、カタログは補助的な見積もりと新モデル発見に使う。
- **OpenAI / Google / Kimi / DeepSeek** — 公式ページのHTMLスクレイピングは**行わない**。上記の手動編集か、管理者用の取り込みAPI (`POST /api/usage/pricing/import`) でJSONを投入する。`dry_run` で差分を確認してから適用できる。

更新に失敗した場合は last-known-good の料金表が維持され、不完全なレスポンスで既存料金が消えることはない。失敗理由はダッシュボードの「料金表最終更新日時」の欄に出る。

## OpenAI データ共有無料枠

「設定 → コストダッシュボード」の管理者向け設定で有効化する。

- **データ共有インセンティブ** — 有効/無効
- **Usage Tier** — `tier_1_2` または `tier_3_plus`

1日あたりの上限:

| | 1Mグループ | 10Mグループ |
|---|---|---|
| Tier 1〜2 | 250,000 tokens | 2,500,000 tokens |
| Tier 3以上 | 1,000,000 tokens | 10,000,000 tokens |

割り当ての規則:

- 日界は**JSTではなく 00:00 UTC**（日本時間の朝9時に切り替わる）。ダッシュボードの日付範囲はJSTのままなので、JSTの1日が2つのUTC無料枠日にまたがることがある。
- リクエストのトークン数は `入力 + 出力`。キャッシュ済みトークンは入力の内数なので二重に数えない。
- 無料枠は**同じ請求スコープの全ユーザー・全プロジェクトで共有**される。ユーザー別画面でも、先にスコープ全体で割り当ててからユーザーを絞り込む。
- `累積 + そのリクエスト <= 上限` のときだけ無料になる。**上限を少しでも超えるリクエストは、超過分だけでなくリクエスト全体が通常料金**になる。
- ツール呼び出し料金・検索料金などトークン以外の課金は無料化されない。
- CLIプロバイダには適用されない。

これはあくまで**推定**である。実際の請求はOpenAI側の判定によるので、請求書と突き合わせること。

## 既存履歴の再計算 (backfill)

料金カタログを導入する前に記録された行は `total_cost = 0` / `pricing_status = 'unknown'` のまま残る。明示的に実行したときだけ再計算される（自動では書き換えない）。

まず影響範囲を確認する。既定は dry-run なのでDBは変更されない。

```bash
venv\Scripts\python.exe scripts/backfill_token_costs.py
```

期間・プロバイダ・モデルで絞れる。

```bash
venv\Scripts\python.exe scripts/backfill_token_costs.py --start 2026-01-01 --end 2026-07-29 --provider openai
```

内容を確認したうえで実際に書き込む。

```bash
venv\Scripts\python.exe scripts/backfill_token_costs.py --apply
```

管理者は API からも実行できる（`POST /api/usage/backfill`、body に `{"dry_run": true, "start": "...", "provider": "..."}`）。

動作の保証:

- `created_at` 時点で有効な料金ルールを使う。改定後の単価で過去を塗り潰さない。
- `provider_reported_cost` を持つ行（OpenRouterなど）は**上書きしない**。
- 何度実行しても同じ結果になる。
- 料金を確定できなかったモデルは `unknown` のまま残し、モデル名を一覧で報告する。カタログに追加してから再実行すればよい。

既定では `total_cost = 0` の行だけが対象。全行を対象にするなら `--all` を付ける。
