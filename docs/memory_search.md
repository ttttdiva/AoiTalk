# 記憶の明示検索と自動コンテキスト

## 検索経路

- `memory_search` は `ScopedMemoryService.search` で保存済みの事実を検索する。`search_past_chats(mode="semantic")` / `semantic_memory_search` の durable-fact 部分も同じ経路を使う。互換一覧の再取得や、関連度ゼロの候補への一律加点は行わない。
- この事実検索は既存 `BM25Tokenizer` の字句正規化・トークンを使う。全角、短い英単語、日本語、従来の英日エイリアスに対応する。これは新しいベクトル検索ではない。
- 会話本文の意味検索は既存の `MemorySearchService` → `CrossSessionMemoryService` → EmbeddingGemma 2 / Qdrant を使う。索引の本文ではなく、現在の権限で読み直した会話原本を返す。
- `ContextBuilder` の自動挿入は引き続き `retrieve_for_context` を使う。自動挿入用の新しさ・pin・重要度による選択と、任意設定のJevによる候補評価は維持する。明示検索に追加のJev呼び出し、外部送信、第二の索引は加えない。

## 事実検索の境界

1. user / global の所有者、project / task / session の現在の読み取り権限、active状態、有効期限を確認する。
2. 指定されたscopeをtop-Kより先に絞る。不正なscope IDを全体検索へ置き換えない。暗黙のprojectはProject Context設定に従い、明示projectも権限確認を省略しない。
3. 最新200件などの候補打ち切りを使わず、権限内の行を100件単位で読み、クエリに一致する候補だけを最大100件のheapに保持する。
4. 公開前に別のDB sessionで権限・状態・期限・本文を読み直す。検索中に忘却・supersede・期限切れ・権限失効した候補や、編集で一致しなくなった候補を返さない。検索自体は利用日時や記憶状態を書き換えない。

重要度やpinだけでは検索ヒットを作らない。訂正済みの旧記憶はactive条件から除くが、複数のactiveな記憶が矛盾している場合、検索順位は真偽の判定にはならない。証拠とscopeを確認する必要がある。

字句が重ならない言い換えには限界がある。例えば「通勤方法」→「会社には自転車で行く」、「仕事に使う端末」→「業務用パソコンはThinkPad」は今回の事実検索では回収できない。会話の意味検索とは区別する。この限界を解決する際は、既存embedding候補経路の再利用を実データで評価し、別索引や追加課金を先に導入しない。

## 会話の期間

`time_range` は次の意味で解決し、結果の `search_window` にzoneとUTCの半開区間 `[start, end)` を返す。

| 指定 | 開始点 |
| --- | --- |
| `all` | 期間条件なし（既定） |
| `today` | 設定zoneの当日0時 |
| `recent`, `week` | 検索開始時点の7日前 |
| `month` | 検索開始時点の30日前 |
| `this_week` | 設定zoneの週初め、月曜0時 |
| `this_month` | 設定zoneの月初1日0時 |

zoneは既存 `AOITALK_TIMEZONE`、未設定時は `Asia/Tokyo`。Windowsなどsystem tzdbのない環境でも解決できるよう、`tzdata` を直接依存に含める。これは [PythonのZoneInfoのデータソース仕様](https://docs.python.org/3/library/zoneinfo.html#data-sources) に従う。終点は一度だけ解決した検索開始時刻とする。DBのnaive UTC `ConversationMessage.created_at` に変換して比較する。session更新日時や本文編集日時は使わない。SQLで期間とACLを候補選別前に適用し、ベクトル順位付け後の原本取得でも同じ条件を再確認する。

`all` 以外では、会話event日時を持たないdurable factsを除外し、`excluded_sources` に理由を返す。事実の更新日時を「今日の会話」の代わりにはしない。不正な期間やzoneは説明付きエラーとし、暗黙に全期間へ広げない。

## 空の結果と障害

明示検索は `raise_on_unavailable=True` を指定する。`disabled`、`index_unavailable`、`encoder_unavailable`、`backend_failed`、`timeout` を、実際の0件と区別する。Qdrantの失敗もこの経路では空配列に隠さない。

- 全ソース成功で0件: `success=true`、空結果。
- 一部ソース失敗: 成功側の結果と `partial=true` / `source_errors`。
- 全対象ソース失敗: `success=false` とソース別エラー。
- キャンセル: `CancelledError` を伝播し、0件成功には変えない。

公開エラーに検索語、本文、providerの例外文字列や秘密情報を含めない。既存の自動・内部呼び出しは既定のbest-effort動作を維持する。

## 検証とコスト

新しい回帰は `tests/test_memory_recall_regressions.py`、`test_memory_search_relevance.py`、`test_memory_search_windows.py`、`test_memory_search_failures.py`。既存のmemory / scoped memory / cross-session / Qdrant回帰と併せて実行する。

2026-10-09、固定main `73a927d` を基準に、合成SQLiteで「古い正解1件＋新しい無関係なpin」を比較した。warmup後3回の中央値、メモリ計測は別実行。暗号化はidentity fixtureで、実PostgreSQL・実embedding・Qdrant server・Jevは使っていない。

| 行数 | 旧検索: 正解 / ヒット数 / 時間 | 新検索: 正解 / ヒット数 / 時間 | 新検索CPU / Python追加メモリ |
| --- | --- | --- | --- |
| 100 | あり / 10 / 7.2 ms | あり / 1 / 8.1 ms | 7.9 ms / 約0.46 MB |
| 10,000 | なし / 10 / 13.1 ms | あり / 1 / 460.0 ms | 457.6 ms / 約0.72 MB |

新検索は両サイズでSQL 2回。保持メモリはbatchとtop-Kで制限するが、CPU・読み取り量・復号量は権限内の行数に比例する。速さのために古い一致を黙って切り捨てない設計であり、この数値は本番性能の保証ではない。大きい本文や本番暗号化での負荷は別途測定が必要。
