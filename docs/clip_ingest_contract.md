# ClipIngest保存契約

ClipIngestは、保存先分類、再利用可能なknowledge意味ノード、loss-sensitiveなtyped source block、取得状態を一つの検証済みplanからtransactionalに保存する。Web、サーバー、モバイルオンライン、モバイルローカル、オフラインで同じデータ契約を使う。

## 保存先探索（Phase 1）と保存内容（Phase 2）

自動取り込みでは保存内容の生成と保存先探索を分離する。

1. Phase 1の初回routerには、fallbackを除く候補の `node_id`、`title`、`breadcrumb` だけを渡し、説明（`routing_hint`）は渡さない。
2. routerが選んだ1候補の `routing_hint` だけを順番に確認し、`accept` / `inspect` / `fallback` を返す。確認は `MAX_ROUTING_INSPECTIONS=3` 件で打ち切り、上限到達時は確認済み候補全体を比較する。
3. `confidence < 0.72`、`ambiguous`、候補外ID、再訪、malformed JSON はコード側で安全にfallback（未設定なら失敗）とする。
4. 確定後のPhase 2は確定targetをコード側で固定し、title/knowledge_items/typed source ranges/添付配置だけを生成する。返却 `target_id`、分類状態、actionは信用しない。
5. Phase 3では、確定target直下の既存topic候補と保存計画を比較してcreate/appendを判定する。`target_node_id` 明示時はPhase 1（保存先倉庫の探索）だけをスキップし、選択targetを保存範囲のcontainerとして固定する。Phase 3はスキップせず、container自身への直接appendは行わない。

## Phase 3と明示保存先の意味

`target_node_id` は「このノード自身へ直接追記する」という指定ではなく、
**「このノード配下を今回の保存範囲（container）として固定する」**という指定である。
自動分類でも明示指定でも、Phase 3の結果は必ずcontainer直下のtopicになる。

- Phase 3はコード側の候補検証と統合判定であり、保存計画が返したtarget・action・候補外IDをそのまま信用しない。
- 既存topicへのappendは、同一対象（同じ製品・モデル・手法・同じ疑問への続報または補足）と厳密に証明でき、appendのconfidenceが既存の閾値以上の場合だけ許可する。同じ分野・用途・似た語感という表層的な近さだけではappendしない。曖昧、候補外、confidence不足、判定失敗はcreateへ倒す。
- 選択container配下に同じtopicがなければ新しいtopic childを作る。1クリップ1topicを機械的に強制するのではなく、同一topicと証明できるときだけ既存topicへ統合する。
- `subject`/`title_detail`から決めたtopic titleはcontainerの子に置く。`knowledge_items`、excerpts、short literals、prompt等の意味・原文ノード、`出典`とそのURL、Research Plannerが採用したsupplemental source、添付は、すべて確定topicまたはそのsemantic childの下に閉じる。container直下にこれらを平坦化しない。
- text-onlyとattachmentありは同じtopic境界を持つ。添付の `root` anchorは選択containerではなく確定topicを意味し、`knowledge:N`等のanchorも同じtopic配下で解決する。添付のSHA dedupe、provenance、ACL、transactionは既存契約を維持する。
- 明示指定時はcontainerの既存本文、`body_json`、legacy原文key、`clip_ingest` metadata、`sort_order`を取り込み単位の保存先として更新しない。自動分類の既存topicへの並び替え規則も、明示指定ではcontainerの順序を変更しない。
- 結果の `target_id`/`target_label` は固定したcontainerを示す。`changed_node_id`/`changed_node_title` はcreateまたはappendで実際に変更したtopicを示し、重複skipではnullとする。`open_node_id`/`open_node_title` は、create/append/duplicate_skipのいずれでもユーザーが確認すべき実topicを示す。container自身をchanged/open topicとして返さない。

この境界により、たとえば `MiniMax H3` を明示保存先にした場合でも、各クリップの知識・原文・出典・添付は `MiniMax H3` 配下の確定topic単位に整理され、別テーマのクリップと兄弟平坦化されない。

## 保存計画 v4（v2/v3/legacy互換）

wireの正本は `schema_version: 4` である。サーバーは旧 `schema_version: 2/3`
（およびschema_versionのないlegacy plan）を読み取り互換として受理するが、
新しく保存する `body_json.clip_ingest.schema_version` は必ず4へ揃える。未知の
schema versionは拒否する。`body_json.clip_ingest` はschema/content modeだけを
保持し、knowledge本文を重複保存しない。本文の正本はtopic直下のKnowledgeNodeである。
複数行原文は `format=doc_block` の `markdown`/`code` childへ保存し、
`body_json.content` が編集可能本文、`title`/`body_text` がlabel mirrorとなる。

LLMは次をJSON objectとして返す。

- `target_id`, `matched`, `ambiguous`, `confidence`, `action`
- `subject` と `title_detail`、それぞれの根拠を示す `title_evidence`
- `content_mode`: `summary` / `verbatim` / `mixed`
- `knowledge_items`: sourceを開かなくても直接再利用できる意味単位（最大8件、各480字）
- v2/v3/legacyの `summary`, `details` は読み取り時に `knowledge_items=[summary,*details]` へ正規化する。
- 1行だけの `short_literals`
- 元sourceの行範囲を指す `verbatim_ranges`
- `used_supplemental_urls`, `unconfirmed`
- 任意の `attachment_placements`（`root`、`knowledge:N`、旧 `summary`/`detail:N`、
  `excerpt:N`、`source` の論理anchor）も指定できる。旧anchorは対応する
  knowledge直下ノードへのaliasとして解決する。

`routing_hint` は候補ターゲットの分類説明だけに使用し、titleや階層の生成指示には使用しない。旧planの `topic` / `excerpts` は互換入力として受理するが、複数行・空行・インデント・長い行を含む入力は全入力を原文ブロックへ退避し、欠落を防ぐ。

### source IDと一行プロンプト

- 原文範囲のcanonical IDは `source:0`（ユーザー入力）、`source:N`（直接取得URLを
  入力順に採番）、`supplemental:N`、`attachment:<upload_id>`。旧 `input` は
  `source:0`、旧 `direct:N` は `source:N` のlookup aliasとしてのみ受理する。
- 再利用するpromptは1行であっても、必ず `verbatim_ranges` に
  `{"kind":"prompt","source_id":"source:0|source:N", "start_line":N,
  "end_line":N}` を出力する。`short_literals` でpromptを代用しない。
- plannerが範囲を省略した場合のcode-side repairは、入力にprompt/プロンプト等の
  明示marker（またはtitle/labelの明示prompt語）があり、かつ非URLの実質1行だけ、
  もしくは `content_mode=verbatim|mixed` の場合に限る。通常の一行proseは
  自動でverbatim化せず、範囲を出したplanだけを採用する。

## titleと階層

- titleは根拠のある `subject` と `title_detail` をコード側で `subject - title_detail` に組み立てる。
- `topic`/`subject` は、source中で最も目立つ固有名詞を選ぶ欄ではない。clipから後で再利用したい中心知識（手法、知見、疑問、構図、workflow、設定目的）を第一に選び、その知識を識別するために必要な製品名・モデル名を次に考慮する。使用モデル、実行環境、出典、作者、providerなどの付帯metadataは、clip自体がそのモデル・環境・人物・出典を説明する内容でない限りtopicへ昇格させず、`short_literals`（`kind=setting`）またはprovenanceとして保存する。`model:...`、`model=...`、`使用モデル:`、`checkpoint:`、`provider:`、`author:`、`source:` などの行だけを理由にsubjectを決めてはならない。
- 表示用の `topic`/`subject` と、knowledge source prefilterだけに使う内部 `topic_anchor` は別物である。中心知識の自然なsubjectをidentifierとして無理に扱わず、metadata行にあるモデル等のidentifierを内部anchorとして使う場合も、既存のstrict境界・完全一致・version/polarity/chimera gateを緩めない。
- titleの要約はsource-groundingを保つ自然な表現差を許容するが、`title_evidence` で指定した単一のsource range内に、中心語と主要述語が存在することを必須とする。自由なsemantic fuzzy matchや、根拠範囲にない未知の製品名・数値・version・能力のtitleへの追加は禁止する。既存のstrictなentity境界、unsupported fact、cross-source chimeraの検査を緩めてはならない。
- このtopic/subject選定とtitle groundingの意味論は、serverとmobile（オンライン、local/offlineを含む）で同一に適用する。
- 上限は240 Unicode code point。subjectを優先し、detail側を短縮する。
- 根拠がないdetailは捨て、subjectだけを許容する。URLの未知の内容は補完しない。
- title/detailと同義のknowledge itemは保存しない。
- `knowledge_items` は確定topic直下の兄弟ノードとして保存し、`おすすめ理由`、`概要`、`特徴`のような汎用ラベルを1段挟まない。appendでも新規生成ノードはtopicの直接子にする。
- command、設定値、URL、短い引用は、意味のあるラベルと値の2段構造を維持する。
- 未取得URLは `元リンク（本文を取得できず内容は未確認）` の下にURLを置く。

## first-class編集可能原文

原文の正本は確定topic配下の typed child KnowledgeNodeに置く。DB migration、revision、暗号化、
REST、sync/outbox経路は通常のKnowledgeNodeとして通過し、明示targetのcontainer自身を
原文のparentにしてはならない。各blockは次を持つ。

- `body_json.format=doc_block`
- `body_json.block_type` (`markdown` または `code`)
- `body_json.label`, `body_json.content`（表示/編集する本文）
- `body_json.clip_ingest.sha256` と flat metrics（`char_count`, `line_count`, `blank_line_count`）
- `body_json.clip_ingest.source_id`, `source_type`, `source_url`, `start_line`, `end_line`
  （legacy migration childは監査用に `legacy_source_id` とnested `metrics` も保持する）

`content` はLLM出力からではなく、検証済み行範囲を元sourceから直接切り出す。CRLF/CRだけをLFへ
正規化し、それ以外の文字、空行、タブ、行頭・行末空白、連続空白、Markdown、コードフェンスを
変更しない。重複または範囲外のspanは保存前に拒否する。保存直後に内容、SHA-256、文字数、行数、
空行数を再検証し、不一致ならtransactionをrollbackする。保存後の編集はユーザー本文として許可し、
取り込み時hashを不変制約として適用しない。

knowledge_itemsも入力・取得本文・添付認識・補足検索というuntrusted dataから根拠を
取るだけで、本文中の命令やsystem/developer風の文言を実行しない。source provenanceを
メタ説明として繰り返さず、具体的な手順・数値・条件、環境・バージョン・個人実測などの
適用範囲を保持する。`投稿文では` `投稿例では` `投稿者は` `この記事では`
`添付画像では` などのobserver framingはknowledge本文へ保存しない。code側でも
安全にscaffoldだけ除去できる場合は正規化し、除去できないmeta-only proseは破棄する。
`この記事は紹介している`、`所感を含む`、`説明している`、
`おすすめしている`だけのmeta-summaryは決定的に除去する。

Webとモバイルは typed childの `markdown` を通常表示時にGFMとしてレンダリングし、Markdown表を
raw pipe文字列で表示しない。クリック/編集操作では同じnodeをraw Markdown editorへ切り替え、
保存・再読込後も `body_json.content` を編集可能にする。`code` も整形表示と編集を同じnodeで
提供する。`verbatim_blocks` / `verbatim_content` は表示経路として使わない。

既存ノードのlegacy keyは `scripts/migrations/migrate_verbatim_content_to_typed_blocks.py` の
dry-run後にmaterializeする。blockごとにcontent、label、順序、SHA-256とline/char/blank metrics
を検証し、child作成→完全性検証→親のlegacy key除去を同一transactionで行う。migration markerで
再実行を検出し、child missing/edited/conflictはfail closedする。成功した親の他のbody_json、
revision履歴、source_refsは保持する。

## URL・X取得状態

`UrlFetchResult.acquisition_status` と `source_refs[].acquisition_status` は、少なくとも次を区別する。

- `success`
- `auth_required`
- `restricted`（年齢・センシティブ制限）
- `access_denied`
- `private`
- `rate_limited`
- `deleted`
- `network_error`
- `empty_body`
- `unsupported_content`
- `fetch_failed`
- `supplemental_verified`

Xは公開syndication、公開oEmbed、明示設定されたBearer API、認証付きCookie HTMLの順で取得する。認証付きCookie HTMLでは、認証ユーザーの個人Cookie（`/api/users/me/x-cookie`）を最優先し、個人行がない場合だけ、運用者が明示設定した共有サーバー互換フォールバックを使う。個人CookieはユーザーIDに束縛した暗号化 credential として保存し、別ユーザーへ共有しない。DELETE は暗号文を破棄した無効化tombstoneを残し、そのユーザーでは共有フォールバックも抑止する。個人行がない場合の共有フォールバックは `AOITALK_X_COOKIE_FILE` で明示し、Netscape形式、有効期限内、X/Twitterドメイン、`auth_token` と `ct0` を満たす場合だけ取得層で使う。

Civitai（`civitai.com` / `civitai.red`）のモデルページは公開REST API（`/api/v1/models/{id}`、必要なら `/api/v1/model-versions/{id}`）から取得する。HTMLスクレイピングとCookieは使わない。mature誘導ページやCloudflare challengeを成功扱いにしない。モデルID / version IDをURLから判別できないCivitaiページだけ、通常のHTML取得へ落とす。

Cookie管理APIは本文をUTF-8のNetscape形式（multipart/ファイル名なし）で受け取り、ファイルへ書き出さずメモリ上で検証する。HttpOnly の `auth_token` は Console JS から取得できないため、Console JS acquisition は行わない。Cookieの生値・平文はDBへ保存せず、canonicalな最小構造を暗号化した暗号文だけを保存する。Cookie値、token、header、パス、ファイル名はprompt、履歴、ログ、例外、API応答へ渡さない。APIのGETは `service/status/configured/source/scope/updated_at` の安全な状態だけを返し、常に `Cache-Control: private, no-store` を付与する。

直接取得に失敗したURLをWeb検索で補足した場合、補足sourceは `supplemental_verified` とし、direct sourceとして偽装しない。ユーザー入力と未取得URLは常に保存対象に残す。

## request単位の外部追加調査モード

`enable_external_research: bool` は、今回の取り込みだけに適用する **trusted HTTP request
field** であり、Researchの実行可否を決める唯一のauthorityである。省略時は必ず
`true` とする。したがって、旧Web client、mobile、その他のAPI利用者がこのfieldを
送らなくても従来挙動（Research ON）を維持する（mobile APIは今回このfieldを追加せず、
defaultに委ねる）。DB、global/user settings、configへ
永続化する設定ではなく、履歴から再実行する場合だけ元requestの値を引き継ぐ。旧履歴に
fieldがない場合も `true` とし、ダイアログを開くたびの初期値は `true` に戻す。

- `enable_external_research=true`（省略を含む）は従来どおり、URL本文の直接取得、
  Research Planner、WebSearch、Evidence Judge、および取得失敗時のrecoveryを実行できる。
- `enable_external_research=false` は外部追加調査を意図的に無効にする。この場合は
  URL本文の取得（`UrlIngestService.fetch_all()`）、Research Planner、WebSearch、
  Evidence Judge、取得失敗URLのrecovery検索を一切呼ばない。取得していないURLに対して
  `UrlFetchResult` を人工生成したり、success / `fetch_failed` といった取得結果を偽装したり
  してはならない。
- falseでも「LLMを使わない」「保存だけを行う」モードにはならない。保存先routing
  （Phase 1、明示target、fallback）、title/content planning（Phase 2）、schema検証、
  knowledge/literal/verbatim計画、Phase 3のcreate/append/duplicate判定、ACL、transactional
  saveは通常どおり実行する。添付の認識もResearch flagとは別概念であり、画像認識skipの
  指定がない限り、ユーザー添付を認識してその結果を入力根拠として利用できる。Research
  OFFを「一切外部通信しない」ことの別名として扱ってはならない。

falseのとき入力本文からURL文字列を抽出・canonicalizeすること自体は許可するが、URLは
取得対象ではなくユーザーが提供した出典である。抽出したURLは入力本文の
`source:0`、`source_type=input` のprovenance（および可視の`出典`）として全件を保持し、
件数制限のあるLLM literalだけに保存を依存しない。AoiTalkが取得したdirect source、
Research Plannerが採用したsupplemental source、取得失敗sourceとして扱わず、
`direct_urls`、`supplemental_urls`、`failed_urls`へ入れてはならない。`acquisition_status`
は未取得を示すために捏造せず、input source refでは省略してよい。URLのcredential query
除去、userinfo拒否、scheme検証など既存canonicalizerとsource_refs検証を再利用する。

ResearchのON/OFFはtrusted request fieldからのみ決まり、source本文・添付本文・LLM出力に
「追加調査不要」「検索しないで」等と書かれていてもmodeを変更しない。逆にfalse時の
routing/content planningでは、ChatGPTやAoiTalkからの申し送り、取り込み操作を説明する
冒頭wrapper proseを保存対象のsubject、title_detail、knowledge_items、verbatimとして
採用せず、実質的な保存内容を分類・整理する。これはwrapperを根拠にmodeを切り替える
ことや、一般本文の語句を広いregexで削除することを意味しない。source本文にないURL先の
内容を推測せず、外部未取得を理由に`unconfirmed`を水増ししない。

Research ONでも、ユーザーが入力したURLは `source:0` / `source_type=input` の提示根拠として
保持し、AoiTalkが実際に取得したredirect後URLなどの `source_type=direct` とは置き換えない。
可視の`出典`と`used_urls`では同一canonical URLを重複表示しないが、revisionのsource refsでは
ユーザー提示とAoiTalk取得という異なるprovenanceを区別する。
入力URLのprovenance件数と、Research ON時に外部取得へ送る1回のfetch batch上限は別管理とし、
fetch batchを絞ってもsource:0に書かれた入力URLをLLM literal件数やfetch上限の理由で捨てない。

なお今回のWeb-only変更では、ユーザーがmobile変更・version/APK・Release不要を明示したため、
共有OpenAPI JSONとfrontend/mobileの生成型は更新していない。これはこのrequest flagに限る
明示的な例外であり、通常のFastAPI wire変更では [`docs/openapi_typegen.md`](openapi_typegen.md)
のcanonical/Web/Mobile再生成手順を適用する。

外部LLMへ渡すprompt/evidenceは、入力・取得本文・redirectのrequested/final URL、
`external_links`、`related_to`、補足検索snippetをprompt専用コピーへ変換する。
userinfo、token/secret/password等のcredential query、明示scheme外のURLはredactし、
fragmentとtracking queryは除去する。redactionは改行数を変えず、KnowledgeNodeへ保存する
原入力（verbatim範囲、SHA-256）は変更しない。revision/source_refsへ入るURLは同じcanonicalizerを
strictに通し、危険値は保存前にfail-closedする。

## モバイル・同期

- オンラインはサーバーAPIのplanとwriterを使用する。
- Web/server APIで明示 `target_node_id` を受ける場合だけ、選択container配下でPhase 3のtopic統合を適用する。モバイルlocal/APIは現時点で `target_node_id` を公開せず、自動target選択からtopicを保存する経路だけを使う。オフラインで明示target保存をサポートすると解釈してはならない。
- ローカルLLMはv4 planを正本として検証し、v2/v3 planも互換入力として同じtitle、semantic children、editable typed blockを作る。
- LLMが使えないオフライン時は、根拠のないdetailを作らず、入力全体を原文ブロックとして保持する。
- local node createの `body_json` はoutbox payloadへ含め、sync conflict検証でもpayloadに含まれる場合だけ比較する。古いclientのpayloadは従来どおり受理する。
- syncのcreate/update payloadには任意の `source_refs` を含められる。これはACL確認後に
  bounds/JSON型/相対path/URL schemeを検証し、absolute path、URL認証情報、token/secret等の
  秘密値を拒否したうえで、`KnowledgeRevision.source_refs_json` へ渡す。source_refsは
  node列ではなくrevision provenanceである。

## 既存データの修復

既存ノードは自動で一括rename・再構成しない。legacy `verbatim_blocks` /
`verbatim_content` のみは、修復前に必ずdry-runを作り、対象node ID、parent、現在のtree、
revision/change summary、source refs、URL、添付provenance、blockのsource ID・範囲・hash、
修復後treeを再現可能な記録として残す。

次の条件を**すべて**満たす場合に限り、lossless structural repairを許容する。

- revision、`source_refs_json`、source URL/ID/range/hash、attachment upload ID/SHA、child作成時刻などの既存provenanceから、Clip境界と対応topicを一意に証明できる。
- 既存node ID、knowledge本文、URL、添付bytes/metadata、revision履歴を再生成・改変せず、
  検証済みlegacy blockをtyped childへmaterializeし、親のlegacy keyを成功後に除去する。
- materializeするblockはcontent、SHA-256、行数・空行数・source範囲が移行前後で完全一致し、
  source refsを捏造しない。CRLF/CR→LF以外の変更、contentの推測補完、行の再折返しはしない。
- 非ClipIngestのユーザー編集本文、別機能のsource、既存ACL・transaction・同期履歴を壊さず、修復前の状態へ戻せる記録がある。

タイトル、意味的な近さ、時刻、並び順、URLの推測だけでClip境界やtopicを再構成する**semantic reconstructionは禁止**する。provenanceが一意でないnode、group、root `body_json`、source wrapper、添付は削除・統合・移動せず、未修復として残す。推測で補完する代わりに、新契約で再取り込みできる範囲を案内する。dry-runで検証可能なparentだけを適用し、曖昧なparentはlegacy keyを残したまま中止する。

旧契約の明示text-only appendのように元のsemantic topic title自体が保存されていない場合でも、Clip境界（rootのKnowledgeRevisionとdirect child集合）が一意に証明できるgroupは、**元titleを復元したと主張しない新規のprovenance-only repair container**へ構造移動してよい。container titleは元source本文・child title・LLMから推測せず、完全な元Clip revision UUIDを決定的に埋め込む（例: `ClipIngest repair — revision <UUID>`）。新containerの `body_json` は `repair_container_is_new=true`、`original_topic_title_recovered=false`、`label_semantics=provenance_only`、`source_revision_id=<UUID>` を記録し、`source_refs_json` は元revisionのexact refsだけを複製する。これは意味topicの復元ではなく、証明済みClip event境界を可視化する構造修復である。source refs・child所属・本文・verbatim・添付のpreconditionが一致しないgroup、または元titleをsemantic titleとして再現しようとする変更は引き続き禁止する。
