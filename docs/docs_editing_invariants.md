# Docs編集不変条件

Docsノードの本文正本は `knowledge_nodes` の子ノード階層で表現する。

- `knowledge_nodes.body_text` は表示本文ではなく、title 由来の検索ミラーだけを入れる。
- 段落、箇条書き、補足、理由、属性は子ノードとして保存する。
- `body_json` は原則として `block_type` や bookmark などの構造メタデータ専用にする。
- prompt、command、URL、台詞などの逐語内容も通常の編集可能nodeとして保存し、`body_json.verbatim_content` を読み取り専用表示のために使用しない。複数nodeへ行単位で格納する場合も、改行・空行・順序を復元可能に保ち、整形・翻訳・要約しない。
- 原本file、path、line、SHA、excerptなどの照合情報をDocs nodeへ混ぜない。照合情報はDB外のprivate artifactだけに置く。
- Markdown風の `# 見出し` や `- 箇条書き` を本文文字列として保存しない。
- Foam由来の生 `[[参照]]` は残さない。解決できる参照は実在node IDを持つ移動可能な参照へ変換し、解決不能なら意味の通る通常文へ直す。

ノード文体は次の粒度にする。

- 1ノードは1主張にする。
- 理由や補足は子ノードへ落とす。
- 「ラベル: 値」は親ノードと属性ごとの子ノードに分解する。
- title は500字以下にする。

実装上の入口は `frontend/src/lib/server/docs-node-writer.ts` に統一する。
`knowledgeNodes` へ直接 `insert` / `update` したり、呼び出し側から `bodyText` を渡したりしない。
Python経路は `DocsGraphService` をwriterとして扱い、任意の長文を `body_text` へ保存しない。

案件に属するノードはDocsルートへ直接置かない。

- ルートの `案件情報` (`system_key=project_information_root`) は1件だけにする。
- 各案件の正本は `案件情報` の子にし、`system_key=project_information:<project_id>` を付ける。
- 会議メモは案件正本配下の `会議メモ`、メールは案件正本配下の `メール管理` に置く。
- default Inbox projectには案件情報正本を作らない。Docsの `Inbox` は一時投入先であり、案件ではない。
- ページを開くだけで「未記入」や一般的な説明文を生成しない。節は根拠のある内容を明示的に保存する時だけ作る。

Docs全体の整理は `docs/docs_curation_contract.md` を正本とする。
Foam原文は倉庫内の詳細を作る情報源として読むが、構文ベースimporterによる全面再取込はしない。
