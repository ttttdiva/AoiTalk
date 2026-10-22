# Agent platform

Agent is the product name for the existing `Agent` identity, with immutable
`AgentRevision` job definitions, organization profile, assignments, automation
rules, and action policies. See [feature terminology](feature_terminology.md)
for the naming contract; legacy `employee` identifiers remain compatible.
Manage Agents at `/operations?tab=agents`; the `agent` and `panel`
query parameters preserve the selected Agent and editor panel.

Execution Team, Subagent, and Execution Profile retain their execution roles. The
code-owned `employee` Execution Team is opt-in (`manual` activation). Its
`employee_operator` declares `external_action_propose` and `telephony_control`;
these declarations do not grant provider execution or substitute for approval.
The General Execution Team has not gained these capabilities.

## Model routing

The Agent is the model-routing origin. Work resolves
`exact AgentRevision -> Execution Team -> Execution Profile -> Subagent ->
provider/model/effort` through `src/services/agent_model_routing.py`; domain
services ask for a work role (`planner`, `explorer`, `researcher`, `writer`,
`data_worker`, `code_worker`, `general_worker`, `reviewer`, `operator`) and never
call `create_llm_client(config)` for Agent work. A per-Subagent override wins
over the Profile's Default Subagent Route; `manual` is the explicit standard
(Base) model. Invalid pins fail closed with `agent_route_*` codes.

At AgentWork start, effective role routes are snapshotted into the existing
`AgentRun.resolved_execution_manifest` and protected by
`AgentRun.execution_manifest_hash`. Retries reuse that manifest and never
re-resolve the current App Config. The snapshot contains only safe identity,
role, provider/model/effort and route-source fields; credentials, base URLs,
filesystem paths, prompts and reasoning are excluded.
An older v1 `AgentRun` without a route snapshot is not re-routed through the
current Settings; its retry stops with `legacy_route_snapshot_required` and
the WorkItem is terminally blocked for explicit operator requeue.

To think with a strong model and work with a local/OSS model, edit the Execution Team's
Execution Profile in Settings → 言語モデル → Execution Team: set the Default
Subagent Route to the strong model (for example Claude CLI / Codex CLI /
OpenAI) and add overrides for worker Subagents such as `code_implementer` or
`general_worker` pointing to `openai_compatible_local` or `ollama`. Select that
Execution Team/Profile and allow the Subagents in the Agent's 職務・モデル panel, which
shows a read-only preview of each Subagent's effective route. The Automation
classifier alone keeps the Profile default route because no Subagent acts.

### Built-in Execution Profile presets

Fresh canonical Execution Team settings seed these ten additive presets. A preset fixes
provider/model/effort routes; it does not change the Execution Team's capabilities or
silently re-route a pinned AgentWork.

Route shorthand below is exact: Bonsai = `openai_compatible_local` /
`bonsai-2-27b-ternary` / provider default; Sol = `codex-cli` /
`gpt-5.6-sol`; Luna = `codex-cli` / `gpt-5.6-luna`; API Luna = `openai` /
`gpt-5.6-luna`; Claude = `claude-cli` / `claude-opus-5-5`.

| Execution Team / preset | Purpose and effective provider mapping |
| --- | --- |
| General / `general-balanced` | Bonsai default; researcher override is Sol / `high`. |
| General / `general-api-luna` | API Luna default; researcher override is Sol / `high`, and `workspace_operator` is Codex Luna / `max`. |
| General / `general-local` | Bonsai for every General member. |
| General / `general-quality` | Sol / `high` for the General route. |
| App Development / `dev-codex-local` | Sol / `high` Head/default; Bonsai explorer, implementation Worker, and operator routes. |
| App Development / `dev-claude-local` | Claude / `high` Head/default; Bonsai explorer, implementation Worker, and operator routes. |
| App Development / `dev-codex-luna` | Sol / `high` Head and Reviewer; Luna / `max` explorer, Worker, and operator routes. |
| App Development / `dev-api-luna` | Sol / `high` Head and Reviewer; Codex Luna / `max` native code roles; API Luna / `max` only for API-capable `employee_operator` (never the code Worker). |
| App Development / `dev-local-only` | Bonsai for every App Development member. |
| App Development / `dev-premium` | Sol / `high` planner/default, Sol / `medium` implementer, Claude / `high` Reviewer, Luna / `max` explorer/operator. |

General `balanced` and `local` presets do not force a heavy planner/reviewer
workflow. `Free Team` remains the existing `routing-profile` compatibility
path; it is not a seeded Execution Team preset or a pool extension.
Bonsai routes stay inside the existing local runner's scoped workspace/tool
loop, and keep `effort_policy=default` because Bonsai has no portable effort
vocabulary; `max` or `high` is not fabricated for it.

Execution Team definitions may be shared by several Agents and by Chat; they
are not the Agent organization chart. Chat and Operations can reference the
same Execution Team/Profile but use different runners. In generic Operations delegation,
Subagent selection supplies a model route, not automatic inheritance of all
Subagent instructions and tools. See
[Agent delegation workflow](employee_delegation_workflow.md).

## Configuration and authority

Personal/custom deployments require both startup flags and a restart:

```text
FEATURE_VIRTUAL_COMPANY=true
FEATURE_AUTONOMOUS_AGENT_RUNTIME=true
```

Organization autonomy must be enabled, with `allow_agent_runtime` and
`allow_external_actions` explicitly true for the Agent capabilities. A
bounded automatic action also requires that organization policy does not force
human approval. Exact revision, Execution Team/Subagent ceiling, employment state,
Project grants and Space assignments are checked again at execution. Enterprise
continues to hard-deny company autonomy and voice regardless of stale flags.

Create a draft, save its role revision and scopes, then activate explicitly.
Role edits create new revisions. Existing work retains its revision pin; rule
bindings must be revised explicitly to select another Agent revision.
Activation requires a revision. State commands use compare-and-set, and the
Web editor sends `expected_state`; revision saves send the next expected version.

## Canonical state and execution

Implementation entrypoints:

| Responsibility | Source |
| --- | --- |
| Shared services and runtime registration | `src/services/ai_employee_platform.py`, `src/services/agent_work_registration.py` |
| Server API composition | `src/api/ai_employee_registration.py`, `src/api/server.py` |
| Rule management, event capture, discovery and evaluator | `src/services/agent_automation_{service,events,runtime,invoker}.py` |
| Registered actions, authorization and fenced execution | `src/services/integration_action_registry.py`, `agent_action_policy_service.py`, `external_action_execution_service.py`, `external_action_work_source.py`, `procurement_action.py` |
| Encrypted credentials | `src/services/integration_credential_vault_service.py`, `src/security/integration_credential_crypto.py` |
| Phone ingress and Live Voice bridge | `src/services/telephony_{service,provider,runtime}.py`, `src/services/live_voice_service.py` |
| Agent editor | `frontend/src/components/operations/agents/`, `frontend/src/lib/agent-employees-api.ts` |
| Persistence | `src/memory/models/{agent_automation,integration_credentials,telephony,operations}.py`, migrations `0002` and `0004` |
| Safe activity projection | `src/services/operations_employee_observability.py` |

```text
ConversationMessage (encrypted source)
  -> AgentAutomationEvent (immutable IDs and safe metadata)
  -> AgentWorkItem -> AgentRun
  -> ExternalAction
  -> exact human approval OR immutable bounded policy authorization
  -> ExternalActionAttempt -> ExternalActionReceipt / uncertain
```

`AgentWorkCoordinator` owns discovery, claims, leases, retry and settlement.
`AutomationRuleWorkSource` and `ExternalActionWorkSource` register on that same
coordinator. Event/discovery tables have no execution lease or retry state.
`ConversationDispatchOutbox` retains its separate conversation transport role.

Message hooks include the repository and the direct durable AgentRun dispatch
path. When Agent automation is enabled, the new message and event commit
together; an event insertion failure rolls back the message transaction.
Events omit message content and ciphertext. Execution rereads the canonical
message with current access checks. Default rules require human messages and
ignore automation causation, copied fork history, and non-human notifications.
Discovery uses fixed upper bounds per sweep so continuous rule/event arrivals
cannot starve transactions that commit late behind an earlier scan position.

Conditions are `always`, normalized keyword ANY/ALL, and semantic matching.
The semantic invoker uses the existing model manager, pinned Execution Team/Profile and
outbound privacy boundary with a tool-free structured response. Invalid output
fails closed. Input mapping is a closed scalar mapping; model output cannot
choose the provider, credentials, action type, URL or authority. The rule Test
button evaluates only and creates no event, work, external action or provider
submission.
Source conversation and Project privacy policies are bound for both runtime
evaluation and dry run. Unsupported model routes remain diagnosable as drafts
and cannot activate a semantic rule. The `employee` Execution Team's explicit `realtime` profile
selects the existing Live Voice model for phone reception without changing the
ordinary chat Main model.

## Management APIs

| Area | API |
| --- | --- |
| Identity, revisions and assignments | Existing `/api/agents/*` |
| Safe editor catalog | `GET /api/agents/catalog` |
| Rules, revisions, lifecycle and dry run | `/api/agent-automation/rules/*` |
| Immutable action policies | `/api/agent-action-policies/*` |
| Code-owned action catalog | `GET /api/integrations/actions` |
| Logical connections | Existing `/api/operations/connections/*` |
| Write-only credential lifecycle | `/api/integrations/connections/{id}/credential/*` |
| Phone routes, readiness, calls and signed ingress | `/api/telephony/*` |
| Read-only monitoring | `GET /api/operations/command-center` |

Administrative writes resolve an authenticated administrator human on the
server and recheck that User in the database. Agent IDs never enter human User
foreign keys. Bounded policy authorization does not create an
`ExternalActionApproval`. Existing manual attempt APIs remain human-only.

## Water replenishment

The registered `procurement.place_order` action has fixed or allow-listed
product and shipping references, integer quantity/price limits, fixed currency,
rate windows and semantic dedupe. A typical water policy uses a 24-hour dedupe
window. Different shortage messages can evaluate separately, but proposals for
the same connection/item/destination serialize before creating an executable
order. An unresolved uncertain action continues to block the same target after
the ordinary dedupe window expires.

The adapter prepares a fresh target-bound quote. A quote over the policy ceiling
blocks or creates a new exact human-approval version according to the selected
fallback. An approval cannot silently authorize a changed quote.

An Attempt is committed before submission. Only strong provider evidence can
mint a Receipt and a successful order state. Definitive pre-submit failure can
retry; a timeout or ambiguous result after possible submission is uncertain and
never blindly retried. Human reconciliation uses the existing Operations ledger.
Recovery classifies committed submissions before retry exhaustion or current
authority checks, settling Action, Attempt and execution WorkItem together as
uncertain. Human reconciliation validates historical submission evidence rather
than today's connection display name or rotated credential. Pending proposals
reserve rate capacity regardless of age; completed orders use Attempt submission
time for rate/dedupe windows.

No supplier endpoint, product, price or credential is supplied by default.
Production procurement is unavailable until a registered supplier adapter is
installed. The deterministic adapter lives in test support and enters the
isolated QA server through explicit constructor injection.

## Credential boundary

`ExternalConnection` contains only logical binding data. Generic credentials
use AES-256-GCM with AAD binding both credential UUID and connection UUID.
Safe responses contain status, revision and permitted capability metadata,
never plaintext, ciphertext, digest or key material. Upload, replace, verify,
disable and execution all enforce scope and current state. Replacing a secret
invalidates its prior verification; execution pins both revision and state hash.

The integration key provider follows the existing Media pattern under a
separate namespace (`AOITALK_INTEGRATION_CREDENTIAL_*`). Its default `local` key
uses the existing field-encryption data key. Versioned key commands and key IDs
support deployment-managed rotation. Preserve older keys while older ciphertext
still refers to them. Existing Media credentials remain unchanged.

Provider verification is code-owned. OpenAI model access verification alone
does not attest a reachable webhook, SIP trunk, or phone number. Removing a
constructor-injected test verifier makes its stored test verification unusable.

## Telephone reception

`TelephonyRoute` binds a server-configured route selector to an exact Agent
revision. `TelephonyCall` stores safe routing/session/run evidence. Ingress
verifies a bounded raw body before trusting the OpenAI incoming-call event.
Call and event IDs dedupe repeated delivery. Route state, feature gates, current
authority, working hours and provider prerequisites are checked before accept.

The session extends existing Live Voice sideband, turn, transcript and tool-call
machinery. It does not create a second speech/LLM stack or poll audio through
AgentWorkItems. Transfers accept only a configured `destination_key` and run
synchronously through the shared action authorization/Attempt/Receipt boundary.
Ambiguous controls become uncertain rather than repeated REFER/hangup requests.
Caller identities are masked/hashed; raw webhook headers and audio are not
persisted. Transcript retention follows canonical Conversation/Live Voice rules.

Phone numbers, carrier/PSTN trunks, public webhook configuration and safe live
test targets are external deployment prerequisites. They are not provisioned
by the local implementation or test harness.

## Migration and verification

The Agent platform migration is `20260908_0002`, following `20260908_0001`. It adds
automation/policy/credential/telephony state and extends the existing action
ledger. Later independent migrations may follow it; always inspect the actual
Alembic graph rather than resetting to the design packet's research revision.
`20260908_0004` adds bounded discovery sweep columns after the concurrent
Knowledge migration `0003`; it preserves all event/work/action evidence.
Incomplete legacy provider evidence is retained unchanged and explicitly
non-executable. Origin IDs retain their historical values after a referenced
run/work record is removed.
Immutable evidence is forward-only: downgrade requires restoring a verified
pre-upgrade backup. SQLite batch migration requires foreign keys disabled before
the migration transaction, re-enabled afterward, and `foreign_key_check`.

Core verification is in `test_ai_employee_models`, `test_agent_automation_*`,
`test_integration_action_execution`, `test_integration_credential_vault`,
`test_operations_employee_observability`, and `test_telephony_*`. Existing
identity/authority, Operations/Media, feature, conversation and Live Voice
regressions remain part of acceptance. Web component tests cover the Agent
editors. The isolated QA harness is `scripts/verification/ai_employee_qa.py`;
credentials come only from repo-root `.env.qa-login`.

Fresh verification evidence is maintained in the private development repository. Unit/mock evidence is not evidence of a real order or a live PSTN/SIP call.
