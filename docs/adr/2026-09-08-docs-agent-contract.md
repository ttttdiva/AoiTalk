# Docs Agent architecture and consistency contract

Status: Integrated on main and locally verified on isolated disposable PostgreSQL. Canonical content remains the existing KnowledgeNode graph.

## Verification checkpoint (2026-09-09)

- Implementation commit `db67338a48a655d827b2fe499101fbd5de055121` (`fix(docs): preplan ordered mutation dependencies`) is pushed to `origin/main`. Reviewed predecessor `3047b536` is an ancestor of that commit and remains historical hardening, not the current HEAD claim.
- Migration head remains `20260908_0007`; chain `20260908_0005` → `20260908_0006` → `20260908_0007`.
- Local verification on isolated disposable PostgreSQL after the ordered-mutation-dependency delta:
  - combined consistency + corpus: 70 passed
  - edit-read / read projection: 24 passed
  - cancellation diagnostics: 18 passed
  - recurrence / occurrence: 13 passed
  - focused Task permission / binding: 5 passed
  - migration graph: 3 passed
- Earlier broad local Docs verification on isolated disposable PostgreSQL, not GitHub CI:
  - ACL / scope / retrieval: 461 passed, 9 opt-in / SQLite skips, and 1 pre-existing `project_join_requests` Alembic/ORM drift failure outside this Docs diff
  - broader Docs regression: 73 passed
  - mutation / binding / recurrence batch: 149 passed, with 1 pre-existing auto-close test-double / config signature failure outside this Docs diff
- These counts are local results. They are not GitHub CI passes. No GitHub CI claim is made here.
- BGE-M3 / native LLM retrieval evaluation was not rerun, because model-facing retrieval and read-projection output did not change. The 2026-09-08 synthetic BGE evidence remains historical only.

## Decision and implemented surfaces

Keep KnowledgeNode containment, editable typed blocks, typed Fields, Supertags,
placements and edges as the sole canonical Docs representation. Human editing
and owning Project/Inbox/Mail/Meeting/Memory services retain their semantics.
No second writable document store or canonical content migration is introduced.

The Agent architecture consists of:

- current ACL-aware selection scope (including canonical/reference subtrees);
- exact/lexical plus own-record/span semantic retrieval, collapsed to node IDs;
- bounded document, record and graph-neighborhood read projections;
- version-bound atomic changesets compiled through DocsGraphService;
- durable, resumable corpus enumeration and evidence delivery;
- database-triggered revisions and coalescing index delivery for every writer.

`docs_search(expand=true)` composes an authorized local section around hits.
`docs_read` supports `outline`, `document`, `record`, `neighborhood`, and `edit`.
The outline remains a compatibility/navigation view. Document/record pages retain
stable anchors, typed content and Field identity, including unset inherited or
shared Fields. Accepted live Project Q&A is read-only domain evidence; inferred
candidates, archived answers and private chat provenance are excluded.

Neighborhood reads authorize both ends of references, backlinks and placements.
A link or display placement cannot grant access. Scoped Memory remains an
independent authority and is never used as a Docs index or automatic write target.

## Revision, concurrency and cancellation

Migration `20260908_0005` historically added protocol state only: library revisions,
a policy revision, index queue, read leases, mutation receipts and coverage runs.
PostgreSQL statement/row triggers cover Python, Next.js, mobile sync, and domain
writers. Changes to child collections, Fields, tags, inheritance, edges, placements,
attachments and bound Task/Q&A content are therefore observable even when the
root's updated_at does not change. Login timestamps and unrelated user-profile
updates do not invalidate edit leases; authorization/session-state changes do.

Migration `20260908_0007` keeps the revision/index triggers but makes the old
fail-fast `docs_write_guard` a compatibility no-op. A short database advisory
critical section serializes Agent mutations with other Agent mutations only;
ordinary Task/Project/User/raw Docs writers are never rejected because an
Agent is active. Before its first canonical DML, an Agent deterministically
prelocks the leased Docs closure and all actual Task/Project/ACL/policy
side-effect rows with PostgreSQL `NOWAIT`, then rechecks the lease, authority
and revisions. A row conflict aborts only the Agent transaction as a retryable
conflict. Model planning, embedding and ordinary reads occur outside this
boundary. The Agent CAS is deliberately conservative: a library content
revision plus the policy revision. This can reject an edit because another
section in the same library changed; the caller must reread. It is safer than
missing a phantom child/Field change. Existing human APIs retain their
historical update contract; this change does not introduce a CRDT or claim
universal human-editor CAS.

`docs_read(view="edit")` creates a 30-minute, actor/root/scope-bound read lease
only for a complete bounded editable projection. Migration `20260908_0006`
also persists edit-read progress as opaque sequential checkpoints. Each exact
next token advances once; retrying the same request replays the rebuilt page
after a lost response, without persisting canonical body/plaintext response
content. Its write_token is distinct from the pagination read_fingerprint.
The latter remains a live-read fingerprint, not a historical snapshot or
authorization grant. Finish the content pages before proposing changes. The
lease records source membership and revisions; it does not claim to prove a
model mentally understood the document.

`docs_mutate` accepts an intent and a bounded ordered changeset. Existing nodes
use full UUIDs; new nodes use unique local references. UUID Field references avoid
name collisions. No omission implies deletion, and no title-based fuzzy matching
silently chooses an existing node. Move/archive validates the complete affected
subtree, including protected or unreadable descendants. Inbox/Mail/Memory and
canonical Project identities retain their owning mutation policies.

Before the first canonical DML, a dry-plan simulates existing and local nodes
together with ordered add/remove tags, resolves inherited and shared schema from
the simulated tag IDs, and plans Task bind, unlink, system-field, Project,
recurrence, occurrence, TimeEntry and notification dependencies. It then takes
deterministic PostgreSQL `NOWAIT` locks and rechecks the lease, revision, Docs
authority and Task Project read/write. A projectless Agent Task bind uses an
existing read-only-resolved Inbox Project override; raw direct Task behavior
remains unchanged. Row contention aborts only the Agent transaction as a
retryable conflict; no receipt and no partial DML persist.

The graph changes and encrypted operation receipt commit together. Reusing the
same operation_id and arguments returns the stored outcome; different arguments
are rejected. Receipt replay rechecks current authorization. A pre-commit stop or
settled AgentRun rolls back the whole transaction; a lost post-commit response is
resolved through the receipt. Database deadlock/serialization conflicts are
reported as conflicts, not partial success. Legacy title normalization now matches
the Web writer's 20,000-character storage limit and preserves whitespace rather
than silently truncating a human-edited line.

## Derived retrieval and index delivery

Docs input version 4 indexes only the node's own title, typed content, description,
tags and native Field values. Bound Task data and reference-target content are not
embedded as if they belonged to a shareable Docs node. Ancestor/neighbor content
is assembled after authorization. Long derived text adds overlapping spans with
deterministic point IDs; canonical node IDs do not change. Payload offsets are
positions in derived index text, never write offsets into canonical content.

Qdrant candidates are scoped/authorized before top-k and grouped by payload
node_id, so one long document's spans do not consume the whole result page.
Hydration rechecks live SQL eligibility and preserves ranking. Structured counts
remain PostgreSQL node/record counts, not vector or span counts.

Every point carries input/model/dimension identity and a source manifest hash.
Unchanged manifests skip embedding, writes use bounded batches, and obsolete
spans are removed only after replacement succeeds. Full text is not copied into
Qdrant payloads. The derived index remains sensitive data, not an encrypted
substitute for canonical storage.

Database triggers coalesce dirty state by library. The lifecycle-owned worker
acknowledges only the requested revision it observed before indexing; edits during
indexing remain pending. Failures, cancellation and restart retain the queue.
An index advisory lock coordinates processes sharing the canonical database;
manual/targeted indexing participates too. Cancellation waits for actual I/O
completion before releasing that boundary. Independent databases must use
separate Docs collections. Incompatible collections are retained and require an
explicit collection migration, never automatic destructive recreation.

## Overview and timeline

`docs_overview` freezes a bounded manifest of records matching structured filters
and streams their authorized content. Its encrypted run state survives process
restart. The same cursor replays the last page; a smaller context budget restores
that page's checkpoint rather than skipping evidence. Current scope membership,
library revisions and policy are revalidated, including initially empty corpora.
A change invalidates the run instead of pretending to offer historical snapshots.

The overview fingerprint matches the delivered projection. It ignores a stale
stored Task-system KFV only when a live bound Task supplies authoritative values.
It still tracks actual Task values, binding, Project ACL, effective schema,
non-Task KFV, and selected/reference visibility. Unrelated library schema remains
excluded.

Output distinguishes source_total, selected_records, delivered_records,
incomplete_records, budget_limited, has_more and coverage_complete. Delivery is
not proof of model understanding. Semantic-topic counts are not inferred from
ranked search. Node creation/update/daily-note dates are not business event dates.
Model-context shaping preserves continuation and coverage contracts, and withholds
an advance cursor or edit token when its associated content cannot fit.

## Compatibility and rollout

1. Apply normal application migrations (or `alembic upgrade head`). No Docs text,
   IDs, Field values, bindings or human layout are rewritten by the migration.
2. Start the backend. The durable worker drains migration-seeded library requests
   and rebuilds old point formats automatically when Docs RAG is enabled.
3. During rebuilding or index unavailability, lexical/structured reads continue.
   Edit/mutation/overview protocols fail closed if their DB triggers are missing.
4. Existing outline/node CRUD and specialized Inbox/Meeting/Mail APIs remain.
   The new APIs are registered in provider-neutral read/write policy sets.

An embedding-model/collection configuration change takes effect with service
restart and requires compatible index identity. Use a new collection for an
incompatible dimension/schema, then reconcile it. Downgrading removes protocol
state/triggers without altering canonical Docs; a code rollback should use a
matching index collection or rebuild it with the old input format.

Static page-summary indexes and a second canonical document model were not
adopted. Own-record/spans plus dynamic sections/neighborhoods provide the selected
multi-resolution design without persistent cross-ACL summaries or dual-write
consistency. Production-scale latency and corpus-specific quality remain things
to measure, not unsupported claims of this implementation.

## Verification

Local PostgreSQL suites exercised trigger behavior, raw-writer invalidation,
ordered mutation dry-plan and dependency locking, concurrent duplicate operations,
partial failure and cancellation rollback, current authorization on replay,
typed/inherited/shared Field updates, Task bind/unlink and Project ACL, recurrence
and occurrence side effects, durable index acknowledgement, corpus change/replay,
graph ACL and exact text paging. The migration chain
`20260908_0005` → `20260908_0006` → `20260908_0007` has been exercised on an
isolated database, with head `20260908_0007`.

Current local post-delta counts are those recorded in the 2026-09-09 checkpoint:
combined consistency + corpus, edit-read / read projection, cancellation
diagnostics, recurrence / occurrence, focused Task permission / binding, and
migration graph. Residual limits of that local evidence: two pre-existing failures
outside this Docs diff (`project_join_requests` Alembic/ORM drift, and an
auto-close test-double / config signature mismatch) are not treated as Docs
regressions, and no GitHub CI result is claimed.

The opt-in cached BGE-M3 benchmark is historical 2026-09-08 evidence only. It
used six synthetic queries over 22 nodes, with identical top-k=3 and
12,000-character context budgets. Recorded Hit@3 and evidence coverage rose from
0.667 to 1.0. That run was not repeated after the ordered-mutation-dependency
delta, because model-facing retrieval and read-projection output did not change.
It remains evidence retrieval on synthetic data, not a production-corpus or LLM
answer-quality claim. See `docs/evaluations/docs_agent_evidence_20260908.json`
and `tests/test_docs_architecture_evaluation.py` for inputs, metrics and
limitations.
