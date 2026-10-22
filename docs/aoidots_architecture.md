# AoiDots 恒久アーキテクチャ

この文書は、source、schema、focused test、限定 runtime readback で確認できる AoiDots の現在契約を記録する。f960 は過去の検証用 tree であり、現在の統合先は main の作業ツリーである。実 provider full flow、Native provider/device、APK の完了をこの設計文書だけでは主張しない。検証判定は aoidots_verification.md に分離する。

## 過去の f960 証跡境界

| 項目 | 値 |
| --- | --- |
| candidate tree | `f960a82ea9f6d34272f6625bea5b3b0532326b7f` |
| selected source root | `.local/aoidots-selected-source` |
| physical selected HEAD | `8a48aa6d627bc922ca2b820a377354dd4d051ed6` |
| materialization | 5059 candidate tree paths、159 cached diff paths、physical normalized diff 0、missing 0、selected extra 0 |
| f960 runtime manifest | `.local/development-resume-20261012/runtime-qa/runtime-source-manifest-f960.json`、manifest SHA-256 `7264bb8c1be9c1f73f539af10ba35be96ab1d62cdf059d869657c2504690e932`、aggregate `54ef12d71ecf74424c15af759aa4a132fafcbdf42e59d9c52e558f0fd028584d` |
| generated OpenAPI | `c27756741dafb0113a5b46c2ecfac50dea8ce04cbb6afc3c7b136bef62e32f30` |
| frontend generated types | `85cf8e93ca463edf3bab451d889ae18c72ecde672d54b2a2b097608d41c79c21` |
| mobile generated types | `44507c26500b270b3680e303d4b1631ffb2db8b67c2ae3517be0a9dfb273e1e6` |

The candidate, backend focused source, frontend test source, and mobile test source are separate historical identities. The hashes above do not identify the current integrated main source. The selected source was preserved at `.local/retired-worktrees/aoidots-selected-source-20261013-0933`; it is no longer an active worktree. Current verification is recorded in `aoidots_verification.md`.

## Root and worker identity

AoiDots uses the ordinary Agent, ConversationSession, AgentRun, AgentWorkItem, and message model. An owner-scoped `AgentPrivateRoot(purpose=aoidots)` points to one ordinary projectless root session and one ordinary root Agent. A worker is another ordinary session with its exact Agent and AgentRevision. `AgentDialogue` links the root and worker sessions, source anchors, delivery/input rows, control revision, and finite budget.

The root remains projectless. `context_project_hint_id` is a read-only UI/selection hint; it does not mutate the root `project_id` or grant authority. An explicit worker Project/Task is checked against Human ACL and the worker assignment. Client supplied Agent, revision, Project, provider, workspace, approval, or prompt metadata does not create authority.

The source role is typed and server-derived. A Human source anchor must be an owner-authored `user` message in the root session with the matching Human Run/Work, capability/authority digests, control revision, and deadline. An Agent-sender source is accepted only for a server-created root-Agent relay/control or worker-Agent delivery/result with the exact corresponding Agent/Revision/session binding, canonical Human ancestry, delivery kind, and lease. An arbitrary or unbound Agent message, a Human-labeled message with an Agent sender, or forged sender/ancestry is rejected before provider entry. The canonical Human anchor remains distinct from the authorized root Agent relay and worker delivery.

## Delivery, source role, and lease

Each dialogue delivery is represented by an ordinary Work row with the verified transport identity:

```text
source_type       = agent_dialogue
execution_adapter = dialogue_transport
domain            = conversation
conversation_session_id = worker or root receiver session
```

The transport claim carries a server-issued lease owner, opaque lease token, and expiry. Claim, renew, handoff, and settle re-read the Work, Run, delivery, Agent/Revision, source references, control revision, and deadline. Lease loss, source provenance mismatch, receiver binding mismatch, inactive Agent, or expired deadline stops the handoff before provider execution. Lease owner and token are never part of the safe client projection.

Delivery idempotency is keyed by the dialogue and idempotency key/digest; sequence and control revision are bounded and conflict on reuse. A duplicate delivery returns or settles the existing identity instead of creating a second receiver Run/Work. A stale callback cannot create a root receipt. Worker completion must correlate the exact assistant message to the worker Run and delivery source IDs, then creates one typed response delivery. Only the root receiver side of a server-derived response delivery can acknowledge the root receipt.

The transport Work is a coordination row, not a new scheduler or provider queue. Root and worker Run/Work rows remain distinct and share common lease/retry/settlement rules. An uncertain external result is fenced; stale lease or old provider continuation is not replayed blindly.

## Current Work projection

`AgentDialogueProjection` is the bounded server projection used by the API and both clients. A work item exposes `current_run_status`, `current_work_status`, `current_delivery_state`, `current_run_id`, `current_work_item_id`, `current_delivery_id`, `status_source`, `phase`, typed refs, safe error, allowed actions, pending input, artifacts, and budget.

When a current worker delivery has the same expected control revision as the dialogue, the worker Run/Work/delivery evidence determines the current phase. A pending input yields `waiting_for_input`; active worker evidence yields `queued`, `running`, or `receiving_result`; blocked/uncertain evidence yields `blocked`. Without matching worker evidence, the dialogue status remains the source of truth. This prevents the immutable Human source anchor from being mistaken for the current worker execution.

Projection lists and refs are bounded. It excludes message bodies, provider responses and arguments, credentials, provider/model/base URL, lease owner/token, and secret-shaped metadata. `safe_error` is a bounded code/message. The API schema uses a closed set of phases and actions (`stop`, `resume`, `rework`, `answer`); clients validate the same closed projection.

## Finite dialogue and provider budgets

Budget admission is server-side and occurs before a new delivery Run/Work is created. The current source clamps the accepted ceilings to:

| budget | default | maximum |
| --- | ---: | ---: |
| turns | 8 | 100 |
| depth | 4 | 32 |
| attempts | 16 | 100 |
| tokens | unset | 1,000,000 |
| cost | unset | 1,000,000,000,000 micros |

The root aggregate and child-local ceilings are tracked separately. Turn/attempt saturation, reserved token/cost, and consumed totals are checked under the budget owner lock. `unknown`, `token_unknown`, and `cost_unknown` usage states fail closed for new admission. Reservation keys and usage digests make reserve, release, charge, unknown-to-known finalization, and replay idempotent; changed payload reuse is rejected. Provider output caps, reasoning-token semantics, tool rounds, retries, cancellation, and timeout are bounded by the remaining budget.

These semantics are source/focused evidence from `.local/selected-target-verification/f960-budget-audit-result.json` and its 68-test missing-delta log. They describe admission and accounting behavior; they do not prove a real provider full-flow charge.

## Durable question and control state

An `agent_question` is stored as a typed `AgentDialogueInput` with its source Run/Work, input revision, expected control revision, deadline, and question message reference. Pausing the worker makes the input durable, moves the worker Run/Work to its pending/blocked state, and releases its lease. Process restart can reconstruct the pending question from the database.

Human answer admission uses owner/session ACL and CAS on input revision and control revision. The answer remains a Human message. Root relay creates a distinct root Agent Run/Work and preserves the Human source identity; it does not rewrite the Human message as an Agent message or revive the old worker Run.

Stop, resume, and rework are control deliveries guarded by the current control revision and exact actor/source lease. Stop cancels the current provider hook once, releases the lease, and advances the revision. Resume/rework creates a new finite root control Run/Work/outbox and requires a new worker delivery; old Run, lease token, and provider continuation are not reused. Stale revisions return conflict and must be refreshed by the client.

## Mobile pointer and normal clients

Mobile AoiDots uses the ordinary API and the same server projection/CAS. `mobile/src/features/aoidots/root-pointer.ts` stores only a reconnect hint keyed by `auth_scope` and `server_fingerprint`, together with the server-returned root/session/Agent IDs. The pointer is not authentication or authority.

`mobile/src/lib/aoidots-api.ts` captures the token scope and server fingerprint before bootstrap, sends the request to that captured server, rejects a changed principal/server on response, and writes the pointer only for the captured scope. Each bootstrap still reads the canonical root from the server. Logout, account change, or server switch invalidates the old scope; offline queues do not blind-replay an Agent-bound command.

Web and Mobile call the same bootstrap, work-list, dialogue, input-answer, and control contracts generated from FastAPI OpenAPI. Closing the panel/overlay changes UI focus only; it does not delete server Run/Work, delivery, input, or outbox rows.

## Schema and production boundary

The additive migrations `20261012_0001` through `20261012_0004` define workspace entries, dialogue/delivery/input, usage/reservation, and budget-root data. Dialogue identity, source/receiver references, idempotency, sequence, control revision, digest, state, and budget fields are constrained by PostgreSQL/SQLite schema checks and unique/foreign keys. Existing Chat/Work rows are referenced through typed foreign keys; they are not inferred from Character or client metadata.

The read-only production audit `.local/development-resume-20261012/effective-schema-audit.json` observed head `20261012_0004` and the target model/table catalog under a read-only transaction. It did not issue repair DML/DDL, determine the historical migration actor, or make production a QA runtime. QA must continue to assert its host, port, database, user, search path, and disabled dotenv before import; the f960 full-flow DB is `aoidots_runtime_qa_f960_20261013` on port `55439`.

## Invariants for future changes

* Keep one owner-scoped projectless root Agent/session and ordinary worker sessions; do not add a second transcript, Agent-only queue, or shadow scheduler.
* Treat server-derived source role, Agent/Revision binding, Work lease, control revision, and budget as authority. Client metadata and old projections are hints only.
* Keep current worker Run/Work projection separate from immutable Human source anchors.
* Do not expose message bodies, provider arguments, credentials, lease tokens, or provider state through ledger metadata or safe DTOs.
* Do not resume a stale lease, old worker Run, or uncertain provider result as a shortcut.
* Keep fake/source/UI/provider/device evidence labeled by lane and candidate. A source PASS, synthetic security PASS, or old dfe6 result cannot become a current AoiDots feature PASS.
