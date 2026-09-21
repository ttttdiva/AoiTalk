# Feature and distribution matrix

This matrix is the release-time contract for the three autonomous/company
flags.  Flags are capability gates, not tenant selectors.  A disabled feature
must be inert and undiscoverable where the route/UI is company-specific, while
shared data contracts and manual workflows remain available.

## Effective profile resolution

`src/features.py` resolves a profile from `AOITALK_PROFILE` and the legacy
`AIVTUBER_ENV`.  If either selector is `enterprise`, the effective profile is
Enterprise (fail-closed; a contradictory Personal selector cannot downgrade
it).  `Config` preserves an explicitly exported Enterprise selector when it
loads a stale `.env` and resets the feature cache after that boundary.  For
non-Enterprise profiles, `FEATURE_<NAME>` environment values take priority over
profile/default values.  `Features.reset_cache()` is test-only; production
startup must load profile/environment before caching flags.

Enterprise hard-denies voice, TTS, Discord, crawler, entertainment, remote
server view, and all three new autonomous/company flags regardless of stale
environment values.  `code_agent` remains independently enabled by the
Enterprise default.  The `config/profiles/*.yaml` files predate the new flags,
so `Features.DEFAULTS`/`ENTERPRISE_DEFAULTS` are the source for their fallback
values.

## Matrix

| Capability / surface | Personal default | Enterprise default | Explicit gate/notes |
| --- | --- | --- | --- |
| `autonomous_agent_runtime` common coordinator | **OFF** | **OFF (hard deny)** | Must be true before WorkSource registration, discovery, claim, execution or restart recovery. |
| `virtual_company` Organization/company Agent management | **OFF** | **OFF (hard deny)** | Gates company profile, assignments and company UI/dispatch; singleton Organization/core Agent data may remain for compatibility. |
| `media_operations_autonomy` Media WorkSources/automatic dispatch | **OFF** | **OFF (hard deny)** | Effective true only when `autonomous_agent_runtime` is true; never disables manual MediaOps. |
| `code_agent` existing code maintenance | OFF | **ON** | Independent of `virtual_company`; use the existing harness/scope contract. Verify actual call-site gates separately. |
| Manual MediaOps Persona/Research/Generation/Pipeline/Calendar/Results | **ON** | Distribution-dependent/manual | Must remain functional when Media autonomy is false; provider status remains truthful/manual. |
| EngagementOps Connections/Opportunities/Approvals/Actions | **ON** | Required/manual | Existing human ACL/approval/Attempt/Receipt boundaries remain. |
| Core Task/Project/AgentRun contracts | **ON** | Required | Do not remove shared schemas merely because company autonomy is disabled. |

The “ON” Personal cells describe the repository's historical/manual defaults,
not an assertion that external credentials or providers are configured.

## Disabled behavior requirements

### `virtual_company=false`

Disable or hide Organization company-profile management where it is
company-specific, company Agent management UI, internal autonomous Task
execution and company-specific Operations panels.  Keep singleton Organization
bootstrap/readability, shared Agent/Revision/AgentRun contracts, manual Tasks/
Projects, MediaOps, EngagementOps, Approvals/Actions, and independently enabled
code-agent functionality available.  Do not delete identity history on a flag
transition.

The identity API currently gates profile and assignment routes with a 404 when
the flag is false.  WS06 distribution must also decide whether base
Organization/Agent routes are advertised; leaving them registered is not by
itself proof that a company UI is enabled.  Mutations remain admin-human even
when the company flag is true.

### `media_operations_autonomy=false`

Do not register Media Research/Generation/Publication/Metrics WorkSources or
dispatch automatic Media Agents.  Keep manual Persona management, Media
Research/Generation/Pipeline/Calendar/Results APIs/UI, credential-vault status,
and manual ExternalAction flow.  A Media adapter must return an explicit
disabled/unavailable result and never pretend that a provider call succeeded.

### `autonomous_agent_runtime=false`

Do not start the common coordinator, register autonomous WorkSources, claim or
execute AgentWorkItems, or recover expired autonomous leases.  Historical
read-only projections may remain.  Ordinary Chat, Task, Project, manual
MediaOps and Operations behavior must continue.  A coordinator/WorkSource
constructed by tests with an explicit override must not become a production
escape hatch around the effective Enterprise/profile gate.

## Dependency and fail-closed rules

`media_operations_autonomy` depends on `autonomous_agent_runtime`; the
`Features.media_operations_autonomy()` accessor clamps a true request to false
when the runtime gate is false.  `Features.dependency_errors()` and
`validate_dependencies()` are available for readiness checks.  Startup should
invoke validation after profile/environment loading and fail closed (or leave
the feature disabled) on an invalid combination.

Feature state is not an ACL.  Agent authority still requires Organization
policy, active Agent/Revision, Space assignment, Project grant, Persona
operator assignment and tool/harness scope intersections.  A true flag only
permits the corresponding runtime surface to be considered; it never grants a
capability to a caller.

## Enterprise distribution boundary

Enterprise handoff defaults keep all three flags false.  A stale `.env`, legacy
`AIVTUBER_ENV`, persisted runtime setting or existing Agent row must not enable
native execution, external connections, provider credentials or autonomous
dispatch.  Existing Enterprise path/network/environment/resource restrictions
remain at least as strong as before.  If an approved future distribution
policy enables a flag, it must explicitly document the policy, source revision,
rollback and provider/scope tests; do not infer enablement from a non-empty
Agent table.

## Startup and rollout checklist

1. Load the selected profile and `.env` before first `Features` access.
2. Record `Features.profile_name()`, `get_all()`, `dependency_errors()` and
   `validate_dependencies()` in startup diagnostics.
3. In Enterprise, confirm the three autonomous flags are false even when
   stale `FEATURE_*` variables are present.
4. Start the common coordinator only when the effective runtime gate is true;
   do not register Media sources when Media autonomy is false.
5. Verify disabled routes/UI are hidden or return a safe 404 while manual
   MediaOps/EngagementOps and shared core routes return expected statuses.
6. To roll back, disable the runtime gate first, stop/drain workers, preserve
   durable history, and leave Organization/Agent/revision rows intact.

## Evidence and known gaps

**Verified (source/tests):** Personal/Enterprise defaults, environment
override behavior, Enterprise hard-deny for legacy and new flags,
`AIVTUBER_ENV` profile fallback, Media→runtime dependency clamp, and
`get_all()`/reset behavior in `src/features.py` and `tests/test_features.py`.

**Implemented (source and focused integration tests):** common coordinator and Media
WorkSource/ExecutionAdapter classes expose runtime/media feature gates.  Custom
test checkers are subordinate to the global effective gate, and registration /
materialization apply the coordinator gate.  A coordinator created before a
profile change still requires lifecycle handling so its cached enabled state
cannot continue dispatching after the profile is disabled.

**Verified (source/test):** `Config` now preserves exported Enterprise
selectors across a stale `.env` load and resets `Features` afterward; the
regression is included in `tests/test_features.py` (14 passed in the focused
run).  **Unverified:** production Enterprise handoff sanitization,
disabled-route/UI behavior against a running deployment, and coordinator
shutdown when a flag changes at runtime.  These remain release gates, not
assumptions.
