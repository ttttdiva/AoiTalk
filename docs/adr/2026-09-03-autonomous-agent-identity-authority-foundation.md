# ADR: Autonomous Agent identity and authority foundation

- Status: Accepted (WS01 target architecture)
- Date: 2026-09-03
- Owners: Agent platform / authorization
- Scope: WS01 — Autonomous Agent Identity & Authority Foundation

## Context

AoiTalk has durable Users, Spaces, Projects, Tasks, MediaOps Personas, ECC
Characters, and an existing code-oriented `agent_harness`.  Those concepts do
not have the same identity or authority semantics.  Autonomous work needs a
stable worker identity that can be assigned to existing work without creating a
second company model, a second scheduler, or a synthetic human account.  This
ADR freezes the additive foundation and the hand-off boundaries for WS01–WS06.

This is a target architecture contract, not permission to implement the later
runtime early.  WS01 may add the identity, relationship, policy, API, and
compatibility foundations described below; it must not start autonomous workers,
dispatch work, or migrate the Operations UI.

## Decision summary

1. One installation/deployment has exactly one deployment-wide `Organization`
   (Company/settings) row.  It is not one company per Space and is not a
   multi-tenant selector.
2. The existing `Space` remains the department/division/team/business-domain
   boundary above Projects.  Agent membership is an explicit relationship to
   that Space and never implies Project write access.
3. `Agent` is a persistent autonomous worker identity.  It is distinct from a
   human `User`, presentation `Character`, MediaOps `Persona`, execution-topology
   `Agent Team`, execution-time `Subagent`, routing `Execution Profile`, work
   `Task`, execution attempt `AgentRun`, and side-effect boundary
   `ExternalAction`.
4. A future durable `AgentWorkItem` is the common unit of autonomous work and
   has a **1:N `AgentWorkItem` → `AgentRun`** attempt relation.  The common
   `WorkSource`, `ExecutionAdapter`, and `AgentWorkCoordinator` are shared by
   Company work, MediaOps, the existing `agent_harness`, and future Apps.  None
   of those runtime tables/coordinator are implemented in WS01.
5. Effective Agent authority is an intersection of independently owned ceilings
   and scopes; no prompt, model output, Character field, Persona text, Team ID,
   Execution Profile, or client payload can grant authority.
6. Enterprise remains fail-closed.  New company/autonomy capabilities are
   feature-gated and inert by default; existing manual MediaOps, EngagementOps,
   Operations, and `code_agent` behavior remain available.

## Organization and Space boundary

### Deployment singleton

`Organization` is a deployment-wide singleton settings/identity model:

* stable UUID primary identity;
* fixed singleton key (canonical value `installation`), enforced with a
  uniqueness constraint and a fixed-value check constraint;
* display/company settings plus bounded, versioned organization-policy and
  budget-policy structures needed by the authority resolver;
* no `space_id`, no `CompanyProfile.space_id`, no per-Space company row, and no
  tenant selector; existing Space/Project/Task/Docs/AgentRun rows do not gain
  `organization_id` tenancy plumbing merely to represent this singleton; and
* a transaction-safe get-or-create/bootstrap operation that converges under
  concurrent first use to exactly one row.

The application bootstraps the row before first use.  Reads return a safe DTO;
secrets and provider credentials are never projected.  Human/admin mutation is
authorized by the existing authentication conventions.  An arbitrary,
unvalidated policy blob is not a permission source: fields are closed,
bounded, validated, and versionable.

`Space` is the sole organization-unit boundary above Projects.  No
`CompanyDepartment` (or equivalent duplicate hierarchy) is introduced.  Agent
space assignments carry an explicit assignment kind (`primary`, `secondary`,
`supporting`, or the repository-compatible closed equivalent), dates/state,
role label, and a bounded policy ceiling.  Space membership alone never grants
write access to Projects in that Space.

## Canonical identity and relation model

The following meanings are normative:

| Concept | Canonical meaning | Authority consequence |
| --- | --- | --- |
| **User** | Authenticated human account | Human actor and grantor/approver where required |
| **Agent** | Persistent autonomous worker identity | Never a User substitute; has its own UUID/lifecycle |
| **Character** | ECC avatar, voice, conversational presentation | Optional Agent link is presentation-only |
| **Persona** | MediaOps brand, publication identity, public personality/content strategy | Not an Agent; existing owner and Project ACL remain authoritative |
| **Agent Team** | Execution topology and capability declaration | Declaration/ceiling only, not an authority grant |
| **Subagent** | Execution-time specialist role | Not a persistent employee identity |
| **Execution Profile** | Provider/model/effort route | Routing metadata, not authority |
| **Task** | Work to be done | Existing Task permissions remain authoritative |
| **AgentRun** | One execution attempt and its audit evidence | May pin one immutable AgentRevision |
| **ExternalAction** | Governed side-effect proposal/execution boundary | Human approval policy remains mandatory |

### Agent and revisions

`Agent` has a UUID identity and a closed lifecycle (`draft`, `active`,
`paused`, `retired`, or the repository-compatible equivalent).  Creation is
idempotent using the repository's established create-content hash pattern and
does not add a human-owner semantic or scheduler/lease fields.  An optional FK
to ECC `Character` is presentation-only; `Character.system_prompt`,
`allowed_tools`, and `model` cannot affect authority.

`AgentRevision` is immutable, monotonically versioned per Agent, and uniquely
identified by `(agent_id, version)` and `(agent_id, idempotency_key)`.  Its
bounded content includes display name, mission, responsibility summary,
operational instructions, canonical Agent Team and Execution Profile
identifiers, allowed Subagent identifiers, capability ceiling validated against
the existing Agent Team capability catalog, wake-policy metadata, budget and
concurrency policies, content hash, idempotency key, version, and human audit
creator/timestamp.  Exact idempotent replay returns the existing revision;
conflicting replay fails.  A safe projection omits provider configuration and
credential material.  Future `AgentRun` rows pin an exact immutable revision.

### Optional organization profile

`AgentOrganizationProfile` is an optional one-to-one employment/company
extension (`agent_id` is both PK and FK).  It contains job title,
responsibility summary, primary Space, manager User XOR manager Agent, closed
autonomy level, bounded company permission ceiling, employment state, and audit
timestamps.  Generic Media/App/code Agents do not require this profile; it does
not duplicate Space or create a department hierarchy.

### Explicit assignments and grants

All relations below are additive durable models with explicit FKs, deliberate
on-delete behavior, bounded enums/policies, active dates/state, and indexes for
lookups.  They remain separate from human polymorphic tables:

* **`AgentSpaceAssignment`** — Agent → existing Space; closed assignment kind,
  role label, bounded policy ceiling, dates/state, and uniqueness against
  duplicate active equivalent assignments.  It does not grant Project write.
* **`AgentProjectGrant`** — Agent → existing Project; role and permissions use
  the existing `ProjectMember` permission vocabulary, with fail-closed
  validation.  It is granted by a real human/admin and is separate from
  `ProjectMember`; Agent IDs never enter `ProjectMember.user_id`.
* **`AgentTaskAssignment`** — Agent → existing Task; closed role (`owner`,
  `executor`, `reviewer`, `observer`, or equivalent), human grantor, active
  state/timestamps.  It is separate from `TaskAssignee`; Agent IDs never enter
  `TaskAssignee.user_id`.
* **`PersonaOperatorAssignment`** — Agent → MediaOps Persona; closed roles
  covering operator/strategist/researcher/creator/analyst/publisher (or a
  compatible set), optional primary flag, bounded capability ceiling, human
  grantor, and active state/timestamps.  Persona owner and Project ACL checks
  still apply; assignment cannot bypass them.

The existing MediaOps sources of truth remain unchanged: Persona and
PersonaRevision, resources and PlatformAccount/credential vault, research and
evidence, editorial/content/variants/QA/rights, CreativeRecipe/
GenerationPlan, GenerationRunIntent/GenerationRun, calendar/metrics/revenue/
experiments, LearningProposal, and ExternalAction approval/attempt/receipt.
`GenerationRunIntent` remains the Media provider-call duplicate fence.

### Target relation graph

The additive identity graph is:

```text
Organization (exactly one per deployment)
    │ policy / budget ceiling
    └── Agent (1) ──< AgentRevision (immutable, 1:N)
             │  optional Character FK (presentation only)
             ├──< AgentOrganizationProfile (optional 1:1)
             ├──< AgentSpaceAssignment >── Space ──< Project ──< Task
             ├──< AgentProjectGrant >──── Project
             ├──< AgentTaskAssignment >── Task
             └──< PersonaOperatorAssignment >── Persona (MediaOps brand)

Future WS02:
AgentWorkItem (1) ──< AgentRun (N attempts; exact AgentRevision pin)
```

`AgentRun` remains additive: existing human/conversation creation is valid with
new nullable fields null.  New fields may include `agent_id`,
`agent_revision_id`, `task_id`, `acting_subagent_id`,
`previous_attempt_run_id` where compatible with current parent/root semantics,
a bounded resolved execution-manifest snapshot, and its hash.  No
`work_item_id` is added until WS02.  Existing `user_id` retains human meaning;
an Agent ID is never written there.  Existing events/tool evidence remain
intact and projections are redacted of credentials, raw secrets, and
unrestricted environment data.  History indexes cover Agent, revision, Task,
and state queries.

`ExternalAction` may receive nullable `origin_agent_id` and
`origin_agent_run_id`; `origin_work_item_id` is deferred to WS02.  Current
approval hash/version, immutable Attempt/Receipt, and provider boundaries do
not change.  An Agent requester cannot approve its own action.

## ActorPrincipal and authority intersection

WS01 introduces a service-layer immutable `ActorPrincipal` value object (not a
universal principal table) with exactly these actor kinds:

* `human` resolves to an authenticated User;
* `agent` resolves to an active Agent (and, for execution, its exact pinned
  AgentRevision); and
* `service` resolves through a stable server-owned service key.

Malformed, unknown, inactive, retired, or revoked actors fail closed.  No
synthetic User row is created, no Agent ID is accepted in historical `user_id`
columns, and conversion helpers are callable only at trusted server
boundaries.  New durable actor-bearing rows use explicit
`actor_user_id`/`actor_agent_id`/`actor_service_key` fields with XOR/check
constraints where referential integrity is required.  Historical actor tables
are not wholesale polymorphized in WS01.  Safe actor/audit projections expose
kind and bounded display data only.

The reusable effective-authority contract is an intersection, never a union or
escalation:

```text
Organization policy
∩ active AgentSpaceAssignment / Space policy
∩ active AgentProjectGrant
∩ AgentOrganizationProfile ceiling (when present)
∩ exact AgentRevision capability ceiling
∩ Agent Team / Subagent capability declaration
∩ ToolPolicy
∩ HarnessExecutionScope / AgentRunScope
∩ ExternalAction approval policy
```

The resolver implements the portions with existing source models in WS01:
active Agent and exact revision, Organization policy, active Space assignment,
Project grant, optional company-profile ceiling, canonical capability
validation, Persona operator assignment, and a safe decision/result object with
bounded deny reasons suitable for audit.  Existing Project permission helpers
remain the vocabulary and authority for human `ProjectMember`; Agent grants
remain a separate relation.  Space membership cannot imply Project write.

No authority is inferred from Character fields, instructions/prompts, Persona
text, Team IDs, Execution Profiles, model output, or request JSON.  An Agent
cannot grant or activate itself.  Approval remains human-only where current
MediaOps contracts require it.

## Common runtime convergence (future)

There is one future autonomous runtime, not a Company-specific runtime.  WS02
introduces the generic durable `AgentWorkItem` and its event/coordination
contract; `WorkSource` adapters describe where work originates, and
`ExecutionAdapter` binds an item to a runtime such as the existing
`agent_harness`.  One `AgentWorkCoordinator` will own dispatch, claims, leases,
retry/recovery, and budget/concurrency controls for Company work, MediaOps,
code-oriented harness work, and future Apps.

The current `src/agent_harness` runners, workspace management, workflow
rendering, privacy restrictions, and Enterprise sandbox behavior are retained
as future execution-adapter assets.  Its process-local scheduling/claim/retry
authority converges to the common durable runtime in WS03, after WS02 exists;
WS01 makes only compatibility changes required by generic identity types.

MediaOps remains the domain source of truth.  Future Media adapters register
Media WorkSources and use existing Persona, research, editorial, generation,
rights, credential, approval, and receipt contracts; they do not rewrite
MediaOps.  `PersonaOperatorAssignment` is the only required Media bridge in
WS01.

## Operations Command Center target (WS05)

WS01 does not change the bare `/operations` route or existing Operations and
MediaOps UI/E2E contracts.  WS05 will migrate the route from Persona-first to an
Overview while preserving Persona at its explicit tab.  The target map is:

| Route | Target surface | WS01 behavior |
| --- | --- | --- |
| `/operations` | Overview (health, pending work, policy/authority summary) | Keep current Persona-first route |
| `/operations?tab=personas` | Media Persona/brand operations | Existing Persona contract unchanged |
| `/operations?tab=agents` | Agent identities, revisions, assignments, authority inspection | No page in WS01 |
| `/operations?tab=work` | AgentWorkItem queue/history and run attempts | No page in WS01; WS02 runtime required |
| `/operations?tab=activity` | bounded AgentRun/ExternalAction activity and audit | No page in WS01 |

The future UI must use Persona/Brand terminology for Media identity and must
not conflate Persona with Agent or ECC Character.

## Enterprise and feature boundary

Repository-conventional flags are reserved with these meanings:

* `autonomous_agent_runtime` gates the future common coordinator;
* `virtual_company` gates company profile/assignment management APIs and future
  company UI; and
* `media_operations_autonomy` gates future Media WorkSource registration, not
  existing manual MediaOps APIs.

All new autonomous/company behavior is disabled by default unless an existing
profile convention explicitly provides a safe rollout.  Enterprise defaults
`virtual_company=false` and `media_operations_autonomy=false`; invalid feature
dependencies fail closed, and Enterprise cannot override a profile restriction
to weaken it.  No worker starts in WS01 regardless of flag values.  The
Organization/Agent foundation may exist inertly for later code-agent or Media
features.  The existing `code_agent` feature is not renamed or reused as
`virtual_company`, and setting `virtual_company=false` does not disable existing
MediaOps, EngagementOps, Operations, or code-agent behavior.

Enterprise execution-scope issuance remains server-issued and trusted.  An
Agent `ActorPrincipal` is explicit and follows the current User path without
weakening path/symlink/junction, network, environment, resource, Project, App,
or sandbox restrictions.  A client or model cannot manufacture authority, and
the existence of an Agent never enables new native-host execution.  If full
runtime binding is deferred, the typed scope/input contract and focused tests
are the WS01 boundary; WS02/WS03 own continuation.

## API and migration contract

Repository-conventional backend services/API surfaces expose safe,
bounded DTOs for:

* singleton Organization get/update;
* Agent create/list/get/state transition;
* immutable AgentRevision create/list/get;
* AgentOrganizationProfile get/update;
* Space assignments, Project grants, Task assignments, and Persona operator
  assignments; and
* administrator-only effective-authority inspection.

Mutations require a real authorized human/admin.  Agents cannot self-grant,
self-activate, or invoke broad administrative tools.  Idempotency and
optimistic version checks follow repository patterns; request bodies and
policies are bounded.  DTOs exclude credentials, hidden provider config, raw
filesystem paths, and secret-bearing metadata.  Additive migrations preserve
existing rows and nullable/non-Agent paths, keep Alembic at one head, support
repository PostgreSQL/SQLite rules, define deliberate FK deletion behavior,
and regenerate mirrored OpenAPI/generated types when contracts change.

Media terminology remains compatible: existing `MediaCharacter` frontend
aliases are compatibility names for Media Persona; ECC `Character` remains the
presentation model; generic `Agent` is the worker identity.  No broad rename of
Media tables, routes, generated types, or UI components is part of WS01.

## Workstream sequence and boundaries

| Workstream | Contract and ownership | Explicit non-goal |
| --- | --- | --- |
| **WS01** | Organization singleton, generic Agent/AgentRevision, optional company profile, explicit assignments/grants, ActorPrincipal, authority resolver, additive AgentRun/ExternalAction links, feature boundary, typed Enterprise scope compatibility, and this ADR | No worker, scheduler, queue, coordinator, WorkItem/Event, autonomous Task/Media execution, provider submission, or Command Center UI |
| **WS02** | Introduce durable `AgentWorkItem`/`AgentWorkEvent`, generic WorkSource/ExecutionAdapter contracts, common AgentWorkCoordinator, claim/lease/retry/recovery and future run/action origin links | Do not create a Company-specific runtime; do not duplicate MediaOps or harness scheduler semantics |
| **WS03** | Converge existing `agent_harness` scheduling/claims/retry/restart behavior onto the WS02 common durable runtime while retaining its sandbox/workspace/privacy assets | No parallel Company scheduler/lease/retry subsystem |
| **WS04** | Register MediaOps WorkSources and ExecutionAdapters against Persona/operator assignments and existing provider/approval/receipt contracts | No rewrite of MediaOps source tables or bypass of human-only boundaries |
| **WS05** | Build Operations Command Center target route/tabs and migrate bare `/operations` to Overview, preserving `/operations?tab=personas` | No premature UI migration in WS01 |
| **WS06** | Enterprise/runtime hardening, profile enforcement, scope/restart/recovery hardening, and production rollout controls | Never weaken Enterprise fail-closed restrictions |

## Explicit WS02 carryover contract (named, not implemented here)

WS02 must define and test the following names and behavior; WS01 only reserves
their links and semantics:

* `AgentWorkItem` and `AgentWorkEvent`;
* `WorkSource` and `ExecutionAdapter`;
* `AgentWorkCoordinator`;
* atomic claim;
* tokenized lease and renewal;
* retry/dead-letter;
* restart recovery;
* concurrency/budget reservation;
* loop prevention;
* Heartbeat wake integration;
* future `AgentRun.work_item_id`; and
* future `ExternalAction.origin_work_item_id`.

The `AgentWorkItem` → `AgentRun` relation is one-to-many: one durable work item
may have multiple immutable attempts, each pinning the exact AgentRevision and
recording bounded execution evidence.  No WS01 API or migration may invent a
surrogate `work_item_id`.

## Rejected designs

* **Company-specific identity/runtime** (`CompanyAgent`,
  `CompanyAgentDispatch`, `CompanyAgentQueue`, `CompanyOrchestrator`,
  `CompanyAgentScheduler`, `CompanyAgentLease`, or `CompanyAgentRetry`) —
  rejected in favor of one generic Agent identity and one future common runtime.
* **One Company per Space / `CompanyDepartment`** — rejected because Space is
  already the canonical organization-unit boundary; a deployment has one
  Organization singleton.
* **Agent as User or synthetic User rows** — rejected; human authentication and
  Agent identity must remain distinguishable, and historical `user_id` meaning
  must not change.
* **Polymorphic rewrites of `ProjectMember`, `TaskAssignee`, Docs actors, or
  App grants** — rejected for WS01; explicit Agent relations preserve current
  ACL and migration compatibility.
* **Authority from Character/Persona text, prompts, model output, Team IDs,
  Execution Profiles, or client JSON** — rejected; authority is resolved from
  trusted, intersected policy and scope sources only.
* **Universal Principal table or unbounded metadata policy** — rejected for
  WS01; use typed `ActorPrincipal`, explicit actor columns, and bounded closed
  policy fields.
* **Second scheduler or early WorkItem/coordinator implementation** — rejected;
  WS02 owns the common durable runtime and WS03 owns harness convergence.
* **MediaOps rewrite or Persona=Agent conflation** — rejected; MediaOps domain
  contracts remain authoritative and `PersonaOperatorAssignment` is the narrow
  bridge.
* **Early Operations route/UI migration or Enterprise relaxation** — rejected;
  WS05 and WS06 own those changes and existing contracts remain intact in WS01.

## Consequences and rollback

The additive graph permits a worker to be addressed, version-pinned, assigned,
and audited without inventing tenancy or weakening human ACLs.  Authority is
explainable as a bounded intersection and can be denied with safe audit
reasons.  Existing conversation AgentRuns, ExternalActions, MediaOps, Spaces,
Projects, and human assignment paths remain compatible.

Rollout is controlled by the three feature flags and profile defaults above;
disabling a flag stops new autonomous/company behavior without deleting
Organization, Agent, revision, or assignment history.  Additive migrations
retain old nullable paths and follow the repository's one-head downgrade
policy.  Any runtime, provider, graph, event-bus, or Operations expansion not
listed here requires a separate architecture and rollback decision.
