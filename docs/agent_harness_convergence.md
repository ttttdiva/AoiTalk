# Agent Harness convergence

`src/agent_harness` predates the generic AgentWork runtime.  Its workspace,
runner, workflow and privacy controls are valuable execution assets, but it
must not remain a second durable scheduler/lease/retry authority.  This guide
defines the compatibility boundary and the migration path.

## Existing harness assets

| Asset | Current responsibility | Convergence destination |
| --- | --- | --- |
| `AgentHarnessSettings` (`config.py`) | Deployment-owned enablement, polling, limits, tracker and runner settings | Coordinator/adaptor configuration; never a client-controlled command channel |
| `WorkItemTracker` / `BuiltInTaskTrackerAdapter` (`tracker.py`) | Projects existing Task rows into a bounded legacy `WorkItem` view | A `WorkSource` that materializes durable `AgentWorkItem` rows |
| `AgentHarnessOrchestrator` (`orchestrator.py`) | Legacy in-process dispatch, running/retry maps, stall reconciliation and shutdown | Compatibility facade that delegates claims/retry/recovery to `AgentWorkCoordinator` |
| `AgentRunner` and Codex/Claude/custom runners (`runner.py`) | Invoke one provider process and normalize output/usage | Called only by `CodeAgentExecutionAdapter` after a durable claim |
| `WorkspaceManager` (`workspace.py`) | Create, prepare, clean and retain task worktrees | Adapter-owned execution asset; cleanup follows coordinator settlement |
| `HarnessWorkflow`/`render_prompt` (`workflow.py`) | Render bounded repository-task instructions | Adapter-owned prompt projection; prompt is never authority |
| `CodeAgentExecutionAdapter` (`adapter.py`) | Prepare one workspace and execute one attempt; returns normalized result | Common-runtime `ExecutionAdapter` for the `code_agent` domain |

The harness `WorkItem` has additive source/Agent/Project/Task/causal fields,
but those values are routing metadata.  They do not grant filesystem,
Project, model, or provider authority.

## Current behavior

When constructed without a coordinator, the legacy orchestrator still keeps
`running`, `claimed`, `retry_attempts` and `completed` in process memory.  It
polls the built-in Task tracker, dispatches one runner task per item, schedules
exponential retry, reconciles terminal or stalled items, and drains/cancels
tasks on shutdown.  This compatibility mode is useful for existing manual
callers, but it cannot by itself prevent duplicate work across processes or
recover a claim after a process crash.

When a coordinator is supplied (or attached with `bind_common_runtime()`), the
orchestrator registers the source/adapter and forwards `tick`, snapshot and
shutdown calls without using its local claim/retry maps.  This is the intended
WS03 convergence path; startup must still ensure only one mode is active for a
given durable WorkItem.

`CodeAgentExecutionAdapter` is deliberately narrower: it creates/validates a
workspace, renders the workflow prompt, invokes one runner attempt, normalizes
the result, and always runs the after-run hook.  It does not claim, renew,
retry, reserve budget, or settle durable work.  A coordinator may pass context
through `**kwargs`, but the adapter ignores unknown values and only consumes
server-issued execution scopes.

Enterprise execution is fail-closed.  The adapter requires a server-issued
claim-specific `AgentRunScope`/WSL2+bubblewrap context before invoking a
runner; if that scope is unavailable it refuses execution.  No Enterprise
host-process fallback is allowed.  The existing path,
symlink/junction, network, environment and workspace controls in
`docs/agent_operator_security.md` remain in force.

## Target common-runtime flow

```text
Task WorkSource discovers an eligible Task
  -> AgentWorkCoordinator materializes/dedupes AgentWorkItem
  -> atomic claim + lease token + concurrency/budget reservation
  -> pin AgentRevision and create AgentRun
  -> CodeAgentExecutionAdapter.prepare()
  -> runner executes exactly one attempt in the issued scope
  -> authority/source refresh before mutation
  -> coordinator settles succeeded/retry_wait/blocked/uncertain/dead_letter
  -> adapter cleanup after durable settlement
```

The common coordinator (`src/services/agent_work_runtime.py`) now provides the
WS02 data-flow contract: `WorkCandidate`, `WorkClaim`, `ExecutionOutcome`,
`WorkSource`, `ExecutionAdapter`, and `AgentWorkCoordinator`, backed by
`agent_work.py` and migration `20260903_0002_agent_work_runtime.py`.  It
performs short transactional claims, PostgreSQL `FOR UPDATE SKIP LOCKED` where
available, SQLite compare-and-set, token-fenced start/renew/settle, stale
recovery, bounded retries and safe `AgentWorkEvent` metadata.  Focused model /
concurrency tests and application startup registration are still required
before this is called production-ready; the compatibility orchestrator is not
evidence of cross-process safety.

## Convergence rules

1. There is one claim/retry/recovery owner: `AgentWorkCoordinator`.  The
   legacy orchestrator must not independently dispatch the same durable item
   once an adapter is registered.
2. A Task tracker may discover only; it must not use `running`/`claimed` sets as
   a durable lease.  Discovery is idempotent by source type/id/revision/intent
   key.
3. Each retry creates a new `AgentRun` and links the previous attempt.  A
   continuation/provider session ID is metadata, not a claim token.
4. Agent authority and Project/Task ACLs are revalidated after execution and
   immediately before mutation.  Task archival, permission revocation, Agent
   deactivation, budget exhaustion or lease loss blocks settlement.
5. Workspace cleanup occurs in the adapter's after-run hook and, where safe,
   after coordinator settlement.  A timeout or cancellation must await the
   runner's cleanup before retrying the same work.
6. Event callbacks may attach bounded `work_item_id` and attempt metadata, but
   never raw prompt, environment, credential, provider response or hidden path.
7. Enterprise scope issuance remains server-owned.  A model/request cannot
   choose workspace root, executable, network, shell hook or resource limits.

## Failure and restart handling

* Runner timeout, transient provider/CLI failure or cancellation is classified
  by the coordinator, not by a second harness retry loop.
* Permission/authority denial or source deletion is `blocked`/permanent and
  must not be retried blindly.
* Unknown external side effect is `uncertain`; code-agent execution itself
  normally has no external publication side effect, but adapters must preserve
  provider fences if introduced.
* A process/application restart invokes coordinator stale-lease recovery.  A
  recovered item is source/authority checked before a new attempt; an expired
  lease never remains an in-memory “running” fact.
* Shutdown cancels owned runner tasks and clears process-local compatibility
  maps without deleting durable work history.

## Migration checklist for maintainers

Before removing the legacy scheduler path, verify:

* Task WorkSource and adapter registration are feature-gated and idempotent;
* duplicate workers yield one valid lease in PostgreSQL and the supported
  SQLite test/runtime mode;
* `AgentRun` rows pin Agent/Revision and retain both retry attempts;
* workspace after-run/cleanup runs on success, failure, cancellation and
  lease loss;
* scope redaction and Enterprise fail-closed tests pass;
* old Agent Harness HTTP settings, status and manual run paths remain
  compatible, but cannot enable an autonomous coordinator while the global
  runtime gate is false;
* Operations projects canonical WorkItem/Run/Task data rather than a copy of
  `AgentHarnessOrchestrator.running`.

## Evidence and gaps

**Verified (source/tests):** legacy orchestrator lifecycle and shutdown tests;
runner output/session normalization; `CodeAgentExecutionAdapter` one-attempt
behavior, cleanup and Enterprise scope checks; and the security contracts in
`docs/agent_operator_security.md`.  The focused
`tests/agent_harness/test_agent_harness_convergence.py` run observed 4 passed
(with only `datetime.utcnow` deprecation warnings).

**Implemented (source, focused runtime tests):** the common coordinator
protocol and durable claim/lease/retry/recovery implementation in
`src/services/agent_work_runtime.py`, additive WorkItem fields in the harness
projection, and `AgentHarnessOrchestrator` common-runtime delegation.
Focused runtime, convergence and startup-wiring tests are passing; production
PostgreSQL contention remains unverified.

**Unverified:** production PostgreSQL contention/restart, real Codex/Claude
provider execution, GPU/network behavior, and a complete application-level
cutover that proves the legacy orchestrator cannot run in parallel with the
common coordinator.
