# MediaOps acceptance and evidence matrix

This is the repository evidence index for the Operations Epic. A row is marked
**PASS** only when the command/evidence named in the row was actually observed;
**IMPLEMENTED** means the bounded contract exists but runtime/provider evidence
is still pending. Provider writes, paid releases and public posts were not made
as QA side effects.

| Journey | Contract/evidence | Status |
|---|---|---|
| Nine-Persona intake | `tests/test_media_operations_setup_service.py` and `tests/test_media_operations_setup_routes.py`: fixed nine-slot preview, inferred-vs-explicit facts, correction, atomic apply, incomplete drafts and persistence/idempotency. | PASS (automated) |
| Persona policy/resources | `tests/test_media_operations_service.py`, `tests/test_media_operations_routes.py`, `tests/test_media_operations_drizzle_constraints.py`: immutable Persona revisions, policy fields, typed resource kinds, ACL and hash constraints. | PASS (automated) |
| Platform accounts | `tests/test_media_operations_setup_service.py`: account identity is separate from Persona, capability/credential status is secret-free, immutable revisions and ACL. Migration `20260901_0015` adds Persona/connection/status/policy fields. | PASS (automated; migration upgrade still requires deployment DB check) |
| Research/editorial | `tests/test_media_operations_research_service.py` and route tests: Persona/account-scoped routines, recurrence/due state, run snapshots, candidate lifecycle/dedup/expiry/promotion and source-only evidence. | IMPLEMENTED — source/manual evidence path; external search runtime not exercised |
| Content variants | `tests/test_media_operations_content_service.py` and route tests: all six closed platform payload types, immutable variant revisions, exact upstream hashes and independent variants. | PASS (automated) |
| QA/Rights gate | Content service tests cover failed/review-required blocking, human-only pass/clear and readiness mismatch. | PASS (automated) |
| Trusted publication | `tests/test_operations_service.py` and `tests/test_operations_routes.py`: Media action payload binds exact revisions/hashes; approval invalidates on change; manual/failed/uncertain/receipt paths remain in the existing Trusted Operations Kernel. | PASS (automated; provider writes not attempted) |
| Metrics/Experiments/Revenue | `tests/test_media_operations_metrics_service.py`: bounded imports, six-platform validation, inconclusive experiment result, evidence and revenue correction semantics. | PASS (automated) |
| Learning proposals | `tests/test_media_operations_metrics_service.py` plus learning service tests: evidence/window/confidence are required; proposals stay pending human review and are not copied wholesale to Memory. | PASS (automated) |
| Work Intelligence | `tests/test_operations_service.py` and `tests/test_work_intelligence.py` cover scoped, redacted projections; MediaOps evidence is metadata-only. | PASS (automated; full suite rerun at ship gate) |
| Agent facade | `src/tools/operations_direct.py` exposes ACL-bound reads and proposal/create tools only. Approval, execution and reconciliation are not model-facing; `tests/test_operations_tools.py` and the targeted backend gate passed. | PASS (automated; live Agent runtime not exercised) |
| Japanese Web journey | Operations navigation now presents Personas / Pipeline / Calendar / Results with EngagementOps preserved; focused frontend tests, scoped lint/type checks and production build passed. | PASS (automated + live Playwright admin smoke) |
| Generation Studio boundary | `src/services/media_operations_generation_service.py` stores opaque workspace/run/output/provenance refs and fails closed when unavailable; no model/LoRA filesystem or DB duplication. | IMPLEMENTED — Generation Studio runtime/GPU not verified |
| Six platform capability | `docs/media_platform_capability_matrix.md` records dated official facts and honest manual/API status. | PASS (research artifact); real credentials unavailable |
| Browser persistence / Agent runtime | Must be exercised against supported FastAPI + Next.js + PostgreSQL QA path using `.env.qa-login`; record Console/Network/reload evidence here before declaring ship complete. | IMPLEMENTED — browser smoke observed authenticated Operations landing and reload; full Persona CRUD persistence and live Agent/provider runtime remain pending |
| EngagementOps regression | Existing CrowdWorks-style service/route coverage passed in the targeted gate; the repository-wide suite still has unrelated pre-existing failures/errors. | PASS (targeted; full-suite result documented below) |

## Character Kernel and Credential Vault follow-up

The Operations surface now treats the Character as the primary object while
retaining the legacy nine-slot intake only as a compatibility import path. The
Character dashboard owns the connected-account projection and links each
PlatformAccount to the selected Character.

| Journey | Contract/evidence | Status |
|---|---|---|
| Unlimited Character Kernel | Character CRUD, revision/idempotency semantics, ACL-scoped dashboard projection and the Characters workspace are covered by the MediaOps service/route and frontend tests. | PASS (automated) |
| Credential Vault lifecycle | `MediaPlatformCredential` stores AES-GCM ciphertext with dynamic Character/account AAD; safe DTOs omit ciphertext/key material, mutations are human-only and ACL-scoped, audit rows are immutable/hash-chained, and add/rotate/verify/disable are idempotent. | PASS (automated) |
| Provider verification boundary | X, YouTube and Patreon OAuth identity calls are the only real verifier paths. Unsupported/manual credentials remain pending or unsupported; a custom adapter cannot manufacture `verified` without the built-in provider evidence marker, `identity=available` and `identity_match`. | PASS (fail-closed contract); real provider not exercised |
| Migration/rekey safety | Alembic `20260901_0018` (vault), `20260902_0019` (recipe-schema repair), `20260902_0020` (legacy audit actor-FK removal) and `20260902_0021` (state-hash evidence binding) upgrade to one head. Duplicate connection bindings fail with an actionable preflight; the rekey script supports dry-run, key selection and immutable rekey events. | PASS (PostgreSQL QA + SQLite migration smoke) |
| Live Character workspace | Built-in Browser QA at `http://127.0.0.1:3002/operations` authenticated with `.env.qa-login`, reloaded and reselected Character `aa8ced73-ecbc-4c02-b64a-8a0368e06302`; dashboard, Connections, Research/Results, Pipeline and Calendar projections returned 200/empty states with no console errors. | PASS (live UI smoke) |

The live smoke intentionally did not send a real provider credential or publish
anything. The built-in Browser file chooser could not complete a local file
selection within its tool timeout, so the upload-byte path remains covered by
backend/frontend tests rather than claimed as a live file-selection PASS.

## Final gate commands

The following evidence was observed for this scoped integration (all commands
were run from the repository checkout unless noted):

* Backend targeted gate: `214 passed in 69.60s` across setup, Persona,
  research, content, metrics, overview, publication, Agent-tool, migration,
  generation and heartbeat suites.
* Latest Agent/research smoke after the final facade additions:
  `16 passed in 15.01s` (`tests/test_operations_tools.py` and
  `tests/test_media_operations_research_service.py`).
* Additional Work Intelligence gate: `13 passed in 4.21s`.
* Frontend MediaOps Vitest: `11 test files, 25 tests passed` via
  `npx vitest --config vitest.media.config.ts run`.
* Live Playwright admin smoke at `http://127.0.0.1:3002`: QA credentials were
  read from `.env.qa-login`; login, `/operations` navigation, Japanese
  Personas/Pipeline/Calendar/Results and EngagementOps labels, and a reload
  were observed without console errors. No production Persona data was
  created.
* Scoped ESLint: clean for MediaOps/workspace files.
* Scoped TypeScript filter: no MediaOps/workspace diagnostics; the full
  repository typecheck retains unrelated baseline diagnostics.
* Python `py_compile` and Ruff: clean for changed backend Epic files.
* OpenAPI regeneration: `613 paths` written to
  `contracts/openapi/fastapi.json` and `frontend/openapi.json`.
* Web and Mobile type generation: `npm run typegen` and
  `node scripts/generate-api-types.mjs` passed.
* Production Web build: `npm run build` passed (one existing Turbopack NFT
  warning in the story-image route).
* Repository-wide backend pytest was also observed before the final small
  tool-signature fix: `7503 passed, 44 skipped, 61 failed, 51 errors`; the
  failures/errors were existing unrelated docs/LLM/story/environment cases,
  so this is not presented as a full-suite PASS.

Latest WS02 follow-up evidence (final hardening and idempotency fix):

* Feature/hardening commit: `7925732f86943405fe75d3f1eb538fd6aee155ab` on
  `main`. This exact ref preserves the immutable `PlatformAccount.create_hash`
  when a credential is attached later and includes a replay regression for the
  original create payload plus `Idempotency-Key`.
* Combined Character/MediaOps/Credential/Vault/Operations regression gate:
  `250 passed in 57.19s` (migration graph, request-body limits, provider
  verifier, publication, Agent facade and all six MediaOps surfaces). A
  post-fix focused vault/crypto rerun also passed (`6 passed`).
* Frontend MediaOps Vitest: `11 test files, 33 tests passed`; the focused WS02
  subset was `4 files, 16 tests passed`. Scoped ESLint and time-safety checks
  were clean, and the production Next build passed with the existing
  story-image Turbopack NFT warning. Full repository lint/typecheck retain
  unrelated baseline diagnostics.
* Built-in Browser smoke: dashboard, connection controls, Research/Results,
  Pipeline and Calendar rendered against FastAPI (`3000`) and Next.js (`3002`);
  dashboard/results/pipeline/calendar API calls were observed as HTTP 200 and
  browser `console.error`/`console.warn` remained empty. No real provider
  request was sent.
* Migration state after the repair migrations: Alembic `20260902_0021` is the
  single current head. Legacy SQLite actor-FK rebuild was exercised with
  foreign keys enabled and preserved rows, indexes and immutable triggers.

The file chooser itself timed out in the built-in Browser tool, so live byte
selection remains incomplete despite the bounded upload parser and frontend
tests. Real browser persistence beyond the observed dashboard/reload path,
live Agent execution, live provider credentials/API writes, and an authorized
Generation Studio GPU run were not available. Provider write was not attempted;
do not turn these external or tooling gaps into a fabricated PASS.

GitHub Actions for the exact ref was run `#1451 / 33545055956`. Python security,
mobile conformance and mobile release-gate jobs passed; schema-drift,
repository-wide frontend lint, and protected-shell/Docs E2E jobs failed on
pre-existing repository issues, while the full backend pytest job remained
in-progress at the last observation. Therefore this is not claimed as a
repository-wide CI-green release; the scoped gates above are the relevant local
evidence for WS02.

**Push:** confirmed: `origin/main` contains
`7925732f86943405fe75d3f1eb538fd6aee155ab`.
