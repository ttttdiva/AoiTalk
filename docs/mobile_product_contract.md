# Mobile Product Contract / Conformance Gate

`contracts/product-contract.json` は、AoiTalk Native Mobile の完成条件を
機械判定するための唯一のデータ正本です。構造は
`contracts/product-contract.schema.json`、FastAPI の API 契約は
`contracts/openapi/fastapi.json` を正本とします。

## 責務分離

```text
FastAPI + Pydantic
       │ scripts/generate_openapi.py --canonical-only
       ▼
contracts/openapi/fastapi.json       (共有 OpenAPI 正本・生成物)
       ├─ frontend/openapi.json       (既存 Web 互換生成物)
       ├─ frontend/src/lib/api-types.gen.ts
       └─ mobile/src/types/api-types.gen.ts (Mobile 生成物)

contracts/product-contract.json      (P0 capability/navigation 正本)
       └─ scripts/validate_mobile_product_contract.py
```

OpenAPI の型は手書き DTO を正本にしません。`mobile/src/types/api.ts` などの
既存のドメイン型は段階的に generated type を利用しますが、API の wire contract
を新たに手動複製してはいけません。生成ファイルは直接編集せず、backend の
response/request model を変更して再生成します。

## Product Contract の内容

各 capability は次を必ず持ちます。

- `id`, `domain`, `criticality`（`p0` / `p1` / `p2`）
- `operations[].operation_id`（OpenAPI の `operationId`。raw URL/routeは禁止）
- `mobile.routes`, `mobile.entry_points`, `mobile.implementation`
- `roles`, `permissions`, `scope`（Project/Space/ACL の可視性）
- `offline`, `persistence`, `mutation`
- 実行可能な前提・操作・結果を表す `acceptance`

現行 P0 registry は auth/scope、canonical navigation、Chat search/session、
Memory、Task、Project visibility、Files、Docs nodes/scale sync を代表します。全 FastAPI
route の一覧を product contract に重複記載するのではなく、製品完成性を判定する critical
capability に限定します。

Apps runtime はこの registry の P0 capability です。Mobile は認証済みの FastAPI を
直接利用し、Release-bound UI、Action run、status/log/file を同じ Project scope で扱います。
宣言と実行履歴は読み取り用にcacheできますが、Action mutation はオンライン時だけ許可し、
不確実なPOSTを汎用 sync outbox へ追加しません。

Hugging Face ブラウザ/同期、Hydrus ブラウザ、Story/Novel Studio、TRPG は Native Mobile の
capability ではありません。正本はそれぞれ独立アプリ `60_huggingface-sync`、
`57_Hydrus_App`、`86_Novel-Graph-Studio` です。

## Navigation invariant

`navigation.tabs` はスマホの bottom tab に表示する **5 件**（Chat / Tasks /
Calendar / Files / Docs）だけです。Apps は route と capability を残し、Settings または
sidebar から到達できる hidden workspace として宣言します。

`navigation.routes` は `mobile/src/app` の screen file と一対一です。layout と test
fixture は screen ではありません。各 route は少なくとも一つの entry point を持ち、
各 capability は宣言済み route と entry point の組み合わせで到達可能でなければ
なりません。これにより orphan route、未到達 critical capability、unexpected tab を
検出できます。

## API transport invariant

P0 capability の API は直接 FastAPI (`fastapi`) または authoritative sync
(`sync`) の operationId を参照します。`next_bff` は P0 transport として禁止です。
Next.js 固有の画面補助 API を使う場合は、P0 registry に載せる前に FastAPI の
canonical operation を追加してください。`/api/conversations/search` はその例で、
OpenAPI には安定した `search_conversations` operationId を持ちます。

## 生成・検証

リポジトリルートで実行します。

```powershell
# FastAPI を起動せず共有 OpenAPI 正本だけを更新
venv\Scripts\python.exe scripts\generate_openapi.py --canonical-only

# Web 互換 artifact も更新する場合（既存 frontend typegen の入力）
venv\Scripts\python.exe scripts\generate_openapi.py

# Mobile 型（frontend の openapi-typescript を共有実行環境として利用）
cd mobile
npm run api:typegen
cd ..

# Product Contract / OpenAPI operationId / route / entry / scope / transport gate
venv\Scripts\python.exe scripts\validate_mobile_product_contract.py
```

validator は network/server を起動せず、結果をソートした deterministic error として
返します。`--skip-route-files` は contract 単体テストなど、Expo source tree が存在
しない環境だけで使用し、通常の CI では route check を省略しません。

変更時は capability の acceptance と対応する regression / integration / Android
scenario を同時に更新します。validator PASS は unit/実機 QA の代替ではなく、API・
navigation・scope の入口を失っていないことを保証する conformance gate です。


## Native Chat: compact navigation and immediate submission

The native conversation screen owns one safe-area-aware navigation header: Back,
project-name selector, character display-name selector and title editing. It must
not add a second screen-title/status toolbar, space-path prefix or character prefix.
Selector labels wrap naturally so long names remain readable at narrow widths and
larger font scales; project selection still uses the scoped project chooser and
character names resolve from the offline cache.

The composer represents the **next** turn's model and model-specific effort, not
an earlier effective fallback route or the server's unrelated global mode. An
explicit empty effort capability hides the effort selector. Model and effort are
snapshotted together in the durable dispatch payload and validated server-side.

Native submission has a single Send control. Pressing it immediately clears the
draft and inserts a stable-ID user bubble, before SQLite or network work finishes.
A following send interrupts the active response without an extra steer/stop choice.
Direct requests use abort signals; server requests serialize confirmed cancellation
before handoff. Late events from retired generations do not modify the new stream.
Group chat uses this same submission path. Session promotion changes route params
without replacing the screen, and loading transitions never remount its children.

Foreground chat writes and background synchronization share the SQLite write
coordinator. User insertion and session counts commit atomically; transient lock
retries yield the JS thread and reuse the same submission ID. Accepted failures
stay in history for retry rather than reappearing in the composer. Unsaved-bubble
retry persists the original identity before dispatch. Account/server scope is
retained with queued input and must match before sending.

## Agent-bound Chat and Operations scope

An Agent-bound conversation keeps the human account as its owner and stores an
optional canonical `agent_id` on the session. Selecting an Agent never changes
the Character presentation, participant rows or Project membership. The
catalogue is a server-owned safe projection of the selectable Agent identity
(`id`, display name, lifecycle state, latest revision and optional Character
link); revision instructions, provider settings and credentials are not mobile
selection data.

Mobile may read a live catalogue or use a previously scoped catalogue cache for
choices. Binding, clearing or switching the session Agent is an outbox mutation
and replays with the original session scope, `agent_id` and `expected_agent_id`.
An omitted `expected_agent_id` preserves legacy dispatch semantics; an explicit
`null` means that the session must still be unbound. The server performs the
human ACL, Agent lifecycle/revision, busy-run and compare-and-swap checks. A
stale or busy binding is rejected rather than silently replacing the selection.
A queued dispatch also carries the Agent identity observed when it was created,
so reconnect cannot send an offline message to a different Agent after a
binding change.

Agent conversation history and AgentRun/WorkItem status are Operations reads.
They are live, permission-checked bounded projections with no mobile
persistence; offline reads are forbidden and must not be represented as a
healthy empty list. The projections retain only safe identity, scope, state,
revision, work-item and run references needed by the Operations screen. Raw
prompts, private transcripts, tool arguments/results, provider data, leases and
credentials never cross this Mobile contract.

### AoiDots capability contract

`chat.aoidots` is the owner-scoped FastAPI capability for consultation and
delegation. Its generated operation IDs are
`bootstrap_api_aoidots_bootstrap_post`, `list_work_api_aoidots_work_get`,
`get_dialogue_api_agent_dialogues__dialogue_id__get`,
`answer_input_api_agent_dialogues__dialogue_id__inputs__input_id__answer_post`,
`control_api_agent_dialogues__dialogue_id__control_post`,
`stop_api_agent_dialogues__dialogue_id__stop_post`,
`resume_api_agent_dialogues__dialogue_id__resume_post`, and
`rework_api_agent_dialogues__dialogue_id__rework_post`. Mobile uses the direct
FastAPI transport for these operations; the project hint is context only and
the private root remains projectless and owned by the human account.

The wire projection is a strict nested DTO: `AoiDotsWorkItem` contains
`AgentDialogueRefs`, `AgentDialogueInputDTO`, and `ArtifactSafeRef` values.
The work projection's canonical human-facing fields are `title`, `label`,
`human_status`, and `allowed_actions`. Pending input projections expose the
validated `question`, `choices`, `input_type`, `allow_multiple`, and
`allow_free_text` fields alongside their stable identity and revisions.
Pending input answers carry a stable client message ID and the expected input
and control revisions. A stale compare-and-swap answer or control command is
rejected. The worker is represented by an ordinary linked Chat child session,
so consultation, delegation, pending input, rework/restart and normal Chat
history keep their own ownership and ACL boundaries.

The capability must support multiple AoiDots sessions at once. Refresh,
reconnect and parallel child work preserve dialogue, child-session, AgentRun,
WorkItem, delivery and artifact references per session; one dialogue must not
replace or mutate another dialogue or an unrelated ordinary Chat session.


## Native sidebar conversation controls

The sidebar provides one accessible five-icon row: Projects, Operations, Reports,
Apps and conversation search. Settings remains on the screen header gear. Search
is available from the Chat, Tasks and Files panels, ignores the currently selected
Project, and matches titles and message bodies across readable old/current history.
Successful online results merge local-only/unsent matches; offline search uses the
available device cache. Results are bounded to 50 and late responses cannot replace
a newer query.

Timeline and Project grouping share the same long-press menu for title editing,
pin/unpin, whole-branch fork, copy ID, deletion and local-session upload. Long press
must never also open the row; mutation buttons reject duplicate presses. Rename,
pin, delete and upload retain their existing server/offline rules and repository
notifications. Deleting the active session returns to Chat; promoting an active
local session replaces its route with the server ID.

A sidebar fork copies the entire current branch. Server-backed sources require a
fresh unbounded messages response without a delta cursor; a cached last message is
never a valid substitute after a failed full read. The parent chain must be complete.
Genuine unsynced inputs are retained in a local copy after reconciliation with that
full snapshot, without dispatching them. Confirmed deliveries from inactive
branches are excluded. On a local fork's first Server send, copied history is
imported before the new pending input is dispatched; the pending input itself
is not imported as already accepted history. Edited titles survive upload.
Local fork history containing system rows or attachments is retained on-device
with an explicit unsupported-import error rather than silently omitted on upload.
Empty sessions and unsupported local Group/Agent/App contexts explain why they
cannot be forked. Failed local copies are
removed rather than presented as a completed fork.
