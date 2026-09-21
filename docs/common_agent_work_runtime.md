# Common Agent work runtime

This is the development contract for the single durable runtime shared by
internal company Tasks, MediaOps, the existing code-agent harness, and future
Apps.  It is intentionally separate from the conversation transport outbox
and from any provider-specific ledger.

## Contract status

WS02 now adds the durable `AgentWorkItem`/`AgentWorkEvent` models and
`20260903_0002_agent_work_runtime.py` migration.  The common service
`src/services/agent_work_runtime.py` implements the coordinator protocol and
the Task WorkSource projects existing Task rows.  Media WorkSource classes are
also additive.  A section marked **deferred** must not be reported as a
production-ready runtime until the named focused concurrency/recovery test and
application startup wiring are available.

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
| `ConversationDispatchOutbox` | Conversation message/run transport delivery | The generic AgentWork queue |

No Company-specific queue, lease, scheduler, retry table or coordinator is
permitted.  All domains use the same coordinator and state machine.

## Durable work-item shape

The WS02 migration adds a stable UUID work item with at least:

* source/domain discriminator and opaque source key (for example Task,
  research routine, generation plan, or code-agent task);
* Agent, Space, Project, Task/Persona/App references where applicable;
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
WorkItem/Event models and migration, and the Task/Media WorkSource projections.
See `src/memory/models/agent_work.py`,
`alembic/versions/20260903_0002_agent_work_runtime.py`,
`src/services/agent_work_runtime.py`, `src/services/task_work_source.py` and
`src/services/media_work_sources.py`.

**Implemented (source, focused runtime tests):** transactional materialize,
PostgreSQL row-lock/SQLite CAS claim, token-fenced start/renew/settle, stale
recovery, bounded retry/dead-letter classification, AgentRun linkage and safe
WorkEvents.  The public `register_source`, `register_adapter` and direct
`materialize` methods now apply the same effective runtime gate as
`discover_and_materialize`; startup still must avoid retaining a coordinator
whose cached profile became disabled.

**Unverified:** PostgreSQL contention/restart under production isolation,
multi-process duplicate-worker behavior, complete coordinator startup/cutover,
real provider uncertainty and production load/performance.  SQLite migration
and unit checks do not establish those properties.
