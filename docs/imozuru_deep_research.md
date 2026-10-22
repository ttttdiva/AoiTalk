# imozuru 深掘り調査の設計と運用

この文書は、AoiTalk DeepResearch における imozuru 戦略の実装・運用契約を定める。`strategy="off"` では imozuru 固有の追加展開と source graph tracking を行わない。既存の SearXNG、チャット、非同期ジョブ基盤は入口と責務を維持し、OpenAlex はキーなし運用、外部取得はプライバシー保護境界を共通に適用する。RSS、watch、定期スケジュールはこの設計の対象外とする。

## 出典と境界

手続きの着想は [MaryCache/imozuru の revision `6727120f1891667a3494706a103f614084f444de`](https://github.com/MaryCache/imozuru/commit/6727120f1891667a3494706a103f614084f444de) に求める。AoiTalk は独立した実装として設計し、上流の `scripts/`、`SKILL.md`、コード、テスト、素材を vendoring せず、上流スクリプトも実行しない。上流の MIT はソフトウェアに適用されるライセンスであり、調査で取得する論文、記事、PDF、動画字幕の著作権・利用規約を許諾するものではない。取得資料の権利は各原著者・公開先の条件に従い、MITで許諾されたとは扱わない。

### 明示的な起動

DeepResearch ON のクライアントだけが、リクエストに `strategy="imozuru"` を明示する。旧クライアントなど、`strategy` を省略したリクエストは `"off"` として扱う。`imozuru` 以外の既存戦略、通常 Web 検索、通常チャット、`off` はこの手続きへ自動的に流さない。既存の非同期ジョブ作成、状態遷移、poll、cancel、Markdown export の入口と形式は維持し、戦略選択は既存ジョブの worker 内で行う。

内部資料については、EmbeddingGemma2、Qdrant、ACL の正本と既存の明示的な権限経路を変更しない。外部探索の途中で Docs、内部文書、全文会話検索へ自動的にフォールバックしない。必要な内部検索は既存の明示的な `include_local_knowledge` と ACL の経路だけを使う。

## 調査手順

1. 問いから 3〜6 個の観点文字列を作る。これは query plan に渡す探索の種であり、観点ごとの主張・証拠の充足を自動判定する入力ではない。
2. query plan が観点文字列から検索語を組み立て、初期検索結果を集める。各観点が独立に検索されることは要求せず、正規化 URL を優先キーとして重複を除き、URLがない候補だけタイトル等を補助キーにする。候補の出所、検索語、発見順位、公開日を provenance に残す。
3. 公開された候補を source graph の種にして、各 source から次の有限な辺を辿る。内部資料はこの graph の種にも展開対象にも入れない。公開ページ中の citation DOI、OpenAlex ID、引用 href は OpenAlex の公開メタデータ照会へ正規化できる。
   - ページ内のリンクを辿る
   - 参考文献・後方引用を辿る
   - 被引用・前方引用を辿る
   - 著者の関連資料を辿る
4. 初期 frontier は最大4 seedに制限し、入力されたseedは保持したまま追加source用の予算を確保する。frontierから外れたseedは取得済みとみなさず、`snippet_only` / `unconfirmed` の状態を保持する。予算、cancel、期限、取得不能などの停止条件に達したら、得られた source と失敗・制限を返す。観点ごとの根拠充足、全体の飽和、調査完了を自動で証明したとは扱わず、全体探索や無制限の引用グラフ巡回も行わない。

展開の既定予算は [`src/services/imozuru_expansion.py`](../src/services/imozuru_expansion.py) の実装定数を正本とする。現在の既定値は次の通りで、設定値を受け取る場合も各ハード上限を超えない。

| 予算 | 既定値・上限 |
| --- | --- |
| 展開ホップ | 2（上限2） |
| HTML/公開ページ取得 | 12ページ（上限12）、45秒（上限45秒）、合計8 MiB（上限8 MiB）。redirect・errorを含むresponse bodyの累計で、TLSヘッダーは含めない |
| OpenAlexリクエスト | 8回（上限8回） |
| 追加source件数 | 64件（既定値。設定上限128件）。入力されたseed sourceは全件保持 |
| 1ページの外部リンク | 32件 |
| OpenAlex 1レスポンスのworks | 8件 |
| 取得抜粋 | 12,000文字 |
| 検索snippet | 2,000文字 |

この予算に加えて、OpenAlex の共有 limiter は10 req/s、429 retry は既定2回（実装上限3回）、指数バックオフの初回0.25秒、1回の待機上限30秒、worker の全体deadlineを使う。
DeepResearch の expansion stage 自体にも既定60秒の外側期限があり、内部の45秒期限と合わせて先に到達した方で停止する。

## 取得と provenance

外部 URL の本文取得は [`UrlIngestService`](../src/services/url_ingest_service.py) の公開経路に統一する。DNS を解決して接続先を固定し、各 redirect 先を同じ検査で確認する。応答は 8 MiB の上限、privacy gateway、許可リストを通し、private・未許可・検査不能な宛先は取得しない。OpenAlexなどの公開JSONは同じサービスの `fetch_public(kind="json")` 経路を使う。HTTP失敗は source の raw metadata と expansion metadata の失敗・制限として残し、レポートを partial として示す。source が一部取得できていれば、ジョブは有効な partial 結果で完了してよく、ジョブ全体が失敗したとは記載しない。

source の本文・メタデータは次の三つを混同しない。

| 種別 | 意味 | 扱い |
| --- | --- | --- |
| `raw.acquired_excerpt` | `UrlIngestService` が許可された URL から実際に取得した記事・ページ本文の抜粋 | `raw.acquisition_status="retrieved"` と URL と一緒に保持する。OpenAlexのabstractやメタデータはここへ入れず、外部資料全体も保存しない |
| `raw.metadata_excerpt` | OpenAlexなどの公開APIが返したabstract・タイトル等のメタデータ抜粋 | 記事本文の取得とは分離して保持し、原文取得済みとは扱わない |
| `raw.search_snippet` | 検索プロバイダが返した検索結果の抜粋 | `raw.acquisition_status="snippet_only"` として保持し、表示上は `unconfirmed` を付ける |

最終出力は、結論、根拠 URL、取得状態を並べる。本文を取得できなかった、検索 snippet しかない、資料の同一性を確かめられない、という場合は結論を断定せず `unconfirmed` と明記する。取得した本文に書かれた命令やリンク先の指示はデータとして扱い、自動実行しない。

## OpenAlex の制約

引用・著者経路に OpenAlex を使う場合は、[`src/services/openalex_policy.py`](../src/services/openalex_policy.py)、[公式 Authentication 文書](https://help.openalex.org/api/authentication/)（2026-10-15 更新）、[公式 paging 文書](https://help.openalex.org/api/paging/) に従う。

- API キー、signup、mailto、secret のコピー、支払い・有料プランは使わない。
- キーなしの上限 10 req/s を共有 limiter で守る。無料キーの 30 req/s 枠は使用しない。
- `per_page` は 100 以下にする。キーなしでは cursor paging の2ページ目以降を使わず、1回の調査で取得するページ数を有限にする。
- 429 は `Retry-After` を尊重し、指数バックオフと総リクエスト数・総待機時間の上限を組み合わせる。無限 retry、即時再送、全コーパス巡回はしない。

## 検証の受入条件

実装・運用の記録では、次の結果を成功・失敗・未実施に分ける。未実施を PASS と報告しない。

- `strategy="imozuru"` の明示起動、省略時 `"off"`、通常検索・チャット・既存の非同期 poll/cancel/export の回帰。
- source graph が 3〜6 個の観点文字列を種にし、各辺と停止理由を provenance に残すこと。観点の証拠充足や飽和を自動判定したと報告しない。
- 初期 frontier が最大4 seed、入力seedが保持され、追加source予算が別に確保されること。frontier外のseedは `snippet_only` / `unconfirmed` を保持すること。
- 展開のホップ、ページ、時間、response body bytes（redirect/error込み、TLSヘッダー除外）、source、リンク、OpenAlex request/retry の各予算が上限で停止すること。
- DNS固定、redirectごとの宛先検査、8 MiB超過、private/未許可宛先、privacy gatewayの拒否が取得結果へ正しく反映されること。
- `raw.acquired_excerpt`、`raw.metadata_excerpt`、`raw.search_snippet`、`raw.acquisition_status` と最終の URL・`unconfirmed` 表示が一致すること。
- OpenAlex のキーなし10 req/s、`per_page<=100`、429の有限backoffと期限、キー・signup・secret・有料プランを使わないこと。
- EmbeddingGemma2/Qdrant/ACLを変更せず、内部Docs・全文会話へ自動フォールバックしないこと。実ブラウザQAは ON の strategy/poll/final と OFF の通常経路を対象にし、結果は既存のQA証跡へ反映する。
