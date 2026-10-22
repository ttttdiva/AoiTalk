# Common Agent work runtime

This is the development contract for the single durable runtime shared by
internal company Tasks, MediaOps, the existing code-agent harness, Chat, and
future Apps.  It is intentionally separate from the conversation transport
outbox and from any provider-specific ledger.  Chat's interactive lane is a
first-class WorkItem/Run participant, while the autonomous coordinator remains
prohibited from replaying interactive Chat turns.

## Contract status

WS02 now adds the durable `AgentWorkItem`/`AgentWorkEvent` models and
`20260903_0002_agent_work_runtime.py` migration.  The common service
`src/services/agent_work_runtime.py` implements the coordinator protocol and
the Task WorkSource projects existing Task rows.  Media WorkSource classes are
also additive.  A section marked **deferred** must not be reported as a
production-ready runtime until the named focused concurrency/recovery test and
application startup wiring are available.

The 2026-10-11 Chat/Operations decision is recorded in Japanese at the end of
this document.  The decision is a contract for the shared ledger and authority
boundary; source presence, migration application, and runtime evidence are
reported separately and must not be inferred from this text.

The additive Chat schema is `alembic/versions/20261011_0002_conversation_agent_binding.py`.
It adds the nullable session/work link and immutable binding table; it does not
replace the WS02 WorkItem/Event migration or create a second queue.

## Responsibilities and ownership

| Component | Owns | Must not own |
| --- | --- | --- |
| `WorkSource` | Discovering due/eligible domain work and materializing an idempotent work item | Claims, leases, provider calls or retry policy |
| `ExecutionAdapter` | Binding a claimed item to one execution domain (company, MediaOps, code harness, App) and settling its domain evidence | A second scheduler, durable claim ledger or hidden provider credentials |
| `AgentWorkCoordinator` | Candidate polling, atomic claim, lease renewal, dispatch, retry/recovery, budget/concurrency reservations and terminal settlement | Domain source-of-truth mutations that belong to a service/adapter |
| `AgentWorkItem` | Durable work identity, source reference, state, priority/deadline, lease/fence, retry and budget metadata | A duplicate copy of Task, Media, or Operations ledgers |
| `AgentWorkEvent` | Append-only lifecycle/audit facts with safe metadata | Raw prompts, credentials, provider payloads or unrestricted paths |
| `AgentRun` | One immutable execution attempt and pinned revision/evidence | Replacing the work-item queue; each work item may have many attempts |
| Heartbeat | Wake/cursor signal that causes due discovery | A competing executor, claim owner or retry scheduler |
| `ConversationDispatchOutbox` | Conversation message/run transport delivery | The generic AgentWork queue, autonomous execution, or a retry authority |
| `InteractiveChatLane` | Exact admission/start/renew/settle of one live Chat turn and its WorkItem/Run pair | Background polling, blind retry, or a second Chat ledger |

No Company-specific queue, lease, scheduler, retry table or coordinator is
permitted.  All domains use the same coordinator and state machine.

## Durable work-item shape

The WS02 migration adds a stable UUID work item with at least:

* source/domain discriminator and opaque source key (for example Task,
  research routine, generation plan, or code-agent task);
* Agent, Space, Project, Task/Persona/App references where applicable;
* nullable `conversation_session_id`; Chat rows use `source_type=chat_turn`
  and `execution_adapter=interactive_chat`;
* state, priority, `not_before`, deadline and idempotency key;
* lease owner, opaque lease token, lease expiry and renewal timestamps;
* attempt count/max attempts, next retry time and dead-letter reason;
* approval/uncertain markers, bounded budget reservation and concurrency key;
* causal parent/source references and created/updated/settled timestamps.

References are links/projections, not copied domain payloads.  The coordinator
must be able to re-read the canonical source before every important mutation.
`AgentWorkEvent` records lifecycle transitions and bounded identifiers, with
append-only ordering and safe redaction.

## Lifecycle and state semantics

The closed state vocabulary is deliberately explicit (implementations may use
repository-compatible names, but not conflate these meanings):

```text
pending -> claimed -> running -> succeeded
                     |       -> retry_wait -> pending
                     |       -> blocked / awaiting_approval / uncertain
                     |       -> failed -> retry_wait or dead_letter
                     +----------> cancelled
```

* `pending`: eligible but not owned by a worker.
* `claimed`: a lease token is held; execution has not necessarily started.
* `running`: adapter has started the attempt.
* `retry_wait`: transient failure with a future retry time; a **new AgentRun**
  is created for the next attempt.
* `blocked`: current ACL, source state, budget, or policy prevents safe work.
* `awaiting_approval`: an existing human approval boundary must complete.
* `uncertain`: an external side effect may have happened; reconciliation is
  required and blind retry is forbidden.
* `succeeded`, `failed`, `cancelled`, `dead_letter`: terminal history.

An interactive Chat WorkItem is admitted by the live transport, starts one
exact `AgentRun`, and is settled by that same transport/run owner.  Its
`max_attempts` is one for the turn.  A disconnected or expired interactive
lease is recovered as a delivery/runtime failure or an explicit uncertain
state; the autonomous coordinator must not create a new Chat attempt from a
stale row.

State transitions are conditional on the current row/version and lease token.
An adapter cannot mark another worker's lease as settled.

## Claim, lease and execution invariants

1. Candidate discovery is advisory.  The coordinator performs an **atomic
   claim** of one eligible row using PostgreSQL `FOR UPDATE SKIP LOCKED` plus a
   conditional update (and a compare-and-set update for SQLite) with a freshly
   generated opaque, **tokenized lease**.
2. Two workers may observe the same candidate, but only one valid token can
   transition it to `claimed`/`running`.  Losing workers back off without
   executing.
3. Renewal and settlement require the same token and owner.  Lease loss or
   renewal failure stops side-effecting work and records a bounded event.
4. An expired `running`/`claimed` item is recovered on coordinator restart or
   the next sweep.  Recovery rechecks source existence, permissions, revision,
   fence and budget before retrying; unsafe or uncertain work is blocked/
   escalated instead.
5. Claim and any coordinator-owned reservation are released exactly once.  The
   current schema carries bounded `budget_reservation` metadata and global
   concurrency limits; provider-specific budget reservation accounting remains
   an integration gate, not an implied success.
6. Duplicate trigger delivery and coordinator startup are idempotent.  A
   deterministic source key/idempotency key prevents duplicate active work.
7. The autonomous poller excludes `source_type=chat_turn` with
   `execution_adapter=interactive_chat`.  Chat uses the explicit interactive
   admission/start/renew/settle path, so an outbox replay cannot become a
   second executor.
8. One session has at most one active interactive turn.  Different sessions
   for the same Agent may run in parallel only within the exact pinned
   revision's `max_parallel_runs`/`max_parallel_tasks` and the global
   coordinator limits.

The same Agent may be bound to multiple independent conversation sessions.  A
shared Agent identity does not merge their history, context, memory,
provider/terminal continuation, or Run/Work lease; each session remains an
independent ACL and execution fence.

The PostgreSQL implementation must use row-level transactional semantics that
work across processes.  SQLite remains a supported test/runtime path and must
have an explicit, exercised lock/recovery strategy rather than a process-local
`set` pretending to be a claim ledger.

## Revision, authority and evidence

At claim/start the coordinator pins the exact `AgentRevision` and creates one
`AgentRun`.  `AgentRun` is immutable attempt evidence; a retry links
`previous_attempt_run_id` and creates another row.  The bounded execution
manifest records the resolved IDs/capabilities for audit but never grants new
authority.  Before Task/Project/Docs/Files/Media mutation, the adapter calls
the authority resolver and canonical domain service again.  Agent deactivation,
grant revocation, source archival/deletion, budget exhaustion or policy change
therefore blocks a stale run.

For a Chat turn, the transport performs the same exact revision pin after
server-side session/Agent binding and Human ACL checks.  The normal text lane
may continue when the background autonomous runtime gate is disabled; the
gate controls background WorkSources/adapters and must not silently disable a
human-requested interactive turn.  Tool calls and side effects still require
the intersection of Human session/Project ACL and the current
`AgentAuthorityResolver` decision.  A new AgentRevision is applied to the
next turn; it never mutates an already-started Run.

Tool/harness scopes, Team/Subagent ceilings and profile/runtime flags are all
intersections.  Prompts, model output and source metadata cannot enlarge the
set.  Events and run DTOs omit credentials, raw provider responses, hidden
paths and unrestricted environment values.

## Failure and recovery semantics

Retry, dead-letter and external fences are distinct outcomes; recovery never
assumes that a timed-out provider call was harmless.

Classify failures before scheduling a retry:

* transient transport/timeout → bounded exponential backoff and new attempt;
* permanent validation/ACL/source deletion → `failed` or `blocked`;
* max attempts exhausted → `dead_letter` with operator-visible reason;
* approval required → `awaiting_approval`, never auto-approve;
* uncertain provider result → `uncertain`, reconcile by provider/source fence;
* budget/concurrency saturation → `retry_wait` or `blocked`, without spinning.

Interactive Chat failures are not converted into an autonomous retry.  The
message/run/outbox idempotency key may safely complete or report the same
delivery, but a new side-effecting Chat attempt requires a new explicit human
turn.  Transport lease expiry therefore follows the exact-claim fence and
settlement contract rather than the background retry schedule.

Media generation retains `GenerationRunIntent` as the provider-submit fence;
the generic retry loop must not submit a second generation merely because the
response was lost.  Publication continues through `ExternalAction` approval,
Attempt and Receipt.  A causal parent/source chain has bounded depth and
dedupe keys to prevent Agent → action → trigger loops.

## Heartbeat integration

Heartbeat due records may wake the coordinator and advance a source cursor,
but they do not claim work or execute adapters.  A duplicate heartbeat is
therefore harmless: source materialization is idempotent and the coordinator
still performs the sole claim.  Existing heartbeat history remains an audit
ledger and is projected into Operations rather than replaced.

## WorkSource/ExecutionAdapter development guide

Use this guide when adding a WorkSource or ExecutionAdapter; the checklist is
part of the runtime contract, not an optional implementation note.

Every new source/adapter must document and test:

1. canonical source identity and idempotent materialization;
2. required Agent/Revision/Space/Project/Persona authority;
3. safe input/output evidence projection and redaction;
4. claim/lease/renew/settle behavior, including lease loss;
5. retry classification, max attempts and dead-letter escalation;
6. restart recovery and duplicate trigger/worker behavior;
7. budget/concurrency reservation and release;
8. provider/external fence and uncertain reconciliation;
9. link back to the canonical source screen and activity ledger.

Do not add a source by calling the current Agent Harness tracker directly or by
writing an Operations shadow row.  The coordinator owns the generic lifecycle;
the adapter owns domain semantics.

## Current evidence and gaps

**Verified (source):** WS01 AgentRun typed identity/revision/manifest fields,
safe redaction, ConversationDispatchOutbox's separate role, additive
WorkItem/Event models and migration, the Task/Media WorkSource projections,
and the Chat-specific `conversation_session_id` link/migration.
See `src/memory/models/agent_work.py`,
`alembic/versions/20260903_0002_agent_work_runtime.py`,
`alembic/versions/20261011_0002_conversation_agent_binding.py`,
`src/services/agent_work_runtime.py`, `src/services/task_work_source.py` and
`src/services/media_work_sources.py`.

**Implemented (source, focused runtime tests):** transactional materialize,
PostgreSQL row-lock/SQLite CAS claim, token-fenced start/renew/settle, stale
recovery, bounded retry/dead-letter classification, AgentRun linkage and safe
WorkEvents.  The public `register_source`, `register_adapter` and direct
`materialize` methods now apply the same effective runtime gate as
`discover_and_materialize`; startup still must avoid retaining a coordinator
whose cached profile became disabled.

**Implemented (Chat lane):** `AgentRunService` pins a fresh immutable binding
and exact revision in the same transaction as the Chat Run/WorkItem/outbox
handoff.  `admit_interactive` uses the session row lock, `max_attempts=1`,
session/Agent fences, and the exact `start`/`renew`/`settle`/`cancel` helpers;
`claim_many` excludes interactive Chat rows.  `interactive_enabled` is
separate from the autonomous runtime gate, so a normal text turn can proceed
while background autonomous WorkSources are disabled.

**Runtime limits:** full multinode contention and real LLM/native provider
execution remain outside the received evidence.  The dedicated PostgreSQL
upgrade/down/up migration smoke, Astra GUI, and Human legacy fake QA are PASS;
those checks do not establish real provider or full multinode behavior.  The
prior Astra GUI result is the independent Web snapshot at HEAD `164ae514`;
v256 Native Pixel/5560 and Portal 5580 transport/media/own-scope operations are
PASS.  Fresh Portal Docs sync/restart and final ownGrant cleanup/5580 stop are
PASS.

**Runtime evidence received:** same Agent across separate Chat sessions reached
provider parallelism with max active 2 while client/provider/history/privacy
namespaces remained isolated; stop cancellation settled Run and Work as
cancelled with one partial `user_stop` outcome.  Final fake-runtime checks also
covered timeout, revision, stale lease, restart uncertainty, no-orphan
admission, gate-off text, and atomic client replay.  Astra GUI and Human legacy
fake QA are also PASS; real provider/native CLI and full multinode execution
remain outside the received evidence.

The isolated fake runtime also passed the bounded invariants: per-session
history tags and provider/privacy namespaces stayed owner-only with provider
parallelism capped at two; injected SDK five-second timeout against a fake
15-second delay produced truthful Run failed/Work permanent with no late
success; old revision hash remained fixed while the next turn used v2; stale
lease settlement was rejected; restart yielded Run failed/Work uncertain with
zero duplicate provider submissions; Agent-bound text worked with the
autonomous gate OFF; failed client admission left no orphan; and stable-UUID
client replay was atomic. These are isolated fake/runtime checks, not real LLM,
native-provider, or production credential evidence.

## 2026-10-11 Chat/Operations 永続ledger運用決定

### lane と責務

ChatとOperationsは同じ `AgentWorkItem` / `AgentRun` / `AgentWorkEvent` を監査・
recoveryの正本として使う。ただし実行laneは分ける。

| lane | admission/claim | retry/recovery | gate |
| --- | --- | --- | --- |
| 通常テキストChat | live transportがserver ACL、Agent active/latest revision、exact Runを検査し、`admit_interactive` → `start_interactive` → `renew_interactive` → `settle_interactive` を呼ぶ | 同じclient message/runのidempotent deliveryだけ。background coordinatorは再実行しない | background autonomous gateがOFFでもHumanのinteractive requestを許可する |
| Operations/Task/Media/App | WorkSourceがmaterializeし、`AgentWorkCoordinator`がatomic claim/lease/dispatch/settleを行う | coordinatorが新しいAgentRunを作る。uncertainはreconcile、ACL失敗はblocked | Organization/profile/runtime gateとsource/adapterのfeature gate |

`ConversationDispatchOutbox` は transport deliveryのoutboxであり、WorkItemを
pollするキューではない。outboxのduplicate delivery、WebSocket reconnect、REST
handoffの再送は、既存のrun/message idempotencyを再利用して同じRunへ戻す。
outbox payloadやclient requestはAgent、Revision、authority、provider routeを
決めない。

### admission の不変条件

interactive rowは次の形でmaterializeする。

```text
source_type       = chat_turn
execution_adapter = interactive_chat
intent_key        = interactive_chat
domain            = conversation
conversation_session_id = ConversationSession.id
active_agent_run_id    = exact AgentRun.id
max_attempts      = 1
concurrency_key   = conversation:<session_id>
```

`AgentWorkItem.conversation_session_id` はnullable FKで、既存のTask、Media、
code-agent、App itemはNULLのままでよい。Chat rowはcanonical sessionを再読出しし、
sessionが削除済み、Agentがinactive、revisionが別Agent、Runのwork linkが別item、
同sessionにactive interactive itemが存在する場合はfail-closedでadmitしない。

Session projectionのDB naive UTC時刻はUTCの`Z`表記へ正規化する。
`agent_binding_updated_at`はChat recencyを変えないbinding mutation clockであり、
WorkItem/Runのcreated/updated時刻や古いbinding作成時刻をclient syncの代わりに使わない。

`start_interactive` のleaseはlive transport専用のtoken fenceであり、background
pollerのleaseと兼用しない。renew/settleは同じowner/token/runだけを受け入れ、
lease loss後のadapter callbackはside effectを続行せずbounded eventだけを記録する。
同一sessionでの次のturnは、前のitemがterminalまたは明示的にsettledになった後だけ
admitできる。

### authority、routing、contextの再検証

Chat turnはHuman session/Project ACLとAgent authorityの交差を使う。Agentの
`AgentRevision`から `AgentRoutingPins` を作り、Team、Execution Profile、allowed
Subagents、provider routeをserver-owned snapshotへ固定する。prompt、Characterの
field、client JSON、model output、route表示値をauthorityとして扱わない。

各tool call、Project/Docs/Files/Media write、ExternalAction proposal/approval、
Run settlementの境界で次を再確認する。

* Humanが対象session/Projectを読める・書けること。
* Agentがactiveで、Runのexact revisionとAgentに一致すること。
* current `AgentAuthorityResolver` が要求capabilityを許可すること。
* Task/Persona/Space/Project grant、resource scope、budget、runtime gateが有効で
  あること。

この再検証はunified async/sync router、ToolDefinitionの直接呼び出し、provider/native
laneを同じ `agent_authority_guard` へ通す。Task、Docs node/attachment、File、Media
asset、Artifact、WorkItem、AgentRunなどのresource IDは、turnでserverが明示承認した
referenceであり、DBから解決したcanonical Project scopeに一致しなければfail-closed
になる。unknown resource、resolver障害、権限の一部だけが許可されたmulti-family
toolは実行しない。

AgentScopedMemoryも同じresource guardを通す。既存 `ScopedMemoryService` のcandidate
SQL prefilterはProject、Task、current ConversationSessionに限定し、embedding、lexical、
relevanceの評価はこの集合の後で行う。personal/global、暗黙cross-session、foreign
scopeは拒否する。HumanActorは人間のまま保持し、Agent/Revision/Run provenanceを
memory行へ明示する。memory toolsは既存の`project_read`/`project_write` capabilityを
使い、scope不明・DB/embedding/lexical障害・原本ACL失敗を空の成功へ変換しない。

Native CLIは有限の `workspace_read` / `workspace_write` / `command_execute` だけを
resource scopeへ写像し、任意のMCP/tool surfaceをAgent能力として扱わない。AgentRun
scopeとcurrent authorityをattestできないCodex native direct Chatはsubprocess前に
拒否し、Claudeはtool-free制限へ縮退する。legacy unbound CLIとOperationsの既存
scoped adapterはそれぞれの従来境界を維持し、Agent-linked Chatの権限を継承しない。

deactivate、grant revocation、Project archive、revision不一致、scope失効、lease
 lossは、bind時のsnapshotを理由に継続させず、blocked/failed/uncertainへ進める。
Terminal/provider continuation handle、history、context snapshot、session memory、
lease tokenはsession/turn単位で保持し、process-global stateに載せない。

AgentをsessionへbindしただけではCharacter-based implicit cross-session recallを
許可しない。context builderとmemory searchは、現在sessionと明示scopeを使い、原本
返却直前にHuman ACL、session/project scope、active/expiryを再確認する。WorkItem、
Run、artifact、workspace、terminal結果はpermission-checked projectionとしてだけ
公開し、別sessionのraw private transcriptやcredential/provider payloadを共有しない。

ChatがOperationsのwork evidenceを表示する場合は、利用者が明示的に選択した
`work_item_id`、`agent_run_id`、artifact ID、またはActivity linkを入力境界とし、
同じcanonical ledgerをscope-awareに再読出しする。linkは画面上の参照関係であり、
Agent bindやWorkItemの存在から別scopeの権限を推測しない。返すのはclosed schemaの
bounded projection（許可済みID、domain/source code、state/outcome、時刻、attempt数、
boundedなresource/evidence reference）だけである。現行helperはresult summary本文を
返さず、resource referenceのuri/pathも表示用のbounded値でありexecution scopeではない。
raw event text、prompt、別session transcript、provider response/model/base URL、
credential、lease/tokenをcontextやChat本文へ暗黙注入しない。

既存 `src/services/operations_employee_observability.py` の `work_metadata` と
`causal_activity` に加え、`AgentRunService.list_agent_activity` と
`src/tools/agent_activity.py` がChat/Agent turn向けの共有safe projectionを提供する。
session ACL、current Project read ACL、Agent active/grant/windowを各rowで再確認し、
runはidentity/lifecycle/scope/time/manifest/resource_refs、WorkItemはsafe DTOと
session IDだけを返す。objective、metadata、result summary、conversation body、
event text、tool argumentsは除外する。HTTPの
`GET /api/agents/{agent_id}/agent-runs` とOperations/Chatのlinkはprivate ACL付きの
read-only projectionであり、画面linkだけで別scopeを開かない。

このhelperを使うAgent activity toolはside-effect-free、件数boundedで、明示された
current Agent/human turnからだけ呼べる。別session transcript/prompt/event textを
contextへ暗黙注入せず、明示されたwork/artifact referenceの安全なstatus/evidence
だけを返す。Chat ledger/body-free ACL regressionとfake runtimeをPASSとして扱う。
full multinodeとreal providerの証拠は別gateである。

### Native Chat ↔ Operations projection contract

For the cross-screen reference, Native Chat assistant messages use
`metadata.agent_run_id`. The client hydrates at most the latest 64 distinct
assistant Run IDs into the ACL-scoped bounded Run DTO. It renders the related
Work button only when the DTO Run ID exactly matches the message metadata and
the message/Run session IDs, Agent identity, and `work_item_id` all agree.
Missing, stale, failed, cross-session/Agent, or Work-less projections remain
without a guessed link.

Operations navigation accepts only typed `{kind, id}` values in `canonical_ref`
or `source_ref`; Chat navigation requires `kind=conversation`. Scalar session
IDs, URLs/paths, and arbitrary metadata are ignored. If both typed references
exist but conflict, the projection is fail-closed. The existing bounded
overview (limit 100) keeps recent `succeeded` and `cancelled` Work discoverable;
it is not an unbounded history or global count.

The Native Operations overview and Work execution reads use a local 30-second
timeout. Timeout, Gateway reauthentication, 401/403, connection, and other
read failures have separate safe classifications. Scope/server-fingerprint
changes, screen deactivation, and aborted requests discard stale responses
through revision/active/abort guards. Character remains presentation/legacy
surface and never supplies Agent identity or cross-session authority.

将来のAoi系coordinationや新しいAppは、このcommon runtimeの
`AgentWorkItem`/`AgentRun`/`AgentWorkEvent`と既存`ConversationSession`をtyped linkで
利用する。新しいidentity、Chat専用queue、Operations shadow ledgerを作らず、source
identity、exact revision、Human/Project/session ACL、lease、artifact scopeを同じ
coordinatorとsafe projectionへ通す。coordination eventは通知・参照のbounded recordで
あり、受信側へAgent authorityやprivate transcriptを継承しない。

### 失敗、再起動、復旧

interactive Chatで次の状態を混同しない。

* delivery duplicate: 同じRun/message idempotencyへ再接続し、別Runを作らない。
* transport lease loss: side effectを停止し、同じtokenでのsettleだけを許可する。
* provider result unknown: `uncertain` とfenceを残し、Operationsのbackground retryで
  Chatを再送しない。
* Human/Agent ACL失効: `blocked` または安全なterminal failureにし、Humanが新しい
  turnを明示するまで再実行しない。
* process restart: outbox deliveryの履歴とRun/Eventを復元するが、live Chatの
  stale itemをcoordinatorがblind executeしない。

実runtimeではstop cancellationがRunとWorkをともに`cancelled`へsettleし、partial
`user_stop` outcomeを確認した。final fake-runtimeではfailure markerとRun/Workの
failed/permanent、restart時uncertain、late success抑止を確認している。

Operationsの通常WorkItemは既存のbounded backoff、new attempt、dead-letter、
GenerationRunIntent/ExternalAction fenceを使う。Chatを同じpoll/retry処理へ混ぜない
ことで、outbox再送と自律処理の二重実行を防ぐ。

### client transport と failure truth

Agent-bound Web/Mobile Chatではprovider/model/Team/ProfileのcontrolsをAgent管理へ
寄せ、Character-direct Chatのlegacy pathを維持する。Mobileのbind/unbind、canonical
refresh、CASはnormal server APIへ送る。offline queueはAgent-bound turnのdirect
fallbackやblind retryを行わず、queued messageを個別cancel/draftへ移す。accepted/
uncertain itemは保持し、legacy unknown metadataは捨てず、次のsendには新しいclient
message IDを割り当てる。

初回native作成はstable client UUIDのPUT aggregateでinitial user message、greeting、
owner participant、binding、sessionを同一transactionにする。legacy POST初回fallback
は拒否し、replayは同じaggregateを返す。Human write ACLはprovider startと同一transaction
で再確認し、old human RunのreplayがAgentへretargetしないようにする。

Provider failureはUI markerとRun/Workのfailed stateを一致させる。verifier再queryで
成功を作らず、SDKのbounded retry契約だけを適用する。final fake-runtimeではこの
failure truthを確認済みで、real providerは別gateに残す。

### migration、parallelism、validation entry

次の順で統合を検証する。各行は実行結果があるまで `IMPLEMENTED` または
`UNVERIFIED` のままにする。

| 検証対象 | 必須 evidence entry | 完了判定 |
| --- | --- | --- |
| schema | `conversation_sessions.agent_id` / binding table / `agent_work_items.conversation_session_id` のPostgreSQL upgrade、SQLite batch、legacy row/FK smoke | nullable compatibility、SET NULL/CASCADE、one-head、rollbackが実測済み |
| binding | active/latest revision、Human ACL、group拒否、expected Agent CAS、busy Run拒否、clear/bind idempotency | focused service/API testと実DBが一致 |
| turn pin | revision更新がin-flight Runに遡及せず、次turnだけ新revision、route snapshot/hashがserver-owned | two-turn revision testとRun manifest検査 |
| ledger | outbox deliveryとWorkItem/Runの一対一、interactive exact claim/renew/settle、coordinatorのChat除外 | duplicate/reconnect/lease loss/restart test |
| concurrency | same Agent別session parallel、max_parallel_runs/tasks saturation、same session single lane、shared resource fence | SQLite CAS + PostgreSQL `FOR UPDATE SKIP LOCKED`のmulti-process test |
| authority | Human ACL ∩ current AgentAuthorityResolver、deactivate/grant revoke race、tool/mutation revalidation | negative permission/revocation testとsafe event projection |
| runtime gate | background gate OFFでも通常text interactive、autonomous WorkSourceは停止 | feature profile test + local runtime observation |
| isolation/memory | Terminal/provider/history/context/session分離、implicit Character recall禁止、AgentScopedMemoryのProject/Task/currentSession prefilter、artifact scope ACL | AgentScopedMemory scope/provenance regression、memory原本再読込検査 |
| shared evidence | 明示link→canonical Work/Run/artifact再読出し、closed safe projection、raw transcript/prompt/event除外 | Bのshared helper、CのChat call site、scope/redaction回帰と実Chat参照 |
| clients | Web/Mobile Agent controls、canonical CAS refresh、stable-UUID PUT aggregate、offline queue cancel/draft、truthful failure marker | generated OpenAPI/types、client replay/CAS、実ブラウザ・mobile sync evidence |
| Native Chat↔Operations | `metadata.agent_run_id` の直近64件hydrate、exact Run/session/Agent/Work検証、typed ref conflict、recent terminal Work、30秒read分類/stale abort guard | `7fdb12a7d228698ae60c832bbb3f999da013147c`、`d9fffd6f5db93358206c850405a81d8c37187627`、`9f5fd306f9cbcb7cd3aecf1dad2baee27b5afad3` のsource/focused test確認。v256 Native/5560の最終QAは下記validation noteの範囲でPASS。Cancelled detailもexact Work/Run参照でPASS |
| Operations read/projection | 30秒scoped read、closed scalar `_run_summary`、schema/ACL/count parity、bounded succeeded/cancelled recent card | `3a3bf1a63e4fd426a9e774e2f1ae6bf90318b72d` の軽量lifecycle projection（privacy semanticsは変更なし）、37 tests、独立review、Project/Space API確認、v256 Project success・完了/取消card→exact Cancelled detail・空Space・compact headerをPASS。GUI成功は27.14秒までの観測で連続完了測定とは区別 |
| External Portal 5580 | v255のimage/multipart/SAF historical evidence＋`artifacts/releases/mobile/v0.1.256/aoitalk-mobile.apk.manifest.json` のv256 smoke | v255の新規PNG upload/thumbnail/viewer/SAF download（1,172,467 bytes・同一SHA）とmultipart code不変を資格付けとし、v256は保持PNG単体DELETE→UI消失＋独立404、再一覧/outbox 0、WSS/JWT/Gateway header、unlink/cancel/real reauth/Docs 200一回/再起動保持/own Grant revoke、fresh Docs personal/project HTTP200・sync terminal/outbox 0・force-stop/restart後HTTP200/outbox 0/noCredentialInput/promoted、最終ownGrant cleanup/5580 stopをPASS |
| browser | Agent selector/catalog、same-Agent sessions、reload/filter、stale 409 recovery、group/legacy label、Agent gear/Work→Chat/UI dispatch | Astra独立GUI final PASS（HEAD `164ae514`、Next `15940`、snapshot `1595`/SHA0）。same-Agent/reload/filter/stale-409/group/legacy、gear、canonical Work→Chat roundtrip、actual dispatch、source-fix後console/API/private-activity checksを確認。v256 Native/5560、Portal 5580は実機PASS |

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

現在のsource/test確認は、schema/binding/turn pin、Chat WorkItem/Run atomic admission、
interactive gate分離、same-session/Agent fences、per-call authority/resource guard、
AgentScopedMemory scope/provenance、bounded Agent activity DTO、client snapshot/replay/
CAS境界までを実装済みとして扱う。B/Operations/Cのfocused suite、専用PostgreSQL
migration upgrade/down/up、legacy FK/immutable/SET NULL/CASCADE smoke、同Agent別session
parallel/provider/history isolation、stop cancellation、final failure truthはPASSとして
受領した。
Backend/client reviewではBのwrite ACL・metadata SQL coherence・direct guard pathsも
bounded merge clearとして確認済みである。
残る制限はreal LLM/native provider、native CLI、full multinodeである。v256 Native/5560の
実機PASSをこれらの実環境PASSへ拡張しない。

作業ツリーで確認できるAPI入口は
`src/services/agent_work_runtime.py` の `admit_interactive*` /
`start_interactive*` / `renew_interactive*` / `settle_interactive*` と、
`claim_many` のinteractive除外である。Chat dispatchからRun/Work/outbox transaction
へ至る呼び出しは `AgentRunService._ensure_interactive_work_in_session` と
`src/services/chat_execution_adapter.py` に実装され、focused ledger回帰で確認されて
いる。`interactive_enabled` は `_runtime_enabled()` と分離されているため、通常text
Chatのgate契約に対する旧不一致は解消済みである。ただしfull multinodeとreal
providerはこの文書だけでPASSにしない。

既存runtime/routing/identity回帰と、Chat interactive lane、session binding、
shared safe activity helper、migration、background gate OFF、final fake-runtime、
Astra GUI、Human unbound legacy fake QAの統合evidenceを受領済みとして表へ反映した。
WebのAstra evidenceはHEAD `164ae514` snapshot、Native v256/5560は上表の実機PASS、
External Portal 5580はtransport/media/own-scope操作、fresh Docs/restart、ownGrant cleanup/5580 stopのPASSを記録している。
実行境界としてreal LLM/native provider、native CLI、full multinodeは `UNVERIFIED` のまま残す。

Current-main facts remain `3a3bf1a63e4fd426a9e774e2f1ae6bf90318b72d` lightweight lifecycle
projection (privacy semantics unchanged) and `bb4d0aeef1d735798416667e5f89b7f048967071`
Files/Project-delete deadline (seven files; 50 Node, 38 Jest, typecheck/lint/review PASS).
The v256 manifest is the release source/SHA authority.  The qualification that
no committed/fixed-snapshot Mobile inputs changed after this APK build applies
only to the manifest snapshot; later docs commits and current-main source remain
separate.
