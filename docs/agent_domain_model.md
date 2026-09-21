# Agent domain model

**Scope:** WS01 identity/authority foundation and the WS02--WS06 hand-off
contract.  This is a source-of-truth map, not a promise that every future
runtime component is enabled in every profile.

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
  +-- optional presentation link -> ECC Character

AgentWorkItem (WS02 durable work unit)
  +-- AgentRun (1:N immutable attempts; exact AgentRevision pin)
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
| `AgentRevision` | Versioned definition: mission, team/profile identifiers, bounded capability ceiling and policies | Text, model choice, or a Team ID is not an authority grant |
| `Character` | ECC presentation/avatar/voice model | Prompt, tools, or model fields cannot grant Agent authority |
| `Persona` | MediaOps public/brand identity | A Persona assignment does not bypass its owner or Project ACL |
| `AgentOrganizationProfile` | Optional job title, manager, employment and company ceiling | It does not create another department hierarchy |
| `AgentSpaceAssignment` | Explicit Agent membership in an existing Space | It does not create a Project grant |
| `AgentProjectGrant` | Explicit Agent Project ACL using the repository permission vocabulary | It is separate from `ProjectMember`; Agent IDs never enter `ProjectMember.user_id` |
| `AgentTaskAssignment` | Explicit Agent-to-Task role (`owner`, `executor`, `reviewer`, `observer`) | It is separate from `TaskAssignee.user_id` |
| `PersonaOperatorAssignment` | Explicit Agent-to-Persona role and optional capability ceiling | It does not approve or publish by itself |
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
mission/instructions, canonical Agent Team and Execution Profile identifiers,
allowed Subagent identifiers, capability ceiling, wake/budget/concurrency
policies, and a SHA-256 content hash.  An exact idempotent replay returns the
existing revision; a conflicting replay is rejected.  Future execution must
pin the exact revision rather than silently following “latest”.

Agent state transitions and revision creation are human-admin operations.
`retired` is terminal for new revisions and execution; `paused`/inactive
identities are denied by the authority resolver.  Deactivation does not erase
history.

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
