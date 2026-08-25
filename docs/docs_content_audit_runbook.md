# Docs内容監査の手順

## 記録の分離

- このファイルには、全ノードで再利用する調査・修復方法だけを書く。
- 個別ノードの調査結果と進捗は `docs/docs_content_audit_ledger.jsonl` に書く。
- 個別ノード名、個別の修正案、個別の完了状況をこの手順書へ混ぜない。

## 意味不明な1行から移行元を特定する方法

1. 現在のDocs DBで対象ノードの `id`、`title`、`parent_id`、`system_key` と、親・兄弟・子を取得する。
2. `system_key` が `foam_source_grounded_v1:node:` で始まる場合、その接頭辞を除いた値をmanifest上のnode keyとする。
3. `artifacts/foam_curation/phase3_source_v1/manifest.json` の `nodes` からnode keyを引き、元ブロックのタイトル、`block_type`、`source_kind` を得る。
4. 同manifestの `placements` を親方向へ辿り、`source.<文書ID>` まで遡る。対象ブロックだけで判断せず、元の親・兄弟・子の並びも取得する。
5. `artifacts/foam_curation/phase3_source_v1/provenance.private.json` の `sources` から、その `source.<文書ID>` と一致する `node_key` を引き、元Markdownの絶対パスとSHA-256を得る。
6. 元Markdownを実際に読み、manifestで得た親子関係と前後の文章を照合する。同名の見出しや文章が複数ある場合、タイトル一致だけで決めず、親階層・前後ブロック・出現順がすべて一致する箇所を採用する。
7. 元の意味が親や子に依存していた場合、1行だけを残さない。文書内での主語、用途、親子関係、必要な詳細を復元対象に含める。

## `system_key`から直接辿れない場合

1. 対象ノード自身だけでなく、親・兄弟・子の `system_key` を確認し、同じ移行元文書へ属するキーが残っていないか調べる。
2. `knowledge_revisions.source_refs_json` に移行元参照が存在する場合は、それを使ってmanifest node keyへ戻る。
3. 現在タイトルとmanifestのタイトルを照合する場合は候補発見にだけ使う。親階層・兄弟・子・前後ブロック・元Markdownが一致するまで移行元確定とは扱わない。
4. 根拠が一意にならない場合は推測で修正せず、進捗台帳を `blocked` にして不足情報を記録する。

## 読解と修復

- 対象は必ず親・兄弟・子・移行元を一緒に読んで判断する。
- 通常情報は、意味と具体性を保った短い文章へ要約する。
- プロンプト、URL、コマンド、具体的手順、商品名、数量、価格、日付など、改変で壊れる情報は原文を保持する。
- 文脈のない文章断片、元文書の1行目だけ、同名入れ子、一子だけの無意味な中間ノードを残さない。
- 親タイトルの主題を、複数の編集可能な子ノードが説明する構造へ直す。
- 調査完了、DB反映、反映後確認を別状態として扱い、確認前に完了としない。

## 中断後の再開

1. この手順書を読む。
2. `docs/docs_content_audit_ledger.jsonl` を読み、`verified` ではない最古の項目から再開する。
3. DB反映前は `applied` にせず、反映後に親子関係、内容、原文保持箇所を再読してから `verified` にする。
