# Docs編集不変条件

Docsノードの本文正本は `knowledge_nodes` の子ノード階層で表現する。

- `knowledge_nodes.body_text` は表示本文ではなく、title 由来の検索ミラーだけを入れる。
- 段落、箇条書き、補足、理由、属性は子ノードとして保存する。
- `body_json` は原則として `block_type` や bookmark などの構造メタデータ専用にする。ただし
  `format=doc_block` の typed block は、編集可能な表示本文を暗号化した
  `body_json.content` に保持してよい。typed block の `title` と `body_text` は検索用の
  label mirrorであり、contentの代替・不変本文ではない。
- 通常のoutlineで利用者が明示的に作成した空行は、空paragraph blockとして保存してよい。
  canonical representationは `title=""`、`body_text=""`、`node_type="node"`、
  `body_json.format="doc_block"`、`body_json.block_type="paragraph"`、
  `body_json.blank=true` の組み合わせに限る。この組み合わせを満たさない任意の空title
  KnowledgeNode（legacy/import由来を含む）は引き続き保存・表示してはならない。空paragraph
  に可視ラベルを補うことも禁止し、titleとbody_textのmirror原則は空文字同士で維持する。
- 短い1行のprompt、command、URL、引用は通常の編集可能nodeとして保存してよい。複数行、
  整形依存、長い行を含むClipIngest原文も、`markdown` または `code` の typed child
  KnowledgeNodeとして保存する。表示本文をreadonlyの `body_json.verbatim_blocks` /
  `body_json.verbatim_content` に保存してはならない。
- typed blockの `body_json.content` は入力から直接切り出し、許可される変換は改行コードの
  CRLF/CR→LFだけとする。空行、タブ、行頭・行末空白、連続空白、Markdown、コードフェンスは
  変更せず、contentを別の行ノードへ分割しない。`body_json.clip_ingest` にはsource、hash、
  flat line/char/blank metricsだけを保持する（legacy migration childの監査用aliasは除く）。
- コードの照合情報（line、SHA、excerpt）はtyped blockの `clip_ingest` provenanceと
  revision/source_refsに保持する。編集後の本文をsystem-managed immutable contentとして扱わない。
- 例外として、system-managed なユーザー添付の原本参照は許可する。`[[file:<path>|<ラベル>]]` 形式のリンク子ノードと、`email_source_path` などの型付きFieldへ保存する原本pathがこれにあたる。原本pathはワークスペース基準の相対pathに限り、excerptやSHAは持たせない。
- 普通のoutline nodeではMarkdown風の `# 見出し` や `- 箇条書き` を本文文字列として保存しない。
  ただし `doc_block` の `markdown`/`code` contentはraw入力を編集可能に保持するため例外とする。
- Foam由来の生 `[[参照]]` は残さない。解決できる参照は実在node IDを持つ移動可能な参照へ変換し、解決不能なら意味の通る通常文へ直す。

ノード文体は次の粒度にする。

- 1ノードは1主張にする。
- 理由や補足は子ノードへ落とす。
- 「ラベル: 値」は親ノードと属性ごとの子ノードに分解する。
- title は500字以下にする。

system-managed な構造化レコードは、検索・関連付け・原文保持に使う型付きFieldを正本にしてよい。

- `system_key=project_mail:*` かつメールSupertagを持つメールノードは、ヘッダー、本文、原本情報をメールFieldへ一度だけ保存し、同値の属性・本文を子ノードへ複製しない。
- メールへ利用者が追加した通常の子ノードはFieldの複製とは扱わず、通常の編集可能nodeとして保持する。
- `system_key=project_inbox_item:*` かつInbox項目Supertagを持つノードは、1回の `/inbox` 受付を1ノードとして扱う。Inbox ID、分類、対応状態、受付元、受付内容、取りまとめは型付きFieldへ保存する。本文は `概要` を最優先し、資料内容に必要な章だけを作る。複数回の応酬は必要な場合だけ `経緯` として意味的に圧縮し、各事実の直下へ根拠ノードをリンクする。追加情報は追記ログにせず、同じノードの文書全体へ統合する。`確認事項`、`次の対応`、`参考資料`、`原資料`、`更新履歴` を固定章として生成しない。

実装上の入口は `frontend/src/lib/server/docs-node-writer.ts` に統一する。
`knowledgeNodes` へ直接 `insert` / `update` したり、呼び出し側から `bodyText` を渡したりしない。
Python経路は `DocsGraphService` をwriterとして扱い、任意の長文を `body_text` へ保存しない。
typed blockの長文は暗号化された `body_json.content` に保存する。
ClipIngestの原文契約は `docs/clip_ingest_contract.md` を正本とする。

既存ノードの `verbatim_blocks` / `verbatim_content` は表示経路として使わず、
`scripts/migrations/migrate_verbatim_content_to_typed_blocks.py` のdry-run後に
typed childへmaterializeする。block順序、label、content、hash/char/line/blank metricsを
検証し、子作成と完全性検証が成功した親だけlegacy keyを除去する。migration markerで
再実行を検出し、child missing/edited/conflictはfail closedする。

案件に属するノードはDocsルートへ直接置かない。

- ルートの `案件情報` (`system_key=project_information_root`) は1件だけにする。
- 各案件の正本は `案件情報` の子にし、`system_key=project_information:<project_id>` を付ける。
- 会議メモは案件正本配下の `会議メモ`、メール原本は案件正本配下の非表示管理領域 `メール管理` に置く。引用チェーンは個別メッセージへ分割し、Inbox本文から該当メッセージへ直接リンクできるようにする。
- `/inbox` の管理ノードは各プロジェクトの案件情報正本直下に `Inbox` として1件だけ置き、その子を1受付=1つの `Inbox項目` とする。
- default Inbox projectには案件情報正本を作らない。プロジェクトに属さない従来のDocsルート `Inbox` は一時投入先であり、`/inbox` の管理ノードとして使わない。
- ページを開くだけで「未記入」や一般的な説明文を生成しない。節は根拠のある内容を明示的に保存する時だけ作る。

用語は、`プロジェクト`（業務・顧客単位）、`Inbox`（プロジェクト内の管理ノード）、`Inbox項目`（1回の受付）、`タスク`（実行が必要な作業）、`原資料`（メールや資料）を使い分ける。Inbox項目を「案件」と呼ばない。

Docs全体の整理は `docs/docs_curation_contract.md` を正本とする。
Foam原文は倉庫内の詳細を作る情報源として読むが、構文ベースimporterによる全面再取込はしない。
