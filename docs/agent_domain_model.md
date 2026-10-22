# Agent domain model

**Scope:** WS01 identity/authority foundation, the WS02--WS06 hand-off
contract, and the durable Agent binding shared by Chat and Operations.  This
is a source-of-truth map, not a promise that every future runtime component is
enabled in every profile.

Current product names and execution boundaries are defined in
[feature terminology](feature_terminology.md). Agent organization and shared
Execution Team configuration are distinct graphs.

The Japanese section dated 2026-10-11 is the current decision record for the
Chat/Operations integration.  It defines the boundary implemented by the
additive schema and transport call sites; deployment/runtime evidence remains
separate and is recorded with its actual status.

## Reading this document

The following labels are used throughout the WS06 documentation set:

* **Verified (source/tests)** means that the named implementation and focused
  tests exist in this checkout.
* **Verified (local runtime)** means that a supported local process was
  exercised and the evidence is named.  A type-check or unit test alone is not
  a runtime observation.
* **Deferred** means the relation or runtime is intentionally owned by a later
  workstream and is not present in the current WS01 foundation.
* **Unverified** means that a production database, provider, multi-process
  deployment, or external service was not exercised.

## Canonical graph

An installation has one organization settings row.  `Space` remains the
existing department/division/business-area boundary; it is not replaced with
an agent-specific company hierarchy.

```text
Organization (one row per installation)
  |
  +-- Space (existing organizational boundary)
        +-- Project
              +-- Task

Agent (durable worker identity)
  +-- AgentRevision (immutable, 1:N)
  +-- AgentOrganizationProfile (optional 1:1 employment extension)
  +-- AgentSpaceAssignment ------> Space
  +-- AgentProjectGrant ----------> Project
  +-- AgentTaskAssignment --------> Task
  +-- PersonaOperatorAssignment --> Media Persona
  +-- optional presentation link -> conversation Character

AgentWorkItem (WS02 durable work unit)
  +-- AgentRun (1:N immutable attempts; exact AgentRevision pin)

ConversationSession (human-owned Chat history)
  +-- nullable canonical Agent binding (one Agent per session)
  +-- AgentExecutionBinding (immutable selection/routing snapshot)
  +-- AgentWorkItem (interactive bookkeeping link, nullable)
  +-- AgentRun (one turn; exact AgentRevision pin)
```

There is deliberately no `CompanyDepartment`, no company-per-Space row, and
no requirement to add `organization_id` to every existing table.  A generic
Agent can operate internal Tasks, MediaOps, code-agent work, or another App
through explicit assignments; the identity does not imply any one domain.

## Entities and authority meaning

| Entity | Meaning | What it does **not** grant |
| --- | --- | --- |
| `User` | Authenticated human account; the grantor/approver for human-only commands | It is not an Agent execution identity |
| `Organization` | Deployment singleton settings, autonomy and budget policy | It is not a tenant selector or a per-Space company |
| `Space` | Existing organizational grouping above Projects | Membership alone is not Project write access |
| `Agent` | Stable UUID worker identity and lifecycle (`draft`, `active`, `paused`, `retired`) | It is not a synthetic User and has no implicit ACL |
| `AgentRevision` | Immutable job definition: mission, Execution Team/Profile identifiers, bounded capability ceiling and policies | Text, model choice, or an Execution Team ID is not an authority grant |
| `Character` | Conversation character definition: presentation/voice and prompt/model/tool execution; the Agent link uses it for presentation | Prompt, tools, or model fields cannot grant Agent authority |
| `ConversationSession` | Human-owned Chat history and its optional canonical Agent selection | It does not turn `user_id` into an Agent, share private transcripts, or permit a group-chat Agent binding |
| `AgentExecutionBinding` | Immutable server-owned snapshot of the selected AgentRevision, routing pins, content hash, and snapshot hash for a session/turn | It is not a mutable permission grant and does not replace current authority checks |
| `Persona` (Media Persona) | MediaOps public/brand identity; separate from conversation Character | A Persona assignment does not bypass its owner or Project ACL |
| `AgentOrganizationProfile` | Optional job title, manager, employment and company ceiling | It does not create another department hierarchy |
| `AgentSpaceAssignment` | Explicit Agent membership in an existing Space | It does not create a Project grant |
| `AgentProjectGrant` | Explicit Agent Project ACL using the repository permission vocabulary | It is separate from `ProjectMember`; Agent IDs never enter `ProjectMember.user_id` |
| `AgentTaskAssignment` | Explicit Agent-to-Task role (`owner`, `executor`, `reviewer`, `observer`) | It is separate from `TaskAssignee.user_id` |
| `PersonaOperatorAssignment` | Explicit Agent-to-Persona role and optional capability ceiling | It does not approve or publish by itself |
| `AgentWorkItem` | Durable work unit with queue, lease, retry and settlement state | It is not an individual execution attempt |
| `AgentRun` | One durable execution attempt and bounded evidence snapshot | It is not a queue item; `work_item_id` links each attempt to its aggregate WorkItem |
| `ExternalAction` | Existing proposal/approval/attempt/receipt side-effect boundary | An Agent cannot approve its own proposal |

## Organization singleton

`organizations.singleton_key` is fixed to `installation`, with both a unique
constraint and a check constraint.  `OrganizationService.bootstrap()` performs
a transaction-safe get-or-create and retries the uniqueness race, so concurrent
first use converges on one row.  Policy and budget JSON are bounded at the
service/API boundary and are projected without secret-shaped keys.  The
organization row starts with `autonomy_level=disabled`.

The application composition root invokes bootstrap during WebChatServer
lifespan startup.  In a required database/Enterprise profile, inability to
bootstrap is a startup failure; ordinary profiles retain their existing
fallback behavior.  A deployment must still verify this against its PostgreSQL
configuration before calling it production-ready.

## Agent and revision lifecycle

Agent creation is idempotent by the repository create-content hash plus a
caller-supplied idempotency key.  The row has an optional FK to
`characters.id` for presentation only and an optional human creator FK.  No
lease, scheduler, prompt, provider credential, or host path is stored on the
stable identity.

`AgentRevision` is immutable in the migration (PostgreSQL trigger; SQLite
update/delete triggers), monotonically versioned per Agent, and unique by
`(agent_id, version)` and `(agent_id, idempotency_key)`.  It stores bounded
mission/instructions, canonical Execution Team and Execution Profile identifiers,
allowed Subagent identifiers, capability ceiling, wake/budget/concurrency
policies, and a SHA-256 content hash.  An exact idempotent replay returns the
existing revision; a conflicting replay is rejected.  Future execution must
pin the exact revision rather than silently following “latest”.

Agent state transitions and revision creation are human-admin operations.
`retired` is terminal for new revisions and execution; `paused`/inactive
identities are denied by the authority resolver.  Deactivation does not erase
history.

`Agent.state` and `AgentOrganizationProfile.employment_state` remain separate.
The former is the execution lifecycle (`draft`, `active`, `paused`, `retired`);
the latter records employment (`active`, `on_leave`, `suspended`, `terminated`,
`contractor`). A naming change must not merge or derive one from the other.

## Explicit relationships

All assignment rows have bounded role/state values, optional active windows,
human grantor FKs (`RESTRICT` on deletion), active-equivalent uniqueness, and
lookup indexes.  Revocation is represented as state/history rather than a
silent hard delete.

* A Space assignment may carry a bounded `allowed_capabilities` ceiling, but
  it can never substitute for an Agent Project grant.
* A Project grant uses normalized `read`/`write` permissions and is required
  for Project/Docs/Story mutation.  The target Project must match the supplied
  Space, when both are supplied.
* A Task assignment identifies who may execute/review a Task; existing Task
  ACLs remain authoritative.
* A Persona operator assignment is required for Agent Media work.  For a
  project-bound Persona, authority resolution derives that Project and still
  requires a Project grant and Space assignment.
* The organization profile currently enforces **at most one** manager (`user`
  and `agent` cannot both be set); both may be null.  This reflects the current
  database check and is intentionally called out rather than overstating an
  exactly-one XOR guarantee.

## AgentRun and ExternalAction compatibility

Existing human/conversation AgentRuns remain valid: new identity columns are
nullable and do not change the historical `user_id` meaning.  The additive
columns are `agent_id`, `agent_revision_id`, `task_id`,
`acting_subagent_id`, `previous_attempt_run_id`, a bounded
`resolved_execution_manifest`, and its hash.  A revision requires an Agent;
manifest rows require a 64-character hash.  Child runs inherit typed identity
and the bounded manifest from their parent.  Safe DTOs expose IDs and metadata
only; credential, provider, model, environment and path-shaped keys are
removed.

`ExternalAction` may link nullable `origin_agent_id`, `origin_agent_run_id`
and `origin_work_item_id` with `SET NULL` deletion behavior.  Existing action
version/payload-hash, human approval, immutable Attempt/Receipt, and uncertain
reconciliation contracts remain the source of truth.

## Current API surface

The additive router is registered after AgentRun routes and requires the normal
cookie-authenticated user.  Mutations derive a real admin human on the server;
request JSON cannot name an actor.

| Surface | Endpoints (all under `/api`) | Gate |
| --- | --- | --- |
| Organization | `GET/PATCH/PUT /organization` | Authenticated; update is admin-human |
| Identity | `POST/GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}/state` | Authenticated; mutations admin-human |
| Revisions | `POST/GET /agents/{id}/revisions`, `GET /agent-revisions/{id}` | Create admin-human; reads authenticated |
| Company profile | `GET/PATCH /agents/{id}/organization-profile` | `virtual_company`; disabled returns 404 |
| Assignments | Space, Project, Task and Persona operator create/list/revoke/state paths | `virtual_company`; mutations admin-human |
| Authority | `GET /agents/{id}/authority` and `/effective-authority` | Admin-human; safe decision projection |

Request models are strict/bounded.  Idempotency keys may be supplied in the
body or `Idempotency-Key` header; a mismatch is a conflict.  Responses use
safe DTO projections rather than serializing arbitrary ORM objects.

## Persistence and migration

Migration `alembic/versions/20260903_0001_agent_identity_foundation.py`
creates the identity graph and adds nullable AgentRun/ExternalAction links.
`20260903_0002_agent_work_runtime.py` adds the generic WorkItem/Event tables,
`AgentRun.work_item_id`/`work_item_attempt` and
`ExternalAction.origin_work_item_id`
links.  Both use native PostgreSQL operations and SQLite batch recreation with
foreign-key handling for legacy parent/child rows.  The migrations preserve
existing rows, keep historical human paths nullable, define deliberate
`SET NULL`/`CASCADE` and grantor `RESTRICT` behavior, and install revision/
event immutability guards.

`20261011_0002_conversation_agent_binding.py` adds nullable
`ConversationSession.agent_id`/`agent_binding_updated_at`, nullable
`AgentWorkItem.conversation_session_id`, the immutable
`conversation_agent_bindings` snapshot table, content/snapshot hash checks,
and SQLite/PostgreSQL update guards.  Native Chat creation and fork paths keep
owner/participant/session/binding changes in one transaction; forked sessions
receive a new binding row and hash rather than copying a provider continuation.
The migration is additive and keeps legacy Character-only rows and human-only
AgentRuns valid.

The repository's OpenAPI artifact and Web/Mobile generated types include these
routes and fields.  Confirm the Alembic graph has one head before deployment;
PostgreSQL upgrade/rollback and multi-process contention still require a
deployment smoke (see the acceptance matrix).

## Evidence and remaining boundaries

**Verified (source/tests):** model and migration definitions above,
idempotent Organization/Agent/Revision service behavior, safe AgentRun
manifest redaction, ActorPrincipal construction, route schemas, and SQLite
identity migration smoke are covered by `tests/test_agent_identity_foundation.py`
and the focused AgentRun/operations suites.

**Implemented additively:** WS02 adds `AgentWorkItem`/`AgentWorkEvent`, the
`AgentRun.work_item_id` and `ExternalAction.origin_work_item_id` links, and a
common `WorkSource`/`ExecutionAdapter`/`AgentWorkCoordinator` service.  These
rows are inert unless the effective runtime gate permits startup; the WS01
identity migration intentionally has no `work_item_id` column of its own and
does not start a worker.

**Implemented additively:** application startup registers the common
coordinator and feature-gated WorkSources/adapters after recovery; cross-process
PostgreSQL contention and provider-specific production execution remain
deployment verification gates.

**Unverified:** PostgreSQL migration under production extensions, multiple
process claim/lease contention, real provider calls, and external credential
verification.  These must not be reported as PASS merely because the local
SQLite/unit checks pass.

## 2026-10-11 Chat/Operations 同一永続 Agent 統合決定

この節は、Chat の会話画面と Operations の自律実行を同じ永続 Agent
identity、WorkItem、Run、設定、権限判定へ接続するための恒久契約である。
実装がまだ作業ツリーにしか存在しない項目は、下の evidence 表で
`実装確認`、`境界外`、`不一致` を分けて記録する。文書上の決定を、未実施の
テスト、migration、実ブラウザ、PostgreSQL実行の完了とは解釈しない。

### identity と表示の境界

`ConversationSession.user_id` は認証済み人間の所有者・操作主体として残す。
`ConversationSession.agent_id` は nullable な Agent FK であり、同じ行に人間を
Agentとして複製しない。Agentを選んでも `user_id`、既存の
`ConversationParticipant`、ProjectMember、TaskAssignee は書き換えない。

通常のCharacter-only sessionは `agent_id=NULL` のまま従来どおり動く。
`character_name` と Character/participant は表示・会話演出の互換境界であり、
そこからAgentを推測しない。`is_group_chat=true` のsessionは canonical Agentを
bindできない。グループ参加者に `participant_type=agent` が残っていても、それは
旧グループ表示・handoff情報であり、session全体の実行identity、権限、cross-session
memoryを与えない。

Character presentation、Media Persona、Execution Team/Subagent は別の責務である。
Characterのprompt/model/tool欄、Personaの公開identity、Team/Profile/Subagentの
routing設定は、AgentのACLやHumanの承認を生成しない。

### session bind の権限とCAS

Agent選択は常にserver-sideで行う。作成時の任意 `agent_id` と、既存sessionの
`PATCH /api/conversations/{session_id}/agent` を同じ判定に通し、次を満たさない
要求は拒否する。

1. 操作者がsessionのownerまたは明示されたconversation adminであり、対象Project
   がある場合はHumanのProject ACLを持つ。
2. 選択対象Agentが `active` で、最新の `AgentRevision` が存在する。Agent作成者や
   adminによる選択の例外があっても、session・ProjectのHuman ACLを省略しない。
3. Agent側のProject grantなど、後続のauthority resolverが必要とする関係を
   serverが検査できる。request bodyのrevision、Team、Profile、instructionsは
   authority入力として受け取らない。
4. `expected_agent_id` が現在の `ConversationSession.agent_id` と一致する。古い
   readを使ったPATCHは `agent_binding_stale` として失敗し、勝手に上書きしない。
5. sessionに `queued`、`running`、`awaiting_approval`、`uncertain` のAgentRunが
   ある間は、bind、unbind、別Agentへの切替を `agent_binding_busy` で拒否する。

bind成功時は `agent_binding_updated_at` を更新し、選択したAgentとその時点の
latest revisionから、変更不能な `AgentExecutionBinding` を保存する。この行は
選択履歴と安全な実行入力の再構成用であり、`revision_content_hash` と
`snapshot_hash`で改ざん検出する。current ACLの代替ではない。削除・
revocationは履歴を消さず、次のturnと次のmutationで再判定する。

### revision と routing のpin

bind時にlatest revisionを選択するが、実行中のturnが「常にlatest」を参照し続ける
ことは許さない。各turnのadmission/start時に、Agentがactiveであることと最新revision
をserverが再読出し、そのrevisionをそのturnの `AgentRun` と `AgentWorkItem` に
exact pinする。turn開始後に作られたrevision、Team/Profile変更、許可Subagent変更は
実行中turnへ遡及せず、次turnから適用する。

`AgentRoutingPins` と `AgentModelRouting` は、revisionの
`agent_team_id`、`execution_profile_id`、`allowed_subagent_ids` を入力にして、
Team/Profile/allowed Subagentsとprovider routeをserver-owned snapshotへ解決する。
clientのrouteやpromptをsnapshotへ混ぜず、snapshotは権限を増やさない。Runの
`resolved_execution_manifest` / hashと安全なroute snapshotは監査用証拠であり、
mutation直前のauthority判定を省略するbearer tokenではない。

### Human ACL と Agent authority の交差

Chatの読み書き、添付、Project/Docs/Filesの操作にはHumanのsession/Project ACLを
先に適用する。その上で、Agentのactive状態、exact revision、Organization policy、
Space assignment、Project grant、Task/Persona assignment、capability ceiling、
ToolPolicy、runtime gateを `AgentAuthorityResolver` で交差させる。許可集合は次の
ように狭めるだけである。

```text
Human session/Project ACL
  ∩ current AgentAuthorityResolver decision
  ∩ exact AgentRevision/Team/Profile/Subagent pins
  ∩ tool/harness/resource scope
  ∩ ExternalAction approval boundary
```

各tool call、Project/Docs/Files/Media mutation、approval boundary、settlement前に
current Agentとgrantを再検証する。Agentのdeactivate、revision不一致、grant revoke、
Project/Taskのarchive、budget・runtime policy変更は、古いsnapshotを理由に継続せず
deny/blocked/uncertainへ遷移させる。AgentはHumanの代替Userにならず、Agent identity
だけで別sessionのHuman ACLを越えない。

### ChatのWorkItem/RunとOperations ledger

Chat turnも `AgentWorkItem` と `AgentRun` の共通ledgerへ記録する。
`AgentWorkItem.conversation_session_id` は nullable FKであり、通常のTask/Media/App
workは従来どおりNULLを取る。Chat行は原則 `source_type=chat_turn`、
`execution_adapter=interactive_chat`、`intent_key=interactive_chat` とし、
`AgentRun.work_item_id` と `AgentWorkItem.active_agent_run_id` を同じturnへ結ぶ。
WorkItem/Eventにはraw prompt、private transcript、credential、provider payload、
host pathを保存しない。

`ConversationDispatchOutbox` はメッセージ/runのtransport deliveryだけを担う。
outboxは汎用Work queueでもChatの再実行権限でもない。interactive transportが
admit → exact claim/start → renew → settleを行い、client message/run identityの
idempotencyを同じturnへ再結合する。autonomous `AgentWorkCoordinator` は
interactive Chat行をpollしてblind retry/executeせず、background restartでChatを
二重実行しない。Chatの通常テキストlaneはbackground autonomous runtime gateがOFF
でも利用できるが、background adapterの自律実行を有効にする決定とは別である。

### 並列実行と共有資源のfence

同一Agentの別sessionは、各turnがpinしたrevisionの
`concurrency_policy.max_parallel_runs` / `max_parallel_tasks` とglobal coordinator
上限の内側で並列に進める。同一sessionはinteractive laneを一度に1件だけ受け付け、
`conversation:<session_id>` をconcurrency/resource fenceとして使う。binding変更中
のrun、同一sessionのassistant生成、同一WorkItemのleaseを、別workerが横取りして
settleしてはならない。

同じAgentは複数の独立した `ConversationSession` にbindできる。sessionごとの
history、context、memory、provider/terminal continuation、Run/Work leaseは分離し、
同じAgentであることだけを理由に一方のsessionを他方へ表示・実行権限・暗黙の
recallとして共有しない。

Terminal process/provider continuation、history、context snapshot、session memory、
run leaseはsession/turnごとに分離する。プロセスglobalな「現在のCharacter」や
provider handleを使って別sessionへ状態を漏らさない。別sessionの並列性を認めても、
Agentのrevision concurrency policy、Human ACL、resource scopeを共有許可へ昇格しない。

### memory、work、artifactの参照境界

Agentがbindされていることだけを理由にCharacter-based implicit cross-session recallを
有効にしない。turnのcontextは現在sessionと、そのturnで明示されたProject/Task/
memory scopeだけから作る。別sessionのraw private transcriptをAgent、Subagent、別の
Humanへ共有しない。既存の会話・事実検索経路は、候補検索後と原本公開前にHuman ACL、
session/project scope、active/expiryを再確認し、検索語や本文をauthority errorへ漏らさない。

Agent work、workspace、生成artifact、terminal result、Operations activityは、
それぞれのpermission-checked scopeから安全なprojectionだけを参照する。
WorkItem/Runの存在やAgentの同一性は、private transcriptの閲覧権を意味しない。
Agent-linked memoryを追加する場合も、source session、scope、retention、redaction、
forget/supersedeの判定を記録し、Agent IDを全履歴の共有キーにしない。

ChatからOperationsの共有work evidenceを参照する場合は、利用者が明示的に選択した
WorkItem、Run、artifact、またはOperations activity linkをserverへ渡し、現在のHuman
session/Project/Task/Docs/artifact ACLとretentionを再検証する。画面上のlinkは参照
関係を示すだけで、別scopeへの権限やAgent authorityを付与しない。Chatはcanonical
ledgerを再読出しし、同じ `work_item_id` / `agent_run_id` / artifact IDを表示用の
bounded projectionとして返す。Chat本文へOperationsのraw event、prompt、provider
response、別session transcriptを自動注入しない。

共有projectionの許可フィールドは、実装したhelperのclosed schemaを正本にする。
最低限、許可済みscope内のAgent/Revision/WorkItem/Run/artifactのID、domain/sourceの
closed code、state/outcome、created/started/updated/completedの時刻、attempt数、
boundedなresource/evidence referenceだけを返す。現行helperはresult summary本文を
返さず、resource referenceのclosed fieldはID、kind/type、uri/pathのbounded値に限る。
uri/pathは参照表示であり、filesystem scopeやprovider authorityを与えない。
credential、token、lease、provider/model/base URL、raw result、event text、prompt、private
transcript、連絡先や秘密形状のmetadataは常に除外する。利用できないoptional ledgerは
空配列と混同せず、`unavailable` として扱う。

現行のOperations観測実装には `work_metadata` / `causal_activity` のclosed ID・
state・timestamp projectionがあり、Chat/Agent turnでは
`AgentRunService.list_agent_activity` と `src/tools/agent_activity.py` が同じ
redaction・scope・unavailable semanticsを再利用する。B ledger/body-free ACL回帰と
HTTP/private projectionを実装確認済みとし、browser/UI linkの実環境evidenceは下表に分けて記録する。

### Native Chat と Operations の相互参照

Native Chatのassistant messageは、この相互参照に `metadata.agent_run_id` を用いる。
画面は同一sessionの直近64件までの異なるRun IDを、ACL付きのbounded Run DTOとして
hydrateする。`relatedWork` のボタンは、DTOのRun IDがmetadataの値と完全一致し、
messageとRunのsession ID、Agent ID、`work_item_id` がそれぞれ検証できた場合だけ表示する。
欠落・stale・取得失敗・session/Agent不一致・Workなしは、推測したリンクを作らず非表示にする。

OperationsのWorkからChatへ戻る識別子は、`canonical_ref` または `source_ref` の
typed `{kind, id}` だけを使う。Chatへの遷移は `kind=conversation` に限り、scalarの
session ID、URL/path、任意metadataからconversationを推測しない。両方のtyped refが
存在して異なる場合はfail-closedとする。Operations overviewは既存のbounded
`current_work`（取得上限100件）から `succeeded` / `cancelled` の最近のWorkも表示するが、
全履歴や全体件数を表すものではない。

NativeのOperations read（overviewとWork execution）はローカル30秒のtimeoutを使い、
timeout、Gateway再認証、401/403、接続失敗、その他の応答失敗を個別の安全な説明へ分類する。
scope変更、server fingerprint変更、画面離脱、Abort後のstale responseはrevision・active・
abort guardで破棄する。これはCharacterのpresentation境界をAgent identityへ昇格させない。

### AgentScopedMemoryの境界

Agentのmemory toolは既存 `ScopedMemoryService` と既存memory tool群を再利用する。
候補のSQL scope prefilterを先に適用し、その後にembedding、lexical、relevanceの
順位付けを行う。許可される候補は現在のProject、明示されたTask、現在の
ConversationSessionだけであり、personal/global、暗黙のcross-session、foreign
Project/Task/Sessionは候補に入れる前に拒否する。検索後の原本再読込でも同じscopeと
Human ACLを確認し、順位やAgent IDでscopeを広げない。

memoryのread/writeは既存の `project_read` / `project_write` capabilityと
`AgentAuthorityResolver` の交差を使う。永続行のHumanActorは人間のまま保持し、
Agent ID、exact AgentRevision、AgentRun/WorkItem provenanceを別フィールドで残す。
AgentをUserへ置換したり、memory rowの所有HumanをAgentへ書き換えたりしない。
scope解決不能、外部scope、current session不一致、DB/embedding/lexical service障害は
fail-closedとし、別session本文やraw promptをAgent contextへ混ぜない。

将来のAoi系coordinationや新しいAppがこの関係を利用するときも、既存の `Agent`、
`ConversationSession`、`AgentWorkItem`、`AgentRun` をtyped IDで参照し、同じ
Human/session/Project ACL、exact AgentRevision、lease、artifact scopeを通す。
coordination専用のAgent identity、Chat transcript ledger、Work queue、shadow
Operations rowを追加して関係を二重化しない。外部coordinationへの通知はbounded
event/linkであり、受信側がAgent authorityを自動継承しない。Character presentation、
Media Persona、Execution Team/Subagentの役割分離もこの拡張で変えない。

### 互換性、migration、sync

この統合は既存データを破壊しないadditive migrationとして導入する。

* `conversation_sessions.agent_id`、`agent_binding_updated_at`、
  `agent_execution_bindings`、`agent_work_items.conversation_session_id` はnullable/
  additiveで追加し、既存Character-only session、既存human AgentRun、既存group
  participantをbackfillでAgentへ推測変換しない。
* Agent FKはsession削除時の履歴保持を優先して `SET NULL` とし、binding snapshotは
  session削除時にCASCADEする。WorkItemのsource/linkはcanonical sourceを複製せず、
  sourceが消えた場合はrecoveryでblocked/terminalを決める。
* migration中は旧readerがNULLを扱え、新readerがbinding table/Work FK不在を成功扱い
  せずfeature/version gateで停止できることを確認する。SQLite batch recreationと
  PostgreSQL FK/index/lock semanticsを別々に検証する。
* Web/Mobile syncにはAgent選択値と`agent_binding_updated_at`をsafe projectionとして
 返す。binding更新でChatのrecency (`last_activity`) を書き換えない。generated
  OpenAPI/typesはserver contractから再生成し、手編集しない。
  DBのnaive UTC時刻はAPIでUTCの`Z`表記へ正規化し、`agent_binding_updated_at`を
  binding変更のeffective mutation clockとして使う。古いsnapshotの作成時刻や
  `last_activity`を同期判定に流用しない。

Agent-bound Chatではprovider/model/Team/Profileの選択をAgent管理のserver-owned
controlsへ寄せる。Character-direct Chatはlegacy presentation/provider pathとして
維持する。Mobileのbind/unbindはnormal server APIのCASとcanonical refreshを通し、
offline queueのdirect fallbackやblind retryをしない。queued messageは1件ずつ
cancelまたはdraftへ移せるが、accepted/uncertain workを消さず、legacy unknown
metadataは保持し、次の送信には新しいclient message IDを割り当てる。

native初回作成は stable client UUIDを持つ `PUT /client/{client_session_id}` の
aggregateとして、initial user message、greeting、owner participant、Agent binding、
session rowを一つのtransactionでcommit/replayする。初回のlegacy POST fallbackは
拒否する。provider failureはfriendly UI markerとtruthfulなRun/Work failedを残し、
verifierを再queryして成功に見せない。SDKのbounded retry契約は既存規約を維持する。
Human write ACLはprovider startと同じtransactionで再確認し、old human Runのreplayが
別Agentへretargetできないようにする。

### 実装確認と残るevidence

| 境界 | 作業ツリーで確認した入口 | 状態 |
| --- | --- | --- |
| nullable session Agent、immutable binding snapshot、content/snapshot hash、safe projection | `src/memory/models/conversations.py`、`src/services/conversation_agent_service.py`、`tests/test_conversation_agent_service.py` | 実装＋focused回帰確認（hash tamper、UTC Z、fork、same-revision distinct pinを含む） |
| server ACL、active/latest revision選択、CAS、busy-run拒否、owner/participant | `ConversationAgentService`、`PATCH /api/conversations/{session_id}/agent`、`GET /api/conversation-agents` | 実装＋focused回帰確認。PostgreSQL row-lock raceは別runtime gate |
| WorkItem→Chat session nullable link、same-session fence、Agent concurrency limit | `src/memory/models/agent_work.py`、`src/services/agent_work_runtime.py`、`tests/test_agent_work_chat_ledger.py` | focused ledger/runtime PASS。full multi-process contentionは別の未実施gate |
| interactive admission/start/renew/settle/cancelとautonomous poll除外 | `AgentRunService._ensure_interactive_work_in_session`、`chat_execution_adapter.py`、`claim_many` | source接続＋B ledger PASS。stop cancellationでRun/Workともcancelled、partial `user_stop` 1件PASS |
| Chat text laneがbackground runtime gate OFFでも動く | `AgentWorkCoordinator.interactive_enabled`、`tests/test_agent_work_chat_ledger.py` | 実装＋focused回帰確認。background autonomous gateとinteractive gateは分離 |
| additive migration、Alembic one-head、legacy/group compatibility | `alembic/versions/20261011_0002_conversation_agent_binding.py`、SQLite batch/guards | dedicated PostgreSQL up/down/up（0002再適用）、legacy FK、immutable guard、SET NULL/CASCADEを実検証PASS |
| Human ACL ∩ AgentAuthorityResolverのtool/mutation revalidation | `src/services/agent_authority_guard.py`、`src/tools/agent_activity.py`、unified/direct tool call sites | per-call guard、resource Project/Task/Docs/File/Media/Artifact scope、fail-closed回帰、final runtime guardを確認。full multi-processだけ別gate |
| Agent activity HTTP/private projection | `/api/agents/{agent_id}/agent-runs`、`AgentRunService.list_agent_activity`、`employee-chat-activity` | session ACL＋current Project read ACL、bounded DTO、raw body/event/tool args除外を実装確認。Astra GUIの初回flow/catalog/gear/activity表示PASS |
| Web/Mobile Agent selection/activity/API/cache、複数Chat並列、terminal/provider/history分離 | Web/Mobile source/tests、`chat_execution_adapter.py`、session-scoped Terminal locks | backend parallel/provider/history isolation、snapshot/replay/CAS、Astra UI same-Agent/reload/filter/roundtrip/dispatchをfinal確認 |
| Client transport/replay/provider failure | Web/Mobile Agent controls、`PUT /client/{client_session_id}`、offline queue、Run/Work failure projection | atomic replay、canonical CAS refresh、queued message cancel/draft、truthful Run/Work failureをfinal evidence PASS。Human unbound Gate ON/OFF fake QAもPASS |
| Agent-linked memory/artifact scope | `src/tools/agent_activity.py`、`docs/memory_search.md`、authority/artifact projection | B shared activity helperとACL/body-free ledger 7件PASS。raw別session transcript/prompt/event textはprojection/contextへ入らない |
| AgentScopedMemory | `src/services/scoped_memory_service.py`、`src/tools/memory/scoped_memory_tools.py`、`src/services/agent_authority_guard.py`、`tests/test_agent_scoped_memory_tools.py` | Project/Task/currentSession prefilter、embedding/lexical/relevance境界、HumanActor＋Agent/Revision/Run provenance、project_read/write、foreign/global/cross-session拒否を実装確認 |
| Native/provider constraints | `src/services/agent_authority_guard.py`、`src/llm/cli_native_capabilities.py`、`tests/test_cli_agent_native_guard.py` | finite native capability map、unsafe Codex pre-process拒否、Claude tool-free fallback、legacy unbound CLI/Operations scopeをsource/test確認。real LLM/native provider/native CLIは未実施 |
| Native Chat↔Operations相互参照 | `mobile/src/features/conversation/useConversationController.ts`、`mobile/src/lib/operations-api.ts`、`7fdb12a7d228698ae60c832bbb3f999da013147c`、`d9fffd6f5db93358206c850405a81d8c37187627`、`9f5fd306f9cbcb7cd3aecf1dad2baee27b5afad3` | metadata Runの直近64件hydrate、exact session/Agent/Work検証、typed ref conflict fail-closed、完了・キャンセルrecent Work、30秒read分類とstale/abort guardをsource/focused testで確認。v256 Native/5560の最終QAは下記validation noteの範囲でPASS。Cancelled detailもexact Work/Run参照でPASS |
| Operations read/projection | `3a3bf1a63e4fd426a9e774e2f1ae6bf90318b72d`、`src/services/operations_command_center.py`、`tests/test_operations_employee_observability.py` | `_run_summary`を明示closed scalarへ限定した軽量lifecycle projection（privacy semanticsは変更なし）。37 tests・独立review・Project/Space APIのschema/ACL/count parity、v256 Project success・完了/取消card→exact Cancelled detail・空Space・compact headerをPASS。GUI成功は27.14秒までの観測で、連続完了測定とは区別する |
| External Portal 5580 | v255のimage/multipart/SAF historical evidence＋`artifacts/releases/mobile/v0.1.256/aoitalk-mobile.apk.manifest.json` のv256 smoke | v255の新規PNG upload/thumbnail/viewer/SAF download（1,172,467 bytes・同一SHA）とmultipart code不変を資格付けとし、v256は保持PNG単体DELETE→UI消失＋独立404、再一覧/outbox 0、WSS/JWT/Gateway header、unlink/cancel/real reauth/Docs 200一回/再起動保持/own Grant revoke、fresh Docs personal/project HTTP200・sync terminal/outbox 0・force-stop/restart後HTTP200/outbox 0/noCredentialInput/promoted、最終ownGrant cleanup/5580 stopをPASS |
| 共通Operations/dispatch回帰 | B ledger、Operations focused suite、C dispatch suite | focused suites、final failure outcome、dispatch回帰はPASS。real provider/full multinodeは別gate |
| Browser UI | Chat Agent selector、Operations Agent activity、Work/Chat links、reload/filter/group/legacy labels | Astra独立GUI final PASS（HEAD `164ae514`、Next `15940`、snapshot `1595`/SHA0）。same-Agent two-session/reload/global-filter/stale-409/group-disabled/legacy-Character、Agent gear、Work→Chat canonical roundtrip、actual UI dispatch、source-fix後のconsole/API/private-activity checksを確認。v256 Native/5560、Portal 5580は実機PASS |

v256 Native validation note: 正式manifest `artifacts/releases/mobile/v0.1.256/aoitalk-mobile.apk.manifest.json` を
機械検証し、source/fixed revision `f8a51a4beb6b81fccbd84cd1b1a428da91a024ca`、APK SHA-256
`72031ccbf8092124a6bdaa6f0e42603cfab86c8ff51735b141e3984abd8aff55`、62,730,112 bytes（62730112）、versionCode 256を確認した。
Pixel 10aの既存9件のChat、Auth/Settings、Character、Agent Dialog、legacy再読込はPASS。全message数不変は直前件数未測定のため主張せず、
現状legacy 2・旧note 1・今回send 0を記録する。5560はA1/B1各1 fake→ordinary exact Work/Run↔same Chat、独立ledger outbox 1/hash/typed refs、
再起動8/8/2/2をPASS。OperationsはProject success、完了/取消cardからexact Cancelled detail、空Space、compact headerをPASS。
GUI成功は27.14秒までに観測したが、連続完了計測とは区別する。v255 Native/Portal QAはhistorical evidenceであり、256 manifestのartifact資格とは分ける。

Portal v255 historical qualification covers new PNG upload/thumbnail/viewer/SAF download（1,172,467 bytes・同一SHA）とmultipart code不変。`aoi-v256-observations.json` でmanifestのSHA/source/bytes/certificate一致を確認し、Portal v256 validation noteは保持PNGの
AppLink、own PNG単体DELETE→UI消失＋独立404、再一覧/outbox 0、WSS 101・116145ms・JWT/Gateway
headers・no cookie/query credentials、unlink/cancel/real reauth/Docs 200一回/再起動保持/own Grant revokeをPASSとした。ownAoi/Hydrusの通常Grant解除（error 0）、own5580のgraceful stop（device not found/PID exit）、fixture/observer停止もPASS。fresh
Aoi5580はclear匿名空→通常Settingsでnonblank primary HTTPS Portal /at＋保存→Chrome consent 1回→verified HTTPS自動復帰→
空user/passwordで実Hermes primary proof/PIN成功→通常QA login 1回→既存サーバー会話一覧をPASS。未保存Login値のSettings引継ぎは
UX限定であり、入力引継ぎPASSとは扱わない。fresh Docs同期/再起動、最終ownGrant cleanup/5580 stopはPASSとする。

既存identity/authority、共通Work runtime、routing pin regression、統合suite、
final fake-runtime、Astra GUI、Human unbound legacy fake QAを受領済みとして表へ
反映した。AstraのWeb evidenceはHEAD `164ae514` の独立GUI snapshotに限られる。v256
Native/5560、Operations projection/API、Portal 5580のtransport/media/own-scope操作、fresh Docs同期/再起動、最終ownGrant cleanup/5580 stopはPASSである。
real LLM/native provider、
native CLI、full multinodeも別の未実施境界として残る。

現行mainのbackend/current factsとして、`3a3bf1a63e4fd426a9e774e2f1ae6bf90318b72d` の軽量lifecycle projection
（privacy semantics不変）と、`bb4d0aeef1d735798416667e5f89b7f048967071` のFiles/Project削除30秒明示期限
（7 files、Node 50/Jest 38、typecheck/lint/review PASS）を維持する。256は正式manifestのsource/SHAを正本とし、
このAPKビルド後のcommitted/fixed-snapshot Mobile入力変更なしという資格付けはmanifest対象snapshotに限り、最終docs commitやcurrent main後続sourceとは混同しない。

isolated fake runtimeでは、5秒timeout/15秒遅延のfailure、旧hash固定→次turn v2、
stale lease、restart時Run failed/Work uncertain・provider duplicate 0、gate OFF
text、admission failure orphan 0、stable-UUID replayを確認した。これはfixture-based
evidenceであり、real LLM/native provider/production credentialのPASSではない。

この表の未完了は、real LLM/native provider、native CLI、full multinodeである。Native UIのPASSをこれらの実環境PASSへ拡張しない。
