# MediaOps autonomous integration and migration

MediaOps is a domain with its own source-of-truth ledgers.  Autonomous Agent
work may operate those ledgers through explicit bridges, but must not replace
Persona, PersonaRevision, research evidence, generation runs, rights/QA, or
publication approval with generic Agent tables.

## Terminology and source of truth

* **Persona / Brand / 発信人格** is the public Media identity.  It is not an
  AI employee and remains available for manual operation.
* **Agent** is the worker identity.  A `PersonaOperatorAssignment` explicitly
  binds an Agent to a Persona with a role (operator, strategist, researcher,
  creator, analyst or publisher) and optional capability ceiling.
* **ECC Character** is a presentation/avatar model.  A Character link on an
  Agent does not grant Media or Project authority.
* **PersonaRevision** and the existing policy/resource rows define the exact
  Media configuration used by a run.
* Research routines/evidence/candidates, editorial/content variants, QA/Rights,
  CreativeRecipe/GenerationPlan, GenerationRunIntent/GenerationRun,
  calendar/metrics/revenue/experiments, LearningProposal and
  ExternalAction approval/attempt/receipt remain canonical.

No Persona-to-Agent conversion, Persona owner removal, broad table rename, or
replacement of Media-specific runs with `AgentRun` is allowed.  An AgentRun
may link to a Media run for audit, but the Media ledger remains authoritative.

## Feature gates

`Features.media_operations_autonomy()` is the gate for autonomous Media
WorkSources.  It is false by default and is clamped false unless
`autonomous_agent_runtime` is enabled.  Enterprise hard-denies both flags even
when stale `FEATURE_*`, `AOITALK_PROFILE`, or legacy `AIVTUBER_ENV` values ask
for true.  When the gate is false:

* do not register Media WorkSources or dispatch autonomous Media Agents;
* keep Persona management, Research, Generation, Pipeline, Calendar, Results,
  EngagementOps and manual ExternalAction workflows functional;
* retain historical data, APIs and read-only projections;
* show a truthful manual/unavailable status rather than a fake provider
  success.

## Target WorkSource and adapter journeys

### Research

```text
Heartbeat/due routine
 -> inspect current ResearchRoutine and PersonaRevision
 -> materialize idempotent AgentWorkItem
 -> resolve active Persona operator Agent + Project/Space authority
 -> pin exact AgentRevision/PersonaRevision and create AgentRun
 -> bounded search/source tools
 -> MediaOperationsResearchService
 -> findings/evidence/candidates in Media ledgers
 -> settle (no publication side effect)
```

The adapter must preserve source URL/hash provenance, candidate idempotency,
expiry/promotion decisions, and the human editorial boundary.  Raw provider
responses, credentials, unrestricted prompts and hidden paths do not enter
generic WorkEvents.

### Generation

```text
GenerationPlan/eligible content
 -> AgentWorkItem + pinned revisions
 -> Media generation adapter
 -> MediaOperationsGenerationService
 -> GenerationRunIntent (submit fence)
 -> configured Generation Studio adapter
 -> GenerationRun/observations/opaque outputs
 -> settle
```

`GenerationRunIntent` is the external-submit duplicate fence.  Generic retry
must not create a second provider submission after a response loss or timeout;
the adapter reconciles the existing intent/run first.  Provider workflow graphs,
model/LoRA paths, credentials and charge controls stay behind the configured
Generation Studio boundary.  If Generation Studio is unavailable, the run is
blocked/failed honestly—there is no fake image/video output or provider receipt.

### Publication and other external actions

```text
Agent evaluates readiness
 -> create/propose existing ExternalAction
 -> AgentWorkItem = awaiting_approval
 -> human approval (Agent cannot self-approve)
 -> trusted execution boundary
 -> Attempt -> Receipt or uncertain reconciliation
 -> settle work item
```

Canonical payload/version/hash validation remains in `OperationsService`.
Approval is invalidated when the payload or pinned revision changes.  Manual
copy/download fallback remains supported, and provider writes are never made
as ordinary tests.  An uncertain external result is reconciled explicitly and
is never blind-retried.

### Metrics and learning

Scheduled/eligible metrics ingestion may become a WorkSource where supported,
but immutable metrics/revenue/experiment ledgers remain authoritative.
`LearningProposal` keeps evidence/window/confidence requirements and remains
pending human review; an Agent cannot directly mutate accepted Persona policy.

## Authority and ACL requirements

An Agent must have an active Persona operator assignment for a Persona-scoped
run.  If the Persona is Project-bound, the resolver additionally requires the
Agent Project grant, normalized permission and active Space assignment.
Persona assignment is not a Project ACL and does not bypass owner, Project,
Task, Docs or Files checks.  Authority is re-resolved before important Media
mutation so revocation/deactivation during a run blocks settlement.

Team/Subagent declarations, Execution Profile/provider choice, model output,
Persona text and request JSON only narrow or describe work; they cannot grant
publication, approval, credential or filesystem authority.  Human grantors and
approvers remain explicit actors.

## Credential and provenance boundary

PlatformAccount/credential-vault rows are separate from Persona and Agent
identity.  Credential storage uses the existing encrypted vault and safe DTOs;
ordinary Media rows/events/projections contain opaque account IDs and status,
never raw token/password/cookie/key material.  Provider identity verification
must use the built-in evidence marker and identity-match rules; a custom
adapter cannot manufacture `verified`.

Media artifacts and evidence keep immutable upstream revision/content hashes,
source URLs, rights/QA assessments, and opaque output references.  Generic
AgentWorkEvent metadata is a bounded index/link, not a second evidence store.

## Development contract for Media adapters

Each Media WorkSource/ExecutionAdapter must provide:

1. canonical source key and idempotent materialization;
2. Persona/operator, AgentRevision, Project/Space and feature-gate checks;
3. source/revision snapshot and provenance links;
4. no publication side effect for Research/learning paths;
5. GenerationRunIntent or ExternalAction fence for provider side effects;
6. transient/permanent/approval/uncertain classification and reconciliation;
7. redacted evidence/events and safe output references;
8. manual fallback and truthful unavailable-provider status;
9. focused duplicate/retry/revocation tests and a link to the canonical UI.

Do not call a provider directly from the generic coordinator, duplicate a
Media ledger in Operations, or treat a manual Attempt/Receipt as a provider
success.

## Current implementation and evidence

**Verified (source/tests for existing MediaOps):** MediaOps setup, Persona revisions/resources,
research candidate lifecycle, content variants, QA/Rights, metrics/experiments/
revenue, LearningProposal review, credential-vault redaction, and
ExternalAction approval/Attempt/Receipt.  See the focused suites listed in
`docs/media_operations_acceptance.md` and the Media service modules under
`src/services/`.

The focused common-runtime bridge suite
`tests/test_media_common_runtime.py` observed 13 passed for pinned Research
candidates, Generation uncertainty, proposal-only publication and normalized
outcomes.  This is local adapter evidence; it is not provider/network success.

`OperationsService.MEDIA_ADAPTER_STATUS` currently reports all six supported
platforms as manual with `provider_calls=false`.  Generation Studio and real
provider credentials are not exercised by unit tests.

**Implemented (WS02/WS04 source and startup registration):** the common runtime
provides the WorkSource/ExecutionAdapter protocol and typed AgentRun links.
`src/services/media_work_sources.py` now contains feature-gated Research,
Generation, Publication and Metrics/Learning projections, and
`src/services/media_execution_adapters.py` contains the corresponding thin
adapters.  The sources require an explicit active Persona operator and should
require a server-owned authority resolver in production; they materialize only
bounded identifiers, hashes and semantic hints.  Construction without a
resolver remains possible for compatibility, but discovery now fails closed
with `authority_unavailable` rather than treating absence as approval.
Application registration/startup keeps both source and adapter sets disabled
when `media_operations_autonomy` is false; local focused tests cover the
registration and disabled paths.

The current source/adapter keys are:

| WorkSource | `source_type` | Execution adapter | WorkSource capabilities |
| --- | --- | --- | --- |
| `MediaResearchWorkSource` | `media.research.routine` | `media.research` | `media.research`, `search` |
| `MediaGenerationWorkSource` | `media.generation.plan` | `media.generation` | `media.generation` |
| `MediaPublicationWorkSource` | `media.publication` | `media.publication` | `media.publication.propose` |
| `MediaMetricsWorkSource` (also learning) | `media.metrics` / `media.learning` | `media.metrics` / `media.learning` | `media.metrics` or `media.learning` |

The adapter declarations are intentionally narrower than some source-side
semantic labels: Research declares `("media", "web_read")`, while Generation,
Publication and Metrics/Learning declare `("media",)`.  The coordinator and
authority resolver intersect these declarations with the WorkItem's required
capabilities; neither list is an authority grant by itself.

The matching `Media*ExecutionAdapter` classes return bounded
`MediaExecutionOutcome` values and are intended to be registered only by the
common coordinator.  They do not claim, retry or settle WorkItems themselves.

**Unverified:** authorized research search, Generation Studio/GPU execution,
provider API publication, network rate limits, external uncertain-result
reconciliation, and a live Agent-to-Persona run against PostgreSQL.  No real
post, release, paid generation or credential mutation was performed as QA.

## 2026-09-17 Automation ownership and Generation Studio boundary

MediaOps Automation is owned by **41_AoiTalk**. AoiTalk owns what is made, why it is made, when it runs, research/planning/review, durable workflow state, and cross-system provenance. `73_ComfyUI-Workbench` remains the source of truth for how media is generated: Presets and immutable Preset revisions, Workflow Registry, canonical Run/Queue/Worker, ComfyUI execution, OutputVersion/CAS, Gallery/History, and generation-level provenance.

The durable AoiTalk control-plane models are deliberately small:

- `AutomationProgram`: stable identity, scope and enable/disable state.
- `AutomationProgramRevision`: immutable Trigger / Discovery / Research binding / Planning policy / Generation action / fallback / execution-mode snapshot.
- `AutomationRun`: state-machine owner and upper provenance. It links to the existing `ResearchRun`; it does **not** create a second Automation-specific Research ledger.

Automation execution modes are `research_only`, `draft`, `review_before_generate`, and `auto_generate`. Runtime states are `scheduled → theme_discovery → research → brief → concept_planning → prompt_planning`, followed by `waiting_review` or `generation_submitting → generation_running`, and terminal `complete` / `failed` or explicit `uncertain`.

Research reuses `ResearchRoutine`, `ResearchRun`, `ResearchFinding`, `ResearchFindingEvidence`, and `ResearchCandidate`. The Automation safe-fetch boundary accepts HTTP(S) only, resolves DNS before connecting, rejects private/loopback/link-local/reserved destinations, disables implicit redirects, revalidates redirects, bounds redirects/time/body size, limits content types, supports allow/deny domain lists and an in-process bounded cache. Search uses the existing `DeepResearchSearchClient`/Media research adapter rather than an Automation-only provider stack.

Generation Presets are never copied into AoiTalk. An Automation revision stores only a Generation Workspace binding, `preset_id`, and revision policy. Per-run prompt candidates are overlays and never mutate or create Preset revisions. For `latest_at_submission`, final resolve/pin happens inside Generation Studio's submission boundary. AoiTalk persists the returned `preset_revision_id`, revision number and checksum only after the external receipt is known.

`HttpGenerationStudioAdapter` is the single 41→73 HTTP boundary. It now provides safe Preset catalog reads and preset-centric semantic image submission in addition to the existing generation operations. AoiTalk writes a stable external idempotency key before the external call. A transport failure after submission becomes `uncertain`; normal resume does not blindly resubmit. Only the explicit uncertain-retry action may resend the same immutable request with the same key, allowing Generation Studio to return its already-created canonical Run.

Scheduled/autonomous execution does not introduce another queue. `MediaAutomationWorkSource` projects due internal/heartbeat Automation Programs into the existing `AgentWorkItem` runtime as `media.automation.program`; `MediaAutomationExecutionAdapter` calls the normal Automation domain. Windows Task Scheduler uses the same domain through `python scripts/aoi_automation.py run <program-id>` and derives a stable minute/hour/day occurrence key when one is not supplied.

The Operations UI exposes `/operations?tab=automation` with an Automation list/editor, immutable revision history, Run Now, enable/disable, duplicate, Research/Planning settings, Generation Studio workspace + Preset selector, revision policy, fallback controls and a Daily Board. The board follows Theme → Research Brief → structured candidates → optional review/edit/regenerate → generation receipt/output lineage. Preset authoring remains in Generation Studio.

Runtime acceptance on 2026-09-17 exercised the changed boundaries rather than only unit doubles. A live `research_only` Automation reused a temporary `ResearchRoutine`, queried DuckDuckGo through the existing search client, persisted an ordinary `ResearchRun` with five source-backed findings and produced a Research Brief with five source references; the QA routine/program were deleted afterward. A separate live `auto_generate` Automation created a temporary Generation Studio Preset through 73's public API, submitted through `HttpGenerationStudioAdapter`, observed `generation_running → complete`, persisted the exact returned Preset revision pin, and reconciled one Asset plus one OutputVersion from the canonical 73 Run. The temporary 41 control-plane rows and Preset were deleted after verification; the immutable Generation Studio Run remains normal generation history. The current 73 runtime had ComfyUI/RTX 5090 healthy but no executable local Comfy image selection (`No live checkpoint available`, Anima unavailable), so the successful cross-system image run used an available remote image selection and **does not** claim a Comfy-backed image GPU E2E.
