# Autonomous Agent acceptance and evidence matrix

This matrix is the final integration ledger for WS01--WS06.  A row is marked
**PASS** only when the named command or runtime observation was actually
performed on this checkout.  **IMPLEMENTED** means source/tests exist but the
required deployment/runtime evidence is still pending.  **UNVERIFIED** means
the environment or safe provider target was unavailable.  **BASELINE** denotes
a repository failure unrelated to the workstream; it is not silently treated
as a PASS.

## Status vocabulary

* **PASS (automated):** focused test command completed successfully.
* **PASS (local runtime):** supported local FastAPI/Next.js/SQLite process was
  exercised and evidence is recorded.
* **IMPLEMENTED:** bounded source contract is present; one or more acceptance
  gates remain.
* **UNVERIFIED:** PostgreSQL multi-process, provider, GPU, credential or full
  browser evidence is missing.
* **BASELINE:** known pre-existing failure/error, recorded with its command.

## Core identity and authority

| Journey / invariant | Evidence to run or inspect | Status at document authoring |
| --- | --- | --- |
| Organization singleton bootstrap under concurrent first use | `tests/test_agent_identity_foundation.py::test_organization_bootstrap_converges_under_concurrency` | PASS (automated, SQLite) |
| Agent creation without synthetic User; idempotency | `tests/test_agent_identity_foundation.py` Agent/revision test; `src/memory/models/agent_identity.py` | PASS (automated) |
| Immutable AgentRevision and exact version/hash | migration `20260903_0001`; revision service/tests | PASS (automated/SQLite; PostgreSQL trigger unverified) |
| Space membership does not imply Project write | identity foundation negative-permission test; `AgentAuthorityResolver` | PASS (automated) |
| Project/Task/Persona assignment is explicit and human-granted | identity service/routes and assignment tests | IMPLEMENTED; full ACL matrix pending |
| ActorPrincipal rejects malformed/inactive/unknown actors | actor/principal tests and `docs/agent_authority.md` | PASS (automated) |
| Current authority revalidation before mutation | resolver/source adapters and revocation tests | IMPLEMENTED; live race/revocation pending |
| Safe AgentRun identity/manifest pin and redaction | AgentRun tests; `src/services/agent_run_service.py` | PASS (automated) |

## Common runtime and reliability

| Journey / invariant | Evidence to run or inspect | Status at document authoring |
| --- | --- | --- |
| WorkSource discovery and idempotent materialization | `src/services/agent_work_runtime.py`, `TaskWorkSource`, `AgentWorkItem` source identity unique key; `tests/test_agent_work_runtime.py` | PASS (focused SQLite runtime tests) |
| Atomic claim with one valid lease across workers | PostgreSQL `FOR UPDATE SKIP LOCKED`/CAS implementation plus two-worker test | UNVERIFIED (no production PostgreSQL contention run) |
| Token-fenced start, renewal and settlement | `AgentWorkCoordinator.start_claim/renew_lease/settle` and lease tests | IMPLEMENTED; cross-process runtime pending |
| AgentWorkItem → AgentRun is 1:N; retry creates new attempt | migration/model links and coordinator `create_agent_run` | IMPLEMENTED; end-to-end retry evidence pending |
| Lease loss during execution blocks late mutation | source refresh + token-fenced settlement test | IMPLEMENTED; process-interruption run pending |
| Stale running recovery after restart | `recover_stale` with expired lease fixture/process restart | IMPLEMENTED; application restart evidence pending |
| Duplicate trigger/worker startup cannot execute twice | source identity/idempotency and two-worker runtime test | UNVERIFIED |
| Retry classification/max-attempt/dead-letter | `ExecutionOutcome`, coordinator settlement and focused tests | IMPLEMENTED; exhaustive failure matrix pending |
| Budget/concurrency reservation and saturation | policy fields/coordinator limits and saturation tests | IMPLEMENTED; production usage source/performance pending |
| Causal depth/intent/mutation dedupe and loop prevention | WorkItem/Event constraints and source keys | IMPLEMENTED; adversarial loop test pending |
| Heartbeat wakes discovery but is not a second executor | heartbeat integration and coordinator startup inspection | UNVERIFIED |
| No ConversationDispatchOutbox reuse as Work queue | model/service separation inspection | PASS (source review) |

## Agent Harness convergence

| Journey / invariant | Evidence to run or inspect | Status |
| --- | --- | --- |
| Existing Task tracker projects safe metadata only | `src/agent_harness/tracker.py`, tracker tests | PASS (automated/source) |
| Code-agent adapter executes one attempt and cleans workspace | `venv\Scripts\python.exe -m pytest -q -o addopts= tests/agent_harness/test_agent_harness_convergence.py` → 4 passed | IMPLEMENTED; real CLI run unverified |
| Common coordinator, not harness, owns claim/retry/settlement | adapter/orchestrator boundary and startup registration | PASS (source/focused convergence tests) |
| Enterprise harness uses server-issued WSL2/bubblewrap scope | security/harness tests and `docs/agent_operator_security.md` | PASS (automated; deployment runtime unverified) |
| Harness cannot bypass disabled runtime/profile | feature-gated startup and negative tests | IMPLEMENTED; startup gate audit pending |

## MediaOps

| Journey / invariant | Evidence to run or inspect | Status |
| --- | --- | --- |
| Media Research due routine → operator Agent → evidence/candidate, no publish | `tests/test_media_common_runtime.py` (pinned candidate case) + Media service/source tests | PASS (focused adapter; provider search unverified) |
| Generation plan → GenerationRunIntent fence; duplicate dispatch safe | `tests/test_media_common_runtime.py` (uncertain outcome case) + generation service/intent tests | IMPLEMENTED; provider/GPU runtime unverified |
| Publication proposal → human approval → Attempt/Receipt or uncertainty | `tests/test_media_common_runtime.py` (proposal-only case) + Operations/Media action tests | PASS (automated; provider write not attempted) |
| Persona assignment does not bypass Project/owner ACL | authority and Media source tests; production registration must reject a missing resolver | IMPLEMENTED; live revocation pending |
| Metrics/learning evidence and human review preserved | metrics/learning focused suites | PASS (automated) |
| Manual Persona/Research/Generation/Pipeline/Calendar/Results remain when autonomy off | feature profile + UI/API regression | IMPLEMENTED; Enterprise live UI pending |
| No raw credential/provider response in generic events | redaction tests and safe DTO review | PASS (automated/source) |

## Operations Command Center

| Journey / invariant | Evidence to run or inspect | Status |
| --- | --- | --- |
| Bare `/operations` opens Overview | frontend route/component test or authenticated browser smoke | IMPLEMENTED; live browser evidence pending |
| Agents, Work and Activity views render safe projections | `OperationsCommandCenter` + `GET /api/operations/command-center`/`OperationsCommandCenterService` source; focused UI/runtime test pending | IMPLEMENTED; non-admin joins and live data pending |
| Persona remains at `tab=personas`; existing Media/Engagement tabs preserved | navigation source inspection and existing tab tests | IMPLEMENTED (live reload pending) |
| All-company/Space/Project/Agent, domain/state/time filters persist in URL | Command Center source filter contract (dedicated component test pending) | IMPLEMENTED (source) |
| Activity projects canonical ledgers, not duplicate state | `OperationsCommandCenterService._activity` projects WorkEvents, AgentRunEvents/ToolCalls, TaskActivity, Heartbeat history and ExternalAction ledgers | IMPLEMENTED; large-ledger/per-ledger load coverage pending |
| Sensitive fields/lease tokens/paths are absent | projection redaction tests | IMPLEMENTED; browser network inspection pending |

## Enterprise and feature distribution

| Journey / invariant | Evidence to run or inspect | Status |
| --- | --- | --- |
| Personal defaults keep new autonomous/company flags off | `venv\Scripts\python.exe -m pytest -q -o addopts= tests/test_features.py` (14 passed) | PASS (automated) |
| Media autonomy clamps off when common runtime is off | feature dependency test in `tests/test_features.py` | PASS (automated) |
| Enterprise + stale `FEATURE_*`/`AIVTUBER_ENV` cannot enable new flags | feature hard-deny tests | PASS (automated) |
| Exported Enterprise selector cannot be overwritten by stale `.env` Personal values | `tests/test_features.py` Config dotenv regression (14 feature tests passed) | PASS (automated; production launcher still unverified) |
| Disabled runtime does not start/register/claim WorkSources | server startup/registration inspection and negative test | IMPLEMENTED source intent; startup runtime pending |
| `virtual_company=false` hides company management but preserves core/manual paths | identity route/API/UI regression | IMPLEMENTED source gate; full distribution test pending |
| Enterprise handoff does not add native execution/egress/provider capability | handoff builder/sanitizer and Enterprise runtime smoke | UNVERIFIED for this workstream |

## Migration, contracts and release gates

| Gate | Command/evidence | Status |
| --- | --- | --- |
| Alembic one-head graph | `venv\Scripts\python.exe -m alembic heads` → `20260903_0002 (head)`; graph test expectation updated and targeted run observed 3 passed | PASS (local graph/test) |
| Additive SQLite migration preserves legacy rows/FKs/indexes | migration smoke with foreign keys enabled | PASS (automated/local) |
| PostgreSQL upgrade/rollback and contention | deployment DB smoke | UNVERIFIED |
| OpenAPI regenerated; Web/Mobile generated types synchronized | `scripts/generate_openapi.py` (652 paths), frontend/mobile typegen and drift checks | PASS (local generation/typecheck) |
| Focused backend, authority, AgentRun, Harness, MediaOps, Operations tests | repository-native focused subsets | IMPLEMENTED; final aggregate pending |
| Frontend lint/typecheck/build and Vitest | scoped commands + production build | IMPLEMENTED; full-repo baseline diagnostics remain |
| Browser E2E/reload/deep links/console/network | authenticated QA using `.env.qa-login` | UNVERIFIED unless a dated run is recorded here |
| Real provider/GPU/credential mutation | approved private/unlisted/draft target only | UNVERIFIED by policy; no real write was attempted |

## Known baseline and external limitations

The WS05 focused Vitest collection was blocked by the repository's stale
`D:/Dev` setup path; scoped ESLint and TypeScript diagnostics for the owned
Command Center files were clean.  Repository-wide test/lint/type failures that predate this work must be listed
with their exact command and first failure; they do not lower the acceptance
requirements above.  Typical examples are unrelated Docs/LLM/story fixtures,
frontend hook/type diagnostics, or unavailable optional provider services.

External PostgreSQL contention, production process restart, live provider API/
GPU execution, and credential-vault verification require deployment-owned
infrastructure.  Until those gates are exercised and recorded, the complete
architecture is **not** a production approval even when focused local tests
pass.
