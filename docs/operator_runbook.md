# Autonomous Agent operator runbook

This runbook is for a deployment operator or incident responder.  It assumes
the existing AoiTalk FastAPI/Next.js deployment, PostgreSQL as production
database, and the repository's Enterprise/Personal profile conventions.

## Safety rules

* Never enable a feature by creating an Agent row or by editing a prompt.
* Never put a token, password, cookie, provider response, host path or shell
  command in AgentRevision, WorkItem, event metadata, Operations projections or
  a ticket/log message.
* Do not test publication, paid generation, credential rotation or destructive
  provider operations against a real account.  Use a private/unlisted/draft
  target and an explicit reconciliation plan.
* An uncertain external result is not a failure to blindly retry.  Reconcile
  the provider/source fence first.
* Preserve unrelated user changes and durable history during maintenance; do
  not reset/clean a working tree as an incident shortcut.

## Preflight before enabling autonomous work

Run from the checkout with the configured virtual environment:

```powershell
git status --short
venv\Scripts\python.exe -m alembic heads
venv\Scripts\python.exe -c "from src.features import Features; print(Features.profile_name()); print(Features.get_all()); print(Features.dependency_errors())"
```

Expected conditions:

1. One Alembic head is reported.  If migrations are pending, stop and apply
   them through the normal deployment process; do not start a coordinator
   against a partial schema.
2. The effective profile is intentional.  For Enterprise, the three
   autonomous/company flags and all prohibited legacy integrations are false,
   even if stale `FEATURE_*` values exist.
   Verify the selector again after `Config`/dotenv loading; an exported
   Enterprise selector must not be replaced by a stale `.env` Personal value.
3. `Features.validate_dependencies()` is true.  A Media autonomy request with
   the common runtime disabled is clamped false.
4. PostgreSQL connectivity, clock synchronization and worker identity are
   healthy.  SQLite tests do not prove multi-process production behavior.
5. Authority rows exist: active Agent, exact AgentRevision, Space assignment,
   Project grant, Task/Persona assignment as applicable.  A flag does not
   replace these rows.

Record the command output and deployment revision in the acceptance matrix;
do not paste secrets or raw policy blobs.

## Controlled rollout

Enable in this order, with a restart/verification between steps:

1. Keep `autonomous_agent_runtime=false` while applying the additive
   migration and exercising read-only projections.
2. If company management is required, enable `virtual_company` only after
   admin-human identity/assignment tests pass.  This flag alone must not start
   a worker.
3. Enable `autonomous_agent_runtime` for a bounded canary (low concurrency,
   short lease, low max attempts).  Confirm coordinator startup, WorkSource
   registration and no duplicate claims.
4. Enable `media_operations_autonomy` only after the common runtime canary and
   Media-specific fence/reconciliation tests pass.  Begin with Research or
   metrics; keep publication proposal-only until human approval is observed.

Use deployment-owned configuration, not model-facing tools, to change flags.
After every change inspect startup logs for effective profile/feature state and
the coordinator worker ID.  A flag transition must not delete WorkItems,
AgentRuns, evidence or Media ledgers.

## Routine operations

* **Overview:** open `/operations` and confirm summary counters, current work,
  Requires attention, budgets and Media status.  It is read-only.
* **Agents:** `/operations?tab=agents` shows safe identity/revision/assignment
  projections.  Use the backend identity API for admin changes; never edit DB
  rows manually.
* **Work:** `/operations?tab=work` filters by `scope`, `space_id`,
  `project_id`, `agent_id`, `domain`, `state` and `range`.  Follow links to the
  canonical Task/Persona/Action screen.
* **Activity:** `/operations?tab=activity` is a projection over WorkEvents,
  AgentRun events, TaskActivity, Heartbeat history, ExternalAction receipts and
  Media evidence.
* **Persona/Media:** `/operations?tab=personas` (and the preserved research,
  generation, pipeline, calendar and results tabs) remains the manual Media
  surface.  Persona is a public identity, not an AI employee.
* **Approvals:** `/operations?tab=actions` is human-only.  Inspect payload
  version/hash and approval invalidation before executing.

## Incident response by state

### Lease lost, stale or duplicate claim

1. Stop/drain the affected coordinator instance; do not start a second retry
   loop.
2. Inspect the WorkItem lease owner/expiry and latest WorkEvents.  Lease tokens
   are not displayed to operators.
3. Run the coordinator's stale recovery once the original process is stopped.
   Recovery must source/authority-check before returning to `retry_wait`.
4. If two workers both report execution, treat the item as an incident and
   inspect AgentRun/event timestamps; do not rerun until side effects are
   reconciled.

### Permission revocation or Agent deactivation

Revoke the assignment/deactivate the Agent through the admin API, then allow
the current lease to settle.  The adapter must revalidate authority before a
mutation and transition to `blocked`/escalated.  A prior manifest or revision
does not retain privilege.

### Transient failure / exhausted attempts

`retry_wait` means a bounded new attempt will be created.  Confirm the
`not_before`, attempt count and max-attempt policy.  `dead_letter` means the
operator must inspect the safe error code and source before replaying; do not
reset attempt counters by hand.

### Approval pending

For `awaiting_approval`, inspect the canonical ExternalAction and exact payload
hash/version.  Only a human can approve.  If content/revision changed, reject
or recreate the proposal; do not reuse the old approval.

### Uncertain provider result

Keep the WorkItem `uncertain`.  Query the provider only through the approved
adapter/reconciliation path, compare the GenerationRunIntent or ExternalAction
fence, and record the resulting Attempt/Receipt.  If the provider cannot be
reached, escalate with evidence rather than resubmitting.

### Budget or concurrency saturation

Leave work in `retry_wait`/`blocked`, lower the canary scope or adjust the
deployment policy through an approved change.  Never bypass a ceiling by
editing the Agent prompt, Team or Execution Profile.

## Disable and rollback

For an emergency:

1. Set `media_operations_autonomy=false` to stop new Media source discovery.
2. Set `autonomous_agent_runtime=false` to prevent new claim/execute/recovery
   work; stop the coordinator and drain owned runs.
3. Keep `virtual_company` false if company surfaces are not approved.
4. Preserve WorkItem/Event/AgentRun/Media history.  Revoke risky grants and
   external credentials through the existing human-controlled vault process.
5. Existing manual MediaOps, EngagementOps, Chat, Task/Project and read-only
   Operations behavior must still work.  Confirm `/operations?tab=personas`,
   `/operations?tab=actions` and core APIs before closing the incident.

Do not downgrade or drop the additive migration as a first response.  If a
schema rollback is unavoidable, follow the migration's documented FK/SQLite/
PostgreSQL procedure and take a database backup; production rollback evidence
must be recorded separately.

## Verification commands

Focused local checks (adjust the test subset to the changed workstream):

```powershell
venv\Scripts\python.exe -m pytest -q -o addopts= tests/test_features.py tests/test_agent_identity_foundation.py
venv\Scripts\python.exe -m pytest -q -o addopts= tests/agent_harness/test_agent_harness_convergence.py
venv\Scripts\python.exe scripts/generate_openapi.py
```

When the WorkItem runtime test module is present, add its path to the focused
pytest command.  Also run the repository-native frontend type generation, scoped lint/typecheck,
production build and browser smoke where those surfaces changed.  Mark a
provider/GPU/PostgreSQL contention gate **unverified** when the deployment
cannot safely exercise it.  Distinguish pre-existing repository failures from
task-caused regressions in the evidence record.

## Evidence to retain

For every rollout/incident capture:

* commit/deployment revision and effective profile/feature snapshot;
* migration head and database/provider environment (without secrets);
* WorkItem ID, Agent/Revision IDs, safe error/outcome and event timestamps;
* authority/assignment changes and approval/payload hash references;
* recovery/rollback command and observed result;
* external verification gaps and next action owner.
