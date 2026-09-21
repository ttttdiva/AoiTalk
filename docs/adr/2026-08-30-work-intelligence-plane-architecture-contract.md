# ADR: Work Intelligence Plane — current implementation architecture contract

- Status: Accepted (implemented)
- Date: 2026-08-30
- Owners: Work Intelligence Plane / application architecture

## Decision summary

The Work Intelligence Plane is a provider-neutral **projection and composition
layer**. It makes live work state, scoped memory lineage, inference inputs, and
execution evidence explainable without replacing the services that own those
states. The current implementation is intentionally incremental:

* `ContextBuilder.build_context()` is the one normal chat context compiler per
  provider turn/request path. Its active `ContextBundle` is rendered by the
  current provider adapter (provider retries may issue more requests without
  introducing another compiler). `WorkIntelligenceCompiler` is an additive
  read-only projection inside that path; it is not a second prompt stack.
* A `ContextManifest` is a sanitized observation attached to existing
  conversation metadata. It is not prompt input, a tool argument, an ACL grant,
  a memory command, or an execution scope.
* Existing PostgreSQL models, `ScopedMemoryService`, `AgentRun`/dispatch
  outbox, `HeartbeatRunner`, `NotificationDelivery`, `ToolRegistry`, and
  provider routing remain the implementation mechanisms. No graph database,
  event bus, `WorkRun` table, or replacement router is introduced by this
  contract.

The earlier WS1→WS8 list that appeared in this ADR was a planning sequence, not
an implementation contract. It is **superseded history**, and must not be read
as a current completion plan. Work Graph/graph storage, a generic capability
gateway, a new durable WorkRun abstraction, and a new model router remain
deferred until a separate decision is made.

## Data / Memory / Inference mapping

| Plane | Current owner / source of truth | Work Intelligence use |
| --- | --- | --- |
| **Data** | `Project`, `Task`/assignee/activity, Docs (`KnowledgeNode`/`KnowledgeRevision` and project references), `ConversationSession`/participants, `AgentRun`, `ProjectApp`/`AppJob`, and active `User` rows. Project/session/task and Docs ACLs are evaluated by their existing services (`ProjectContextResolver`, `project_permissions`, `docs_acl`, `DocsScope`, and related repositories). | Read a bounded live projection after authorization and liveness checks. Deleted/archived rows and rows outside the trusted Project/Task/Session are omitted. The compiler never writes these rows. |
| **Memory** | `ScopedMemoryService` and its scoped-memory rows are the managed-memory authority for global, user, project, task, and session scopes. Its classify/sensitivity, dedupe, correction, lineage, and mutation paths remain authoritative. | `ContextBuilder` retrieves memory once for the turn. Work Intelligence receives those rows only as advisory input, and the Manifest records bounded hash-only lineage. A memory conflict marks an advisory conflict; it cannot replace current Task/Docs state. Project Steward writes only through `ScopedMemoryService`. |
| **Inference** | `ContextBuilder`/`ContextBundle` plus the provider request/runtime. `WorkIntelligenceCompiler` ranks a typed live projection; provider/model routing and `UnifiedTurnRuntime` still own generation and tool execution. | Render the selected context and the untrusted-data Work Intelligence block to the model through the existing request. Capture structural snapshots, provider metadata, and (when enabled) an inert Manifest for inspection/reproducibility. Manifest hashes and inspector data never become inference authority. |

This mapping is deliberately asymmetric: Data and Memory services own facts and
mutations; Work Intelligence and ContextManifest explain what was selected for
Inference at a particular turn.

## Authoritative owners and trust boundaries

The following boundaries are normative for the current code:

1. **Request identity:** immutable `TurnContext` is the request-local trust
   root. Its actor, Project/Task/Session, explicit references, and
   `include_project_context`/strict-scope flags cannot be replaced by prompt
   text or mutable provider fields.
2. **Project and Task scope:** `ProjectContextResolver` and the existing Project
   permission helpers decide whether a Project, session, or Task is readable.
   A Task-derived Project is accepted only after the resolver's binding checks.
3. **Docs:** `get_project_docs_library`, `DocsScope`, `docs_acl.can_read_node`,
   and `docs_readable_node_predicate` are the Docs visibility boundary. The
   Work Intelligence loader applies that boundary before selecting node or
   revision metadata; it does not infer access from a title or hash.
4. **Project↔Knowledge relationships:** `ProjectKnowledgeService` owns
   canonical/related/reference relationships. Their Manifest entries are
   observations of already-authorized rows.
5. **Managed Memory:** `ScopedMemoryService` is the only supported durable
   mutation owner. Context extraction, Work Intelligence compilation, and
   Manifest construction are read-only.
6. **Tools and capabilities:** `ToolPolicy` and the provider-neutral
   `filter_tools_for_client`/`runtime_tool_capability_status` gate exposure and
   execution. A Work Intelligence item or Manifest hash cannot authorize a
   tool.
7. **Execution and audit:** `AgentRunScope`/sandbox owns execution roots;
   `UnifiedTurnRuntime` owns provider-neutral turn/tool execution; `AgentRun`,
   `AgentRunEvent`, `AgentRunToolCall`, and `AgentRunEdge` own durable run
   history. `AgentRunService` persistence methods are not a substitute for
   API/session/Project ACL checks; those checks remain at the route/runtime
   boundary.
8. **Proactive work:** `HeartbeatRunner` owns durable heartbeat claims, leases,
   cursor advancement, and retry timing. `ProjectStewardCollector` is a
   read-only owner-scoped evidence collector. `ProjectStewardService` may
   mutate only Project-scoped Memory through `ScopedMemoryService` and must
   persist an owner alert before reporting a successful alert-producing run.
9. **Notifications:** the existing `NotificationDelivery` row and its unique
   `dedupe_key` are the durable delivery/idempotency boundary. The Python
   notification service revalidates current Project owner/deletion state before
   list, read, and delivery; the Next notification BFF performs the same check
   before list/read and cancels stale rows before read-all, keeping the UI path
   aligned with the service boundary.

Every projection follows:

```text
authoritative row/service
    -> current ACL/policy resolution
    -> bounded structured projection
    -> sanitizer / hash-only observation where persisted
    -> inspector or audit artifact
```

Projection output cannot widen scope, grant access, mutate its source, or be
used as a replacement source of truth.

### ACL hardening applied at adjacent boundaries

The final integration also closes the principal cross-scope paths that could
otherwise feed an apparently valid projection:

* canonical Project Information pointers are validated for the owning Project
  and Docs library before Project Knowledge/Docs scope or scoped-memory
  promotion uses them;
* Task assignees are checked as active users with current Project-member read
  access before assignment/notification fan-out;
* project-bound group-chat participant discovery and creation are restricted to
  the current Project membership/ACL; and
* conversation/task references and AgentRun origins are re-authorized against
  the live session and Project, with AgentRun API reads also checking the
  session↔Project binding.

These checks happen before Work Intelligence projection or inspector link
resolution. They are not replaced by a Manifest hash or by a provider's cached
registry.

## One ContextBuilder compiler path and provider parity

For a normal turn the path is:

1. The server binds `TurnContext` and resolves the current Project/Task/Session
   through the existing resolver.
2. The provider bridge calls `ContextBuilder.build_context()` once. The builder
   retrieves scoped Memory, Project/Docs indexes, session/task context, and
   (when the literal Work Intelligence gate is enabled) invokes
   `WorkIntelligenceCompiler.compile()` with the authorized Project context and
   Memory rows as advisory input.
3. The resulting `ContextBundle.render_with_trace()` allocates one bounded
   budget by layer priority and renders in stable order. The only Work
   Intelligence text sent to a model is `work_intelligence_block`; the typed
   sidecar remains transient for traces/Manifest construction. The render trace
   retains selected/retrieved character counts, budget clipping, and hash-only
   item provenance/offsets.
4. The provider records the exact request observation in the existing
   `context_snapshot` path. `context_manifest_metadata()` may attach a
   validated Manifest to the provider's existing metadata seam; a Manifest
   failure is optional telemetry and never fails a successful chat turn.

The production bridges all use the same builder contract:

| Runtime | Current bridge |
| --- | --- |
| Native `AgentLLMClient` | `ContextBuildingMixin`/`turn_execution` builds one bundle and supplies the rendered dynamic context to the native runtime; tools are supplied by the request-local filtered registry. |
| Gemini, Ollama, SGLang, OpenAI-compatible local | Provider sync bridges call `ContextBuilder.build_context()` with the current turn's user/Project/Task/Session and immutable Project Context flag, then render/capture the same bundle. |
| CLI (`CLILLMClient`) | Normal chat uses the same builder with its documented minimal mode (`include_project_information=False`, `include_task_context=False`, minimal Project context); textual tool loops still pass through the common filter. Strict tool-free/internal helper paths intentionally omit chat context. |
| Free Team routing | Delegates to the selected target provider. It validates/retains the target's generation metadata rather than compiling a second context. |
| ChatGPT Web provider | Director-only/browser interaction path and not a normal factory target; it is not an additional ContextBuilder compiler. |

When Project Context is explicitly OFF, the shared migration-safe bundle
projection clears Project/Task/Work Intelligence layers and related debug
metadata before rendering or snapshotting. This prevents a reused provider
instance from turning an older bundle into current-turn context.

## Work Intelligence compiler contract

`src/services/work_intelligence.py` owns a bounded, read-only
`WorkIntelligenceResult` (`version=wi-core-1`). The normal `ContextBuilder`
caller supplies the ProjectContextResolver result after ACL checks; the
compiler validates identity/scope consistency and then loads live rows. The
compiler itself is not a second Project ACL implementation (a matching
`project_context` is an authorization assertion from the caller, not a grant
created by this module). Direct record maps are test/repository adapters; when
no trusted resolver context is supplied they must carry an explicit
authorization marker, and they never manufacture access.

The projection may include:

* active Project identity and active Tasks (status, priority, version/freshness,
  creator/owner/assignee relationships, and recent activity/approval evidence);
* ACL-filtered Docs nodes and revisions (identity metadata only, not bodies);
* already-authorized session participants and recent AgentRuns; and
* project-bound enabled Project Apps and recent App Jobs (the projection does
  not replace the App service's separate permission check).

Rows are filtered for deletion/archive/closed state, constrained to the current
Project, ranked against the current query, and bounded by the configured
`max_items` (8), `max_people` (8), `max_evidence` (24), and character budget
(default 3,600; all values are clamped). Prompt-injection-shaped titles/text
are dropped from the model-facing block, which begins with an explicit
untrusted-data warning.

### Evidence, freshness, uncertainty, and relationships

Each item carries source, version, freshness, evidence, and optional
relationship data. `WorkEvidence` records kind/source/relation/version/
freshness plus `strength`/`uncertain`; `WorkRelation` records an allowlisted
relation type, subject/target, evidence hashes, confidence, and uncertainty.
Owner/assignee/creator/editor/participant/activity/approval observations are
kept distinct. Activity, participant, AgentRun, and App Job observations are
weak/uncertain where the source cannot establish durable expertise. A
"who/owner/expert" query refuses to overclaim and records
`insufficient_expert_evidence` when only weak observations exist.

The result also exposes aggregate freshness (`compiled_at`, Project, Tasks, and
Docs latest timestamps), omission counters, and an item trace. Advisory
Scoped Memory rows may set `advisory_conflict`; current Project/Task/Docs state
continues to win and the conflicting memory is not rendered as authoritative
Work Intelligence.

### Ambient live refresh

Compilation has no ambient cache. Each `ContextBuilder` invocation and each
`resolve_work_intelligence_references()` call reads the current authorized
state; a second compile can therefore observe a changed Task status or Docs
revision immediately. Provider turn boundaries clear prior snapshots/bundles,
and Manifest capture is tied to the current turn identity. This is a best-effort
live observation, not a subscription or event stream.

## Contextual capability current revalidation

Provider registries can outlive a Project or configuration change, so
`filter_tools_for_client()` performs the final gate on every exposure/execution
pass before planning or tool-pack early returns:

* `runtime_tool_capability_status()` re-reads the effective current config and
  runtime capability state;
* `ws_*`/workspace tools require a current trusted Project match,
  `workspace_tools_enabled`, an existing manifest, and matching manifest and
  entrypoint hashes;
* Story/Agent Team context is resolved once per exposure from the current
  request and resolver result; resolver failure fails closed, and stale team
  rosters are not retained;
* strict actorless and strict Project scopes wrap or block tools that cannot
  prove the required boundary; and
* planning/review phases apply their read-only allowlists before contextual
  packs are materialized.

There is no `work_intelligence` callable tool or generic Capability Gateway in
this implementation. Work Intelligence is context data; existing tool policy,
ToolRegistry, pack loading, and provider adapters remain the capability surface.

## Planning, AgentRun audit, and restart settlement

Planning is provider-neutral (`PlanningPolicy` AUTO/PLAN_FIRST/DIRECT) and uses
the existing `HumanInteractionManager` plus `AgentRun` audit. An immutable
`ApprovedPlan` canonicalizes the material plan, structured actions, context
selection, and evidence hashes. Its binding includes:

* monotonic plan revision;
* canonical plan digest;
* canonical ordered action digest;
* `context_selection_hash`; and
* evidence-hash-set digest and bounded evidence hash list.

`approved_plan_allows_tool()` requires exact canonical tool arguments except for
fields explicitly listed as dynamic. Approval compares all material binding
dimensions; edited actions/context/evidence require a matching binding rather
than a replay of an older approval. `planning_runtime` emits
`plan.requested`, `plan.feedback`, `plan.approved`, `plan.binding_mismatch`,
`plan.cancelled`, and `plan.timeout` events (plus interaction request/resolution
events) through `AgentRunService`. The pending interaction id/revision is
restart-visible metadata, while the actual in-memory Future is deliberately not
reconstructed.

`AgentRunToolCall` and append-only `AgentRunEvent` rows retain tool/action
success, mutation confirmation, timing, and bounded arguments/results. If a
result contains a ContextManifest, the AgentRun boundary keeps only a
validated hash/reference projection (Manifest hash, reproducibility hashes,
evidence hashes/count). ContextManifest prompt/source fields are stripped, and
the named sensitive Webex tool outputs use their existing redaction marker;
generic tool arguments/results remain bounded audit data and are rendered only
through the ACL-protected AgentRun surfaces.

Provider tasks and interaction Futures are process-local. On WebChatServer
startup, `AgentRunService.reconcile_stale_runs_after_restart()` marks in-flight
`running` runs failed once, clears pending interaction metadata, appends
`run.reconciled_after_restart`, and closes open parent edges whose children are
already terminal. Queued conversation dispatches remain in the existing
`ConversationDispatchOutbox`; lease claims are retried up to the bounded
`DISPATCH_MAX_ATTEMPTS` (currently five). Exhaustion changes the outbox to
`deadletter` and appends `dispatch.deadlettered`/`run.failed` to the AgentRun.
An explicit user stop cancels the run and settles its dispatch lease. No
process restart attempts to resume an opaque provider task or Future.

## Project Steward and durable proactive notifications

`HeartbeatRunner` enumerates active Project scopes for `project_steward`, uses
durable `HeartbeatRunState` cursors/leases with deterministic jitter, and
serializes Steward executions with a single-flight lock. The collector is
read-only, owner-scoped (`Project.owner_id` plus active Project), and
incrementally collects bounded chat, Task/activity, and Docs/revision/reference
evidence in deterministic `(changed_at, event_id)` order. A collector or model
failure leaves its cursor unchanged and schedules the existing retry path.

`ProjectStewardService` runs a fresh isolated Project Automation client with a
physical read-only tool registry and isolated system prompt. It validates the
complete structured plan before applying any mutation, then writes/forgets only
Project-scoped Memory through `ScopedMemoryService`, preserving evidence refs,
namespace, dedupe, and idempotency. Collector output is bounded and strips
secret-like **keys** from structured payloads; it is not a value-level secret
redactor, so selected authorized chat/Task/Docs text may be sent to the
configured Project Automation model.

When a plan contains an insight, forget proposal, or question,
`_persist_owner_alert()` must commit an existing `NotificationDelivery` row
before the Steward run returns success. The row is `channel=in_app`,
`notification_type=project_steward`, owner- and Project-scoped, and carries:

* a dedupe key of the Project, normalized heartbeat name, evidence digest, and
  proposal digest; concurrent/repeated inserts converge on the unique
  `NotificationDelivery.dedupe_key`;
* bounded proposal/question text that has already passed Steward validation; and
* content-free evidence references (`kind`, evidence id, timestamp, row hash)
  plus an evidence digest over all referenced evidence, not only the displayed
  prefix.

Immediately before insertion, `persist_project_steward_notification()` checks
that the Project is still active and owned by the same user. A duplicate is a
successful retry; a database/authorization failure prevents cursor advancement.
The broadcaster and optional push transport run after durable persistence and
are not the idempotency authority. Python list/read/delivery and the Next BFF
list/read paths re-check current owner scope; the BFF read-all path cancels
stale owner-only rows before proxying. Stale rows for a deleted or transferred
Project are therefore suppressed/cancelled on both user-facing paths.

## Manifest and rollback relationship

The companion ADR, `2026-08-30-context-manifest-shadow-contract.md`, is the
field-level contract for the inert observation. In short:

* `ws1-shadow-1` remains accepted for historical manifests without a typed Work
  Intelligence sidecar;
* manifests carrying the current typed sidecar use producer `wi-core-1`; both
  producers retain schema `1.0`, sanitizer `1.0`, and `mode=shadow` validation;
* all resource/evidence/relationship identifiers persisted by the Manifest are
  deterministic `sha256:` correlation hashes, never ACL identifiers; and
* persistent Manifest metadata is controlled by literal boolean config keys and
  can be rolled back without changing the current prompt/runtime path.

## Reused mechanisms and explicit deferrals

Implemented behavior reuses:

* PostgreSQL domain rows and existing ACL/repository services;
* `ContextBuilder`/`ContextBundle` render traces and `context_snapshot`;
* `ConversationMessage.message_metadata` for optional Manifest persistence;
* `AgentRun`/events/toolcalls/edges and `ConversationDispatchOutbox` for durable
  execution audit and dispatch recovery;
* `HeartbeatRunner`/`ProjectStewardCollector`/`NotificationDelivery` for
  proactive bounded work and owner inbox delivery; and
* `ToolRegistry`, `ToolPolicy`, contextual packs, and current provider routing.

The following are not silently implied by this ADR: a graph DB or Work Graph,
an event bus, a new generic capability service, a new `WorkRun` abstraction, a
new model router/catalog, or provider ownership changes. Any of those requires
its own authority, migration, and rollback decision.

## Consequences

Current turns gain a bounded, inspectable, evidence-aware Work Intelligence
projection and an inert reproducibility artifact while Data, Memory, ACL,
Inference, execution, and notification authorities stay explicit. Live
re-resolution keeps inspector links honest after revocation, while durable
AgentRun/Heartbeat/NotificationDelivery mechanisms make restart, retry, and
proactive delivery auditable without introducing a parallel platform.
