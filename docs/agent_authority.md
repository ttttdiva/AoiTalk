# Agent authority and actor model

This document describes the fail-closed authority boundary for a generic
Agent.  It separates a durable worker identity from the human and service
principals that operate the application, and it records which parts are
implemented in WS01 versus deferred to the common runtime.

## Non-negotiable rules

1. An Agent has its own UUID and lifecycle.  It is never written into a
   historical `user_id` column and never requires a synthetic `User` row.
2. A request body, prompt, model output, ECC Character field, Media Persona
   text, Team ID, Execution Profile, or client-supplied role is not an authority
   source.
3. Every important decision reads current assignment/policy rows.  A stale
   worker snapshot is not a grant and must be revalidated before mutation.
4. Effective authority is an **intersection** of independent ceilings and
   scopes; a missing or malformed input denies rather than broadens access.
5. Human-only operations (administration, grant/revoke, approval and trusted
   external execution where required by the existing contract) remain
   human-only.

## Typed `ActorPrincipal`

`src/services/actor_principal.py` supplies an immutable value object with three
closed kinds:

| Kind | Identifier | Resolution |
| --- | --- | --- |
| `human` | UUID | Active `User` row |
| `agent` | UUID | Active `Agent` row; execution additionally pins a revision |
| `service` | server-owned key | One of `aoitalk.system`, `aoitalk.agent-harness`, `aoitalk.migrations`, or `aoitalk.media-adapter` |

Human/Agent UUID parsing is strict.  Unknown service keys, inactive Users or
Agents, inconsistent `is_agent` markers, and arbitrary mapping/object inputs
raise `ActorPrincipalError`.  `actor_fields()` projects explicit
`actor_user_id`/`actor_agent_id`/`actor_service_key` columns; it does not
rewrite historical actor tables.  `to_dict()` is safe for audit/UI use and
contains no ORM object or credential.

Use `from_authenticated_user()` at a trusted HTTP/session boundary and
`from_agent()` only after resolving the server-side Agent row.  Do not call
`ActorPrincipal.agent()` on an untrusted request payload and do not infer an
Agent from `user_id`.

## Effective authority intersection

`AgentAuthorityResolver.resolve()` evaluates the current database state and
returns an immutable `AuthorityDecision`:

```text
Organization policy/autonomy
  ∩ active Agent state and exact AgentRevision
  ∩ Agent Team capability declaration
  ∩ revision capability ceiling and allowed Subagents
  ∩ active Space assignment + Space policy ceiling
  ∩ active AgentProjectGrant + Project permission
  ∩ optional employment/profile ceiling
  ∩ explicit PersonaOperatorAssignment (for Media work)
  ∩ ToolPolicy capabilities
  ∩ HarnessExecutionScope/AgentRunScope capabilities
  ∩ ExternalAction approval policy
  ∩ runtime feature gate
```

The resolver uses the repository's normalized Project permission vocabulary
(`read`/`write`).  A Project-bound Persona causes the resolver to derive the
canonical Project/Space and still require the separate Agent Project grant and
Space assignment.  Space membership alone therefore cannot turn a
`project_read` decision into `project_write`.

An optional `revision_id` must belong to the Agent.  If it is omitted, the
resolver selects the latest revision by version; execution code should pass the
exact revision pinned to its run.  Missing revision, inactive/retired Agent,
missing Organization, disabled Organization autonomy, invalid capability,
expired/revoked assignment, Project mismatch, employment suspension, or
runtime-disabled write/external capability produces a denial reason.

Capability declarations are filtered against the canonical Agent Team catalog.
Unknown requested/tool/harness capabilities are explicit deny reasons.  Empty
or malformed policy values never become an allow-all value.  A Persona
operator assignment only narrows the effective set; it cannot provide an ACL
or approval by itself.

`AuthorityDecision` exposes `allowed`, IDs, the requested capability, the
effective capability set, and bounded `deny_reasons`/`reason`.  The result is
appropriate for audit and operator explanations, not as a bearer token.  The
resolver intentionally does not return prompts, provider configuration,
filesystem paths, raw policy blobs, or credentials.

## Administrative API boundary

The identity router exposes both
`GET /api/agents/{agent_id}/authority` and
`GET /api/agents/{agent_id}/effective-authority`.  Both require a
cookie-authenticated **admin human**; an Agent/service principal or non-admin
User receives 403.  Query values are bounded and capability lists are split
and normalized server-side.  The response is the safe decision projection.

Identity/assignment mutations likewise derive the authenticated admin User on
the server.  The client cannot choose `actor_user_id`, grant itself a Project,
activate itself, or approve an ExternalAction.  Company profile and assignment
routes additionally return 404 while `Features.virtual_company()` is false,
so disabled company surfaces are undiscoverable.  Organization and base Agent
identity routes remain additive WS01 compatibility surfaces; whether they are
advertised in a profile is a WS06 distribution decision (see the feature
matrix).

## Boundary with existing ACLs and side effects

The AI employee platform adds `external_action_propose` and `telephony_control`
as narrow, opt-in capability declarations. These require both company/runtime
features and explicit Organization runtime/external-action permission. Provider
execution separately validates exact human approval or the active immutable
action policy. Bounded policy execution never sets the legacy
`external_action_approved` flag or manufactures an approval row. See
[AI employee platform](ai_employee_platform.md).

* Existing human Project/Task/Docs/Files ACL services remain authoritative for
  human users.
* `AgentProjectGrant`, `AgentTaskAssignment`, `AgentSpaceAssignment`, and
  `PersonaOperatorAssignment` are explicit Agent relations; they do not insert
  Agent UUIDs into `ProjectMember.user_id` or `TaskAssignee.user_id`.
* External publication follows the existing
  `ExternalAction -> Approval -> Attempt -> Receipt` boundary.  An Agent may
  propose only when its effective capability allows it; human approval and
  reconciliation remain separate checks.
* The resolved AgentRevision and manifest are copied to an AgentRun for audit,
  but a manifest is evidence, not an authority grant.  Mutation paths must
  resolve current authority again rather than trusting that snapshot.

## Redaction and error handling

The identity models, identity routes, AgentRun DTOs and Operations projections
whitelist safe fields and remove keys containing secret/token/password/
credential/provider/model/path-like markers.  Errors are bounded (`invalid_*`,
`*_missing`, `*_denied`, `*_inactive`, `external_action_approval_required`,
etc.) and must not echo secret values or hidden host paths.

An unknown or malformed principal is a denial, not an anonymous fallback.  A
missing optional profile table is treated as no profile and never as an extra
grant.  This behavior is intentional during additive rolling migrations.

## Evidence and gaps

**Verified (source/tests):** typed principal validation and safe projection;
Agent/Revision/assignment service authorization; Space-versus-Project negative
permission behavior; exact revision binding and manifest redaction; admin-human
route checks; and the focused identity/AgentRun/Operations tests.  Primary
implementation references are `src/services/actor_principal.py`,
`src/services/agent_authority.py`, `src/services/agent_identity_service.py`,
`src/api/agent_identity_routes.py`, and
`tests/test_agent_identity_foundation.py`.

**Implemented additively:** WS02 supplies durable WorkItem claims, lease
fencing and run linkage in `src/services/agent_work_runtime.py`; the resolver
contract remains the authority input for adapters.  **Deferred:** complete
runtime capability issuance and pre-mutation authority hooks in every future
WorkSource (WS03--WS04).  The resolver itself still does not start a worker or
make a provider call.

**Unverified:** PostgreSQL row-lock behavior under concurrent revocation,
multi-process lease/race tests, real provider approval/execution, and a
production Enterprise identity distribution.  Do not convert unit-test PASS
into evidence for those deployments.
