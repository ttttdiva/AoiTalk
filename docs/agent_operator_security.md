# Local Coding Operator Security Contract

This document records the Task18 security boundary implemented in the current
runtime.  It is intentionally explicit about the host capabilities that are
not available: a Windows Job Object controls process lifetime, not filesystem
access, and therefore is **not** treated as a repository sandbox.

## Run-scoped repository contract

`src/security/agent_run_scope.py` is the single path policy for a coding run.
The parent controller constructs an immutable `AgentRunScope` containing the
canonical repository root, repository identity, run ID, baseline revision and
Git state, read/write/delete/command roots, and optional scratch roots.  The
scope uses canonical component-aware containment and rejects `..`, absolute
escapes, symlink/junction/reparse escapes, and unsafe move/rename/copy source
or destination paths.  `run_scope_context()` binds the contract through a
`ContextVar`; raw model-provided paths are never converted into a scope.

`src/services/agent_run_scope_service.py` is the parent-only factory that
captures the immutable Git snapshot and issues the opaque capability.  The
turn runtime and Director handoff preserve that marker; a missing or
cross-run/mismatched marker blocks write-capable repository workers instead of
falling back to an arbitrary `workspace_root` from model/project text.

The production Terminal/Director entrypoint invokes that factory only from the
dedicated `agent_operator.repository_root` (or
`AOITALK_OPERATOR_REPOSITORY_ROOT`) setting.  To opt into the parent QA lane,
set `agent_operator.qa_browser_enabled=true` and provide explicit
`agent_operator.qa_allowed_origins`; the entrypoint then starts a separate QA
profile and injects the capability into the `ui_qa_worker` facade.

When a scope is bound, OS operation tools, file editors, filesystem readers,
workspace-MCP wrappers, and background/foreground command entry points consult
it.  Repository-root deletion is denied.  Unscoped legacy application calls
retain their existing user/project behavior.

## Shell and child-process boundary

Generic `CommandExecutor` and `BackgroundJobRegistry` **fail closed** for an
active run scope unless a real file-scoped backend owns process creation.  A
cwd check or dangerous-command regex cannot constrain shell redirections or
child/grandchild processes, so no placeholder backend is accepted.

On the current Windows host, `src/security/wsl_bwrap_backend.py` provides and
tests a concrete WSL2 Debian + bubblewrap backend.  It mounts only the selected
repository at `/workspace`, uses `--clearenv`, `--unshare-pid`,
  `--unshare-net`, read-only runtime binds, and `--die-with-parent`.  The
  foreground, streaming, and background scoped command paths use the same
  backend ownership; target writes and child/grandchild writes succeed, while
  outside/symlink escapes remain hidden and timeouts terminate the host wrapper
  and its namespace descendants.  Read-only scopes use `--ro-bind`.  Explicit
  scratch roots are rejected until a separate mount mapping is supplied.

Claude/custom runners, Agent Harness runners, and any generic shell path must
not be treated as file-scoped unless they use this backend or another verified
OS backend.  Active scoped CLI/native and harness runs therefore route through
the WSL2+bwrap owner or fail closed; the unscoped legacy path remains for
ordinary non-Operator automation.

## Subprocess and MCP environment

`src/utils/subprocess_env.py` now creates a small runtime allowlist, sets
`PYTHON_DOTENV_DISABLED=1`, and blocks secret-shaped inherited variables.
MCP configuration values are passed only when their exact configured names are
explicitly authorized; parent `os.environ` is never copied wholesale.  Agent
Harness runners use the same helper.  This prevents ordinary inheritance and
dotenv rehydration, but a genuine OS file sandbox is still required to protect
against a malicious child that deliberately bypasses environment conventions.

Agent Team workers cannot configure MCP through the specialist delegate, and
the separate MCP child process cannot inherit a Python `ContextVar`; any future
worker MCP lane must propagate a signed scope contract or remain disabled.

## Worker, Director, Browser, and Git boundaries

* `WorkerReport` is a bounded structured report with findings, evidence,
  changed scope, verification, unresolved items, decision, and references.
  Publication metadata is parent-owned; workers cannot grant themselves
  commit/push/reset/clean authority.
* Agent Team children are leaves: nested delegation, Director/browser tools,
  MCP, and Git publication tools are filtered/denied.
* `BrowserRunScope` separates a parent-only Director lane from a QA lane with
  exact HTTP(S) origin allow-lists, ChatGPT/file URL rejection for QA,
  repository-scoped upload/download checks, separate profile/process IDs, and
  bounded action/lifetime leases.  `ChatGPTWebProvider` guards its shared
  profile coordinator against Agent Team roles.
* `QABrowserTransport` and `launch_playwright_qa_transport` are the
  driver-agnostic adapter boundary.  The launcher creates a temporary,
  per-run profile, passes the sanitized environment, installs the exact-origin
  request/redirect guard, and returns a transport that applies
  the navigation/redirect/file/action gates before invoking an injected
  Playwright/MCP driver and closes it under the same bounded lifecycle.
  The parent-owned `QABrowserRegistry` issues only an opaque capability facade
  to the opt-in `ui_qa_worker`; implementers, reviewers, and all non-QA workers
  cannot construct or receive the raw driver/profile/context.
* `GitPublicationGate` captures a run baseline and performs a non-mutating
  parent preflight that preserves pre-existing user changes, rejects outside
  or unrelated paths and worker publication actions, and requires explicit
  parent/Director review.  The parent remains the only component that may
  invoke the real commit/push transport.

The Browser policy remains transport-agnostic at the Playwright/MCP driver
boundary, while the parent registry and Agent Team `browser_qa` capability
provide the runtime exposure boundary.  A deployment-specific driver is still
injected by the parent; no worker can supply a raw page, profile, or transport.

## Local model selection

Coding workers continue to use the existing Agent Team v3 routing and
Execution Profile/provider selection.  Qwen3.8 27B and Gemma 4 26B A4B remain
selectable through `openai_compatible_local_profiles.py`; no model ID is
hard-coded into the security contract.

`src/services/model_bakeoff.py` provides a callback-based comparison contract
for Qwen/Gemma (or any other configured route) without downloading or starting
models itself. It records tool-call success/malformed calls, path hallucination
and exploration precision, patch/test/build success, Director required fixes,
rounds, latency, and context samples. Use the same fixture and an explicit list
of model/profile routes; `best_model` is a deterministic tie-broken summary,
not a hard-coded default.

## Verification anchors

Focused acceptance tests cover:

* repository path/symlink/junction/reparse policy;
* active-scope OS operation and WSL2+bwrap foreground/stream/background
  child-boundary behavior;
* sanitized child/MCP/Harness environments;
* structured worker reports and leaf-tool filtering;
* Director profile-role denial and Browser origin/path/lifecycle policy;
* Git baseline/publication preflight.

The repository does not silently reuse the Director profile or credentials.
