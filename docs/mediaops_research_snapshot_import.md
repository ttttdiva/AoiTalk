# MediaOps 研究 snapshot の取込と固定参照

`scripts/mediaops_bootstrap.py` は保存済み研究 JSON/JSONL をデータとして読み、通常の認証済み API で Docs 原文、ResearchRun、候補を保存する。生成・投稿や研究 builder の実行は行わない。設定済み owner、project scope と既存 Team/model binding の検証は従来どおりで、この取込が権限を追加することはない。

## ファイルを配置しない入力と offline plan

`research_snapshot_from_sources({basename: utf8_bytes_or_text})` はメモリ内の source-map を解析する。`make_plan_from_sources(bundle_sources, research_sources, settings)` は既存 bundle 取込と同じ計画を作り、従来の Path 入口はこれらへ委譲する。agent bundle を持たず研究 snapshot だけを独立して取り込む場合は `make_research_plan_from_sources(research_sources, settings)` を使う。この入口は raw Docs、6媒体の manual routine/run、URL finding、候補だけを計画し、Agent、Team/model binding、automation triggerを作らない。いずれも source-map はJSONデータとして扱い、権限・実行主体を入力から追加しない。

Library.read などの本文から派生 request を作る場合、取得元の ID/version、EOF とページ連続性、申告された member SHA と実際の派生表現 SHAを区別する。元ファイルの download・xattrs確認やZIP実バイト検証を行っていなければ、それらを成功とは記録しない。全行のcanonical hash照合と原文保存は、モデルが全文を読んだという主張とは別である。

原文1ファイルとAPIレスポンスの16MiB上限は維持する。offline生成planはraw Docs本文をpayloadとreuse照合に持つため、専用 `read_plan` の32MiB上限で読み、既存のplan/payload hashと許可endpointを検証する。CLIのapply/readback/resumeもこのreaderを使う。Path入力は容量確認後に有界readし、source-mapもUTF-8バイト数で同じ16MiB制限を受ける。

## run refsと中断再開

候補を保存するとserviceがrunのsource_refsを最大100件まで正当に拡張する。import-runのjournal hashは当初のsource_refsへ復元した不変projectionで照合し、実際のrefsは別途、同runに保存済みの計画候補から再計算する。候補key、owner/project/run/revision、日時・scores・原文投影・evidence・candidate hashと、存在するjournal bindingを照合し、計画順の重複排除・100件上限と一致するrefsだけを許す。元refsの除去や並替え、未知URL追加、候補・run不変fieldの改変は拒否する。

POSTのcommit後、journalに候補IDを記録する前の中断でも、保存済み未journal候補を照合してからidempotent POSTで再開する。従来のrun作成直後のjournal hashは互換を保つ。任意の外部改変後にjournalを再baselineする仕組みは追加しない。

## 原本と証拠の保持

- 最終 `*-benchmarks.json` の `records` と全6媒体の `*-evidence.jsonl` を使用する。証拠 ID は媒体をまたぐ共有 ID として照合する。同 URL を参照する別 ID は別の観測として残す。重複 ID、不明参照、衝突する alias、存在しない alias target は計画作成時に拒否する。
- `PUBLIC_BREADTH_RESUME4_VALIDATION.json.reused_evidence_event_aliases` だけを明示 alias として使う。元 record、alias 宣言、全 ledger は変更せず Docs の typed child に保存する。alias のために新しい証拠 row を作らない。
- `file_sha256` は読み込んだファイルの実バイトから計算する。`record_hash` は JSON parsed value の canonical 表現の SHA-256、`snapshot_hash` はファイル名と実ファイル SHA の canonical map の SHA-256 であり、ZIP バイトの hash ではない。Docs 本文では従来どおり CRLF/CR→LF だけを正規化する。
- 候補には原 record の ID/hash、ファイル hash、原文 child の `docs:<id>`、全 `evidence_refs` の hash を artifact evidence として固定する。個別 URL evidence には元 row/hash、ledger/hash と原文 child を保持する。これらの note は原文への参照であり、実行・権利・売上を確認したという意味を持たない。
- 元 record は全量を保存する。長い候補 summary は identity、sampling、money metadata の部分投影と明記し、元 claims、claim-level status、record-level state、null と missing_reason、観測時刻、金額区分、追加 payload へ固定参照で遡れるようにする。20件の typed evidence 上限では原 record 用1件と URL 最大19件を使い、note の `source_ref_count` / `inline_evidence_count` / `inline_omitted` で省略の有無を明示する。全参照は原文に保持する。

任意の `research-provenance.json` を同じ入力ディレクトリに置けば、通常の raw Docs 保存と固定 pointer/hash で上流 provenance を保持できる。これは必須ファイルでも元研究 ZIP の構成ファイルでもない。回収元が申告した archive/file SHA、Library ID、source version と、JSON 再整形後の入力 SHA を別項目にし、ZIP バイトを取込側が検証していなければその事実を明記する。申告情報を実バイト検証済みへ格上げしない。

## Snapshot 更新と Plan

候補 key は媒体、元 benchmark ID、snapshot hash の組から作る。同じ snapshot の再実行は同じ候補になり、変更した full snapshot は別版の候補と原文になる。更新時は別 journal を使い、既存 journal の payload を書き換えて途中実行を再開しない。過去版は保持するが、同じ benchmark の版数を新しく調査した件数に合算しない。

候補の human acceptance と ContentItem への promotion は引き続き別の操作である。ContentItem の brief には原 brief ID、原文 child、期待 hash と実行範囲の対応を明記する。例では正式 `BRIEF-EXP-A01-20261006` と過去テストの短 label `BRIEF-EXP-A01` を異なる文字列として扱う。原 brief の3課題や rights constraints は全文を保存し、実行 brief が参照する課題 ID と権利条件の参照を明示する。

GenerationPlan は既存の server-owned research context に ContentItem hash、候補 hash、原文参照と brief を固定する。別 session で Plan を読み直し、参照先 Docs の元 record と期待 hash を照合する。Docs 自体は編集可能なので、pointer の存在だけで後日の本文が元 snapshot と一致すると判断しない。権利は `unknown`、受入は `not_run`、売上は `unverified` を維持し、原文の null をゼロや確認済みに変えない。

Plan の既存8,000文字 prompt 上限、evidence note 2,000文字上限、ResearchRun source refs 100件上限は変更していない。原文の全量を provider prompt へ重複展開せず固定参照を使い、なお上限を超える場合は明示的に範囲を絞った ContentItem を作る。取込・保存・draft Plan の成功は、実生成、配信、課金結果や商品品質の受入にはならない。

## 回帰確認

`tests/test_mediaops_bootstrap.py` は global evidence、同 URL の別 ID、alias、長い原文、部分投影、更新版と欠損 graph を検証する。`tests/test_media_operations_research_generation_context.py` は隔離 SQLite の正規 Docs writer を使い、暗号化原文の保存・fresh session 読込、Candidate→ContentItem→Plan の ID/hash/pointer と unknown の保持、provider submission / GenerationRunIntent / Run / Output がゼロであることを検証する。共有 DB や実 provider を使用しない。

`tests/test_mediaops_bootstrap_sqlite.py` は実DocsGraphService/ResearchServiceを隔離SQLiteで使い、emit→read→apply、実candidate commit直後の中断、同snapshotのresume/replay、元run refsや不変field、未journal候補の日時・scores・hash改変拒否を検証する。通常DB・provider・モデル呼出しは使わない。
