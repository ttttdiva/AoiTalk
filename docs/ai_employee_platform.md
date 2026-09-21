# AI employee platform

AI社員 is the Web product name for the existing `Agent` identity, an immutable
`AgentRevision`, organization profile, assignments, automation rules, and action
policies. Manage employees at `/operations?tab=agents`; the `agent` and `panel`
query parameters preserve the selected employee and editor panel.

Agent Team, Subagent, and Execution Profile retain their execution roles. The
code-owned `employee` Team is opt-in (`manual` activation). Its
`employee_operator` declares `external_action_propose` and `telephony_control`;
these declarations do not grant provider execution or substitute for approval.
The General Team has not gained these capabilities.

## Configuration and authority

Personal/custom deployments require both startup flags and a restart:

```text
FEATURE_VIRTUAL_COMPANY=true
FEATURE_AUTONOMOUS_AGENT_RUNTIME=true
```

Organization autonomy must be enabled, with `allow_agent_runtime` and
`allow_external_actions` explicitly true for the employee capabilities. A
bounded automatic action also requires that organization policy does not force
human approval. Exact revision, Team/Subagent ceiling, employment state,
Project grants and Space assignments are checked again at execution. Enterprise
continues to hard-deny company autonomy and voice regardless of stale flags.

Create a draft, save its role revision and scopes, then activate explicitly.
Role edits create new revisions. Existing work retains its revision pin; rule
bindings must be revised explicitly to select another employee revision.
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
| Employee editor | `frontend/src/components/operations/agents/`, `frontend/src/lib/agent-employees-api.ts` |
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
path. When employee automation is enabled, the new message and event commit
together; an event insertion failure rolls back the message transaction.
Events omit message content and ciphertext. Execution rereads the canonical
message with current access checks. Default rules require human messages and
ignore automation causation, copied fork history, and non-human notifications.
Discovery uses fixed upper bounds per sweep so continuous rule/event arrivals
cannot starve transactions that commit late behind an earlier scan position.

Conditions are `always`, normalized keyword ANY/ALL, and semantic matching.
The semantic invoker uses the existing model manager, pinned Team/Profile and
outbound privacy boundary with a tool-free structured response. Invalid output
fails closed. Input mapping is a closed scalar mapping; model output cannot
choose the provider, credentials, action type, URL or authority. The rule Test
button evaluates only and creates no event, work, external action or provider
submission.
Source conversation and Project privacy policies are bound for both runtime
evaluation and dry run. Unsupported model routes remain diagnosable as drafts
and cannot activate a semantic rule. The explicit Employee `realtime` profile
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

The employee migration is `20260908_0002`, following `20260908_0001`. It adds
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
regressions remain part of acceptance. Web component tests cover the employee
editors. The isolated QA harness is `scripts/verification/ai_employee_qa.py`;
credentials come only from repo-root `.env.qa-login`.

Fresh verification evidence is maintained in the private development repository. Unit/mock evidence is not evidence of a real order or a live PSTN/SIP call.
