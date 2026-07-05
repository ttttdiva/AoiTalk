# 案件情報 Docs 正本化設計

## 目的

案件情報を、固定フォームではなく Docs の正本ページとして扱う。ユーザーは Web ページのように読みやすい案件ページを、その場で直接編集できる。エージェントは同じ正本を読み書きし、チャットで発生した疑問は Q&A として後から再利用できる。

この設計で避けるもの:

- 見出し数、入力欄数、カテゴリ名が固定された案件情報フォーム
- 編集ボタン、プレビュー切替、別画面編集
- 「最新状況」「次アクション」のような曖昧な固定枠
- AI が抽出した要点を、案件情報本文とは別の見せ物として並べる UI
- 旧 project information DB と Docs の二重正本化

## 現状の前提

- Docs の本文正本は `knowledge_nodes.body_text` と `knowledge_nodes.body_json`。
  - `frontend/src/db/schema.ts`
  - `src/memory/models/knowledge.py`
- Docs node は `project_id` を持てるため、案件と直接紐づけられる。
- Docs のスーパータグは `knowledge_supertags`、node との紐づけは `knowledge_node_supertags`、型付き field は `knowledge_fields` / `knowledge_field_values`。
- Docs API は node 作成時に `project_id`、`supertag_ids`、`field_values` を受け取れる。
  - `frontend/src/app/api/docs/route.ts`
  - `frontend/src/app/api/docs/nodes/[id]/route.ts`
  - `frontend/src/app/api/docs/nodes/[id]/fields/route.ts`
- 案件情報タブは canonical Docs node と Q&A、record table 参照を読む。旧 `project_info_categories` / `project_documents` / `project_facts` は現行正本ではない。
  - `frontend/src/app/api/projects/[id]/information/route.ts`
  - `frontend/src/components/projects/project-information-panel.tsx`
- エージェント文脈には、すでに project 紐づき Docs node も混ざる。
  - `src/services/context_builder.py`
- 案件情報の本文は Docs (`knowledge_nodes.body_text/body_json`) を正本にする。機密保存要件は会話本文や record table と同じ扱いで監査する。

## タグとスーパータグ

通常タグは検索・分類用のラベルに近い。スーパータグは「この node は何の型か」を定義するオブジェクト型で、field、テンプレート、AI への指示、UI レンダリング規則を持つ。

案件情報では、単に `#案件情報` を付けるだけでは不十分。`案件情報` スーパータグを作り、次の責務を持たせる。

- この Docs node が案件情報の canonical page であることを示す。
- エージェントが本文をどう更新してよいかを定義する。
- Q&A、参照カード、構成図、ファイル参照などの埋め込みブロックを扱う。
- Project への一意な紐づけと権限確認を支える。

## 正本モデル

### Canonical page

案件ごとに 1 つの canonical Docs node を持つ。

推奨:

- `projects.knowledge_node_id` を正式に model/schema/API へ復旧し、canonical 案件情報 node へのポインタにする。既存 migration で追加済みの可能性があるため、Alembic は重複追加ではなく存在確認つきにする。
- `knowledge_nodes.project_id` も必ず入れる。
- `knowledge_node_supertags` で `案件情報` スーパータグを付ける。
- canonical 判定は `projects.knowledge_node_id` を第一優先にし、欠けている場合だけ `project_id + 案件情報 supertag + canonical field` から補完する。

理由:

- `project_id` と supertag だけでは複数 node ができた時に正本が曖昧になる。
- 以前の migration に `projects.knowledge_node_id` の意図があるため、別名の新概念を増やすより、DB 既存列と SQLAlchemy / Drizzle / serializer を整合させる方がよい。

### 本文

ユーザーが見る案件情報の中心は `knowledge_nodes.body_text/body_json`。

- `body_text`: Markdown 互換の編集可能テキスト。検索、エージェント入力、差分レビューに使う。
- `body_json`: 埋め込みカード、参照カード、画像、構成図、Q&A block placeholder などの block metadata を持つ。Q&A の質問・回答そのものは重複保存しない。
- `knowledge_revisions`: 保存履歴。エージェント更新時も必ず revision を残す。

本文は自由構成にする。標準テンプレートとして `概要`、`進捗`、`課題管理`、`決定事項`、`構成`、`検証` などを初期表示しても、見出し名・数・順序はユーザーが自由に変えられる。

### 暗号化と履歴

案件情報 Docs 本文には会話本文と同等の機密保存要件を適用する。

- `knowledge_nodes.body_text` と `body_json` は暗号化対象にする。
- `knowledge_revisions.body_text/body_json` も同じく暗号化対象にする。
- Next.js API と Python agent tool の両方で、同じ暗号化/復号経路を使う。
- 検索用には平文正本ではなく、権限確認後に生成する派生 index を使う。
- 既存 Docs node がある場合は、暗号化 migration と読み取り互換を用意する。
- 暗号化、revision、検索 index、backfill が揃うまで、旧 DB から Docs への正本切替は行わない。

### 案件情報スーパータグ field

`案件情報` スーパータグに最低限の field を持たせる。field は UI の固定入力欄ではなく、検索、agent routing、自動更新制御のための metadata。

| field | type | 目的 |
| --- | --- | --- |
| `Project` | `project_ref` | 対象案件。必須。 |
| `Page Role` | `select` | `canonical`, `child`, `archive`。 |
| `Agent Update Policy` | `json` | エージェントが本文更新してよい範囲、レビュー要否、根拠必須条件。 |
| `Q&A Enabled` | `checkbox` | チャット由来 Q&A 化の対象にするか。 |
| `Source Scope` | `json` | 参照対象にする会話、Docs、ファイル、record table の範囲。 |
| `Progress Digest` | `text` | 一覧・検索用の短い進捗要約。本文の正本ではなく派生 cache。 |

`進捗` は意味のある概念なので残す。ただし固定パネルではなく、本文中の見出しにも field の短い digest にもなれる。

## UI 設計

### 基本体験

- 案件情報タブは旧固定 DB パネルではなく、canonical Docs node を直接開く。
- 編集ボタンは置かない。ページ自体が常に編集面。
- プレビュー切替は置かない。Markdown 記法は表示に反映されながら、その場で編集できる形にする。
- 保存は自動保存を基本にし、保存状態だけを控えめに出す。
- 見出し、本文、箇条書き、表、画像、参照カード、Q&A block は同じ document flow の中に置く。

既存 `DocsNodeEditor` をそのまま埋め込むだけでは不可。現行 Docs editor には preview mode / preview toggle があるため、案件情報タブでは `ProjectInformationDocumentEditor` のような専用 variant を作る。

- 常時直接編集。
- preview toggle なし。
- 編集ボタンなし。
- 自動保存。
- 案件情報スーパータグ用 renderer を内蔵。
- 同じ部品を通常 Docs 側にも後で戻せるが、通常 Docs editor の既存 preview mode に案件情報を従わせない。

### 見やすさの作り方

単なる Markdown エディタにしない。`案件情報` スーパータグが付いた node では、本文中の block を読みやすくレンダリングする。

- H1/H2/H3、lead paragraph、callout、table をランディングページ風に整える。
- `進捗` や `課題管理` は見出し・カードとして表現できるが、固定枠にはしない。
- 画像、構成図、リンク、Docs 参照、ファイル参照、record table 参照をカードとして本文へ埋め込める。
- カードは装飾用ではなく、クリック可能な参照・根拠・添付として扱う。
- 右上の Docs ボタンに逃がすのではなく、案件情報タブそのものを Docs-backed editor にする。

### 埋め込みカード

`body_json.blocks[]` にカード metadata を持たせ、`body_text` には読みやすい fallback marker を残す。ここで扱うのは file/url/docs/record table/image/diagram などの参照カードであり、Q&A の質問・回答本文はここに持たせない。

例:

```json
{
  "type": "reference_card",
  "kind": "file",
  "title": "構成図",
  "target": "_projects/project_x/design/network.png",
  "caption": "最新版の構成図"
}
```

対応する fallback:

```md
[[file:_projects/project_x/design/network.png|構成図]]
```

これにより、エージェントと検索は text を読める。UI は rich card を出せる。

## Q&A 設計

### 役割

Q&A は固定 semantic block として持つ。チャットでユーザーが案件について疑問に思ったことを、後で人間と AI が見返せる形で残す。

これは「AI 抽出した要点」ではない。案件情報本文そのものを AI が更新するのとは別に、「人間が何を疑問に思ったか」を保存する補助データ。

Q&A の正本は `project_qa_entries`。Docs 本文側は Q&A block の表示位置と表示条件だけを持つ。

- `project_qa_entries`: 質問、回答、状態、根拠、重複回数の正本。
- `body_json.blocks[]`: `{"type":"project_qa_block","source":"project_qa_entries","filters":...}` のような placeholder。
- `body_text`: `[[project-qa]]` のような fallback marker。

これにより、Q&A が table、body_json、body_text に三重保存される状態を避ける。

### DB

新規テーブル `project_qa_entries` を追加する。

| column | type | 目的 |
| --- | --- | --- |
| `id` | uuid | 主キー |
| `project_id` | uuid | 対象案件 |
| `knowledge_node_id` | uuid nullable | canonical 案件情報 node |
| `question` | encrypted text | 質問本文 |
| `answer` | encrypted text nullable | 回答本文 |
| `normalized_question_hash` | text | 重複検出 |
| `status` | text | `unanswered`, `answered`, `stale`, `archived` |
| `review_state` | text | `candidate`, `accepted`, `rejected` |
| `confidence` | float | 抽出・回答の信頼度 |
| `asked_count` | int | 類似質問の回数 |
| `source_session_id` | uuid nullable | 元会話 |
| `source_message_ids` | json | 根拠メッセージ |
| `source_agent_run_ids` | json | 根拠 agent run |
| `source_tool_call_ids` | json | 根拠 tool call |
| `answer_source_refs` | json | Docs/file/URL/task/record table 参照 |
| `created_by` | uuid nullable | 作成者 |
| `updated_by` | uuid nullable | 更新者 |
| `created_by_agent` | bool | agent 生成か |
| `created_at` | datetime | 作成日時 |
| `updated_at` | datetime | 更新日時 |
| `last_asked_at` | datetime | 最後に聞かれた日時 |
| `deleted_at` | datetime nullable | soft delete |

暗号化については `ConversationMessage.content` と同じ機密保存方針に合わせる。Python 側は暗号化プロパティ、Next.js/Drizzle 側も同等の暗号化ヘルパー経由で読み書きする。

### UI

案件情報ページ内に `Q&A` block を置く。

- 初期テンプレートでは下部に `Q&A` を置く。
- ユーザーは質問・回答を inline で修正できる。
- `candidate` は控えめに表示し、採用・却下できる。
- `accepted` は通常の Q&A として表示する。
- Q&A block は案件情報スーパータグの固定 semantic block として必ず存在させる。
- ユーザーが表示位置を document flow 内で移動することは許可する。
- block placeholder を削除しても、保存時または読み込み時に復元する。
- Q&A entry 自体の削除・却下は `project_qa_entries.review_state/status` で管理し、本文編集で物理削除しない。

### 生成フロー

1. チャット保存後、`conversation_sessions.project_id` がある会話を対象にする。
2. ユーザー発話が案件情報への質問かを分類する。
3. その場の回答、参照した Docs、tool call、agent run を source として束ねる。
4. `normalized_question_hash` で既存 Q&A と照合する。
5. 既存なら `asked_count` と source を追加する。
6. 新規なら `candidate` として作成する。
7. 高信頼で、根拠が canonical Docs node または明示 source にあるものだけ `accepted` に昇格可能にする。

## エージェント接続

### 読み取り

`ContextBuilder` は canonical 案件情報 node を解決する。

入力順:

1. `projects.knowledge_node_id`
2. `knowledge_nodes.project_id + 案件情報 supertag`
3. `record_tables` の構造化表参照

プロンプトには次を入れる。

- canonical node title
- `body_text` の重要部分
- `accepted` Q&A の上位件数
- 本文内 reference card の target
- revision id / updated_at

### 更新

旧 `upsert_project_fact` 中心の tool は廃止し、Docs 正本を更新する tool に置き換える。

新規または置換する tool:

- `get_project_information_doc(project_id)`
- `patch_project_information_doc(project_id, patch, source_refs, update_reason)`
- `upsert_project_qa(project_id, question, answer, source_refs, review_state)`
- `list_project_qa(project_id, status, review_state)`
- `attach_project_information_reference(project_id, kind, target, title, caption)`

エージェント更新規則:

- 本文を正本として更新する。別枠の「AI要点」は作らない。
- 既存見出しを尊重し、必要な時だけ見出しを追加する。
- `進捗`、`課題管理`、`決定事項`、`要確認` などは本文構造として扱い、固定 DB category として押し付けない。
- 更新時は必ず revision と source_refs を残す。
- 根拠がない断定は `要確認` または Q&A `unanswered` に回す。

## 旧 DB との関係

旧 project information DB は Docs 正本と併存させない。現在の head では旧 API / agent tool / UI 参照を消し、Alembic migration で旧テーブルを drop する。

- `project_info_categories` / `project_documents` / `project_facts` / `project_info_sync_states` は旧案件情報メモ schema なので現行 schema から落とす。
- canonical Docs node がない案件は、API/tool の初回 ensure で `案件情報` Docs node を作る。
- record table は旧案件情報メモ schema ではなく、構造化一覧の正本として残す。本文へコピーせず、Docs から参照する。

ローカル DB では旧 facts/documents/sync states は空、categories は既定値のみだったため、drop による案件本文の消失はない。

## API 設計

`GET /api/projects/:id/information`

- canonical Docs node を resolve / ensure する。
- `node`, `supertag`, `field_values`, `qa_entries`, `reference_cards`, `legacy_snapshot` を返す。
- 旧 UI 用の categories/documents/facts は段階的に `legacy_snapshot` に移す。

`PATCH /api/projects/:id/information`

- canonical node の `body_text/body_json/title/field_values` を保存する。
- 既存 `/api/docs/nodes/:id` を内部利用してもよいが、案件情報としての permission と revision reason をここで付ける。

`GET /api/projects/:id/information/qa`

- Q&A を status/review_state で返す。

`POST/PATCH /api/projects/:id/information/qa`

- Q&A の追加、採用、却下、回答更新、source 追加を行う。

`POST /api/projects/:id/information/references`

- file/url/docs/record table/image/diagram 参照カードを `body_json` と attachments/edges に追加する。

## 実装順序

1. 旧 DB 実装を互換経路として残さず、Docs 正本と record table 参照へ一本化する。
2. `projects.knowledge_node_id` について、DB 既存列の有無を確認し、SQLAlchemy model / Drizzle schema / API serializer に復旧する。Alembic は存在確認つきにする。
3. `knowledge_nodes.body_text/body_json` と `knowledge_revisions.body_text/body_json` の暗号化、読み取り互換、検索 index 方針を実装する。
4. `案件情報` スーパータグと field 定義を seed する。
5. `project_qa_entries` を SQLAlchemy model / Drizzle schema / Alembic に追加する。
6. 既存データを確認し、旧 facts/documents がある場合だけ canonical Docs node へ backfill する。
7. `/api/projects/:id/information` を Docs-backed resolver に差し替える。旧 DB snapshot は返さない。
8. 案件情報タブを専用 `ProjectInformationDocumentEditor` に置き換える。既存 `DocsNodeEditor` の preview mode は持ち込まない。
9. reference card renderer/editor と、`project_qa_entries` を正本にする Q&A block renderer/editor を追加する。
10. `ContextBuilder` と project info tools を canonical Docs node 優先に変更する。
11. チャット後の非同期 Q&A candidate 生成 job を追加する。

## 検証

実装時に必要な確認:

- migration upgrade / downgrade
- 既存 project information API の fallback
- Docs node 作成、保存、revision 作成
- `案件情報` スーパータグ field 保存
- Q&A candidate 生成、採用、却下、重複質問の `asked_count` 更新
- agent context に canonical node と accepted Q&A が入ること
- frontend lint / build
- WebUI で、編集ボタンなし・プレビュー切替なし・本文直接編集・カード表示・Q&A block を確認

今回は設計書作成のみなので、mobile は対象外。release gate、APK build、upload、metadata 更新は不要。
