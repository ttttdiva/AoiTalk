# Operations Command Center contract

Operations combines a company-wide read-only command-center projection with
dedicated domain management panels. The bare route is the Overview; Media
Persona remains an explicit tab. AI employee management is documented in
[AI employee platform](ai_employee_platform.md). The command-center API itself
remains read-only and stores no queue, approval or domain state.

## Route map

| URL | Surface | Mutating? |
| --- | --- | --- |
| `/operations` | Overview / Command Center | No; read-only projection |
| `/operations?tab=agents` | AI employee identity, role revisions, assignments, automation, integration, phone and activity | Dedicated identity/rule/policy/credential/telephony commands; projection remains read-only |
| `/operations?tab=work` | Generic AgentWorkItems and latest attempts | No |
| `/operations?tab=activity` | Cross-ledger activity timeline | No |
| `/operations?tab=personas` | Existing Media Persona/brand surface | Existing domain commands only |
| `/operations?tab=research` | Existing Media research detail | Existing domain commands only |
| `/operations?tab=automation` | MediaOps Automation programs, immutable revisions and Daily Board | AoiTalk-owned Theme/Research/Planning/Review orchestration; Generation Studio remains execution owner |
| `/operations?tab=generation` | Existing Generation Studio detail | Existing domain commands only |
| `/operations?tab=pipeline` | Content variant/QA/Rights pipeline | Existing domain commands only |
| `/operations?tab=calendar` | Due/review/publication calendar | Existing domain commands only |
| `/operations?tab=results` | Metrics/revenue/experiments/results | Existing domain commands only |
| `/operations?tab=connections` | EngagementOps connections | Existing domain commands only |
| `/operations?tab=opportunities` | EngagementOps opportunities | Existing domain commands only |
| `/operations?tab=actions` | Approvals / Actions | Existing human approval boundary |

Unknown tabs must fail to the Overview rather than expose an unbounded route.
The route does not add a Company selector: one deployment has one Organization;
filters are All company, Space, Project, Agent, domain, state and time range.

## Projection contract

The backend command-center projection should be ACL-scoped and bounded.  It
may join/read the following existing ledgers:

* singleton Organization summary and feature/runtime status;
* Agent identity/state, Organization profile and safe authority ceiling;
* current `AgentWorkItem` rows and latest `AgentRun` attempts;
* Spaces, Projects, Tasks and their ACL-scoped names/status;
* approval queue, `ExternalAction`, Attempt/Receipt and uncertain outcomes;
* Heartbeat due/schedule history;
* artifacts and evidence metadata;
* MediaOps research/generation/Persona status;
* activity events listed below.

It must not copy mutable source payloads into a second operational table or
return rows the authenticated operator cannot read.  Work/Action rows without
a Project must be explicitly classified as safe global records before a
non-admin can see them; `project_id IS NULL` is not an implicit ACL grant.  A
missing optional source returns an empty section with a truthful status, not an
invented success.

## Overview minimum

The Overview presents these safe summary counters:

* Active Agents, Working, Idle, Blocked, Awaiting Approval, Uncertain and
  Failed/Stale;
* budget used/limit/percentage (or `—` when no budget source exists);
* current work with Agent, Space, Project, Task/Persona/source, latest
  AgentRun, elapsed time, lease health and attempt;
* Requires attention: approvals, blockers, uncertain external results,
  exhausted retries, stale leases, permission revocations and budget limits;
* supporting Project health, schedules/due work, recent artifacts/evidence,
  MediaOps status and runtime usage.

Counts are projections, not write authority.  “Working” must not be inferred
from a process-local worker map when a durable WorkItem row exists.

## Agents view

Expose only safe projections of:

* Agent UUID/display name/lifecycle, job title and primary Space;
* explicit Project grants, Persona assignments and optional Character link;
* current work, latest AgentRun and bounded run history;
* Agent Team, Execution Profile, effective capability ceiling, budget and
  concurrency summaries;
* blockers/failures and bounded deny reasons.

Never expose credential material, raw prompts requiring protection, hidden
filesystem paths, model/provider secrets, lease tokens or unrestricted policy
JSON.  Links go to the canonical Agent, Task, Project or Persona screen.

## Work view

Display generic WorkItems across internal/company, MediaOps, code-agent and App
domains.  Required columns/filters are source/domain, Space, Project, Agent,
Task/Persona/App, state, priority, `not_before`/deadline, lease health,
attempts, approval state and budget reservation.  Each row links back to its
canonical source and then to AgentRun/evidence/tool calls where ACL permits.

Work state labels retain their distinct meanings (`pending`, `claimed`,
`running`, `retry_wait`, `blocked`, `awaiting_approval`, `uncertain`,
`succeeded`, `failed`, `cancelled`, `dead_letter`).  An uncertain or stale row
must remain visible until reconciled; it must not be collapsed into “failed”.

## Activity view and causal links

Activity is a timeline projection over canonical ledgers, including:

* `AgentWorkEvent` and `AgentRunEvent`/`AgentRunToolCall`;
* existing `TaskActivity` and Heartbeat run history;
* `ExternalAction` Attempt/Receipt and approval invalidation;
* Media research/generation evidence, metrics and learning review events;
* artifact/evidence records and permission/lease/recovery events.

Each event has a bounded ID, timestamp, actor kind, domain/state and safe
detail.  Causal links follow Organization → Space → Project → Agent →
WorkItem → AgentRun → evidence/tool calls/ExternalAction.  Raw provider
responses, credentials, prompts, shell output and hidden paths are not activity
payloads.

## Filters and deep links

Filters are persisted in the query string so reload/back/deep-link behavior is
deterministic:

```text
scope=all|space|project|agent
space_id=<id>   project_id=<id>   agent_id=<id>
domain=internal|media|code|apps
state=<work-state>   range=24h|7d|30d
```

The server must enforce the same ACL scope represented by these values; a
query parameter is not authorization.  Invalid IDs/unknown states return an
empty safe projection or a bounded validation error, never all-company data.

## Existing surfaces preserved

Persona is a public Media identity and remains addressable at
`tab=personas`; it must not be relabeled as an AI employee.  Existing
Research/Generation/Pipeline/Calendar/Results and EngagementOps tabs remain
functional when autonomous flags are disabled.  Approvals/Actions remain a
human command surface, not an Agent self-approval control.

## Current implementation and evidence

The Web client now contains `OperationsCommandCenter` with Overview, Agents,
Work and Activity views, URL-persisted filters, safe normalization/redaction,
canonical links and a fallback from `/operations/command-center` to the
existing `/operations/overview` projection.  Navigation places the bare route
on Overview and retains MediaOps/EngagementOps deep links.  FastAPI now exposes
`GET /api/operations/command-center` backed by
`OperationsCommandCenterService`; it composes ACL-scoped Project/Space/Agent/
WorkItem/AgentRun/ExternalAction projections and redacted activity metadata.
The current source intentionally returns empty schedules/artifacts/evidence /
Media status sections until those canonical joins are wired, so a 200 response
is not evidence that every overview card has live data.  End-to-end ACL scope
and data availability must still be verified against the running FastAPI +
Next.js + PostgreSQL path before claiming a live Command Center PASS.

The current source references are
`frontend/src/components/operations/operations-command-center.tsx`,
`operations-workspace.tsx`, `operations-workspace-navigation.tsx`,
`src/services/operations_command_center.py`, `src/api/operations_routes.py`,
`src/services/operations_service.py`, `src/services/work_intelligence.py`,
and the existing Operations/MediaOps route modules.

## Acceptance evidence

**Verified (source; existing tests where named):** component route/filter
contracts and safe projection normalizers, existing Operations ACL/approval
tests, MediaOps tab tests, and generated API contracts where applicable.

**Unverified:** production-sized ACL joins (especially non-admin Agent/Task/
Space filtering), all-company/Space/Project/Agent filtering against real data,
the currently empty schedules/artifacts/evidence/Media panels and the static
Organization summary fallback, browser reload/deep-link with live AgentWork
rows, console/network cleanliness in every tab, and performance under a large
event ledger.  Record those observations in
`docs/autonomous_agent_acceptance_matrix.md`; do not infer them from a static
render or unit test.
