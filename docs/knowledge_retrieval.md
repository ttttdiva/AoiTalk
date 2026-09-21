# Knowledge / Docs retrieval contracts

Retrieval uses the existing PostgreSQL manifests, Docs graph, Qdrant hybrid
index and authorized workspace/past-chat tools. It does not treat a relevance
search page as a census of the underlying corpus.

| Request | Access path | Evidence boundary |
| --- | --- | --- |
| Find material about a subject | `docs_search`, `knowledge_search`, `bm25_search`, `search_past_chats` | Ranked hits establish relevant examples, not a total. |
| Count or group matching documents | `docs_query`, `knowledge_query` | `total_matches` counts the complete authorized set satisfying the supplied structured filters before pagination. |
| Establish a timeline | Structured date filter/order, then read the selected documents | The timestamp's provenance must support the claim being made. |
| Summarize a broad period/corpus | Page through structured results and read evidence; use bounded repeated retrieval or Deep Research where appropriate | Report coverage and gaps; one top-k page cannot establish corpus-wide trends. |

Knowledge filters describe stored metadata. They cannot establish an exact count
of documents about an arbitrary semantic topic. Refine a question into supported
metadata conditions or explicitly describe the result as sampled retrieval.

## Docs scope and document reads

The implicit Project Context lane resolves the strict canonical Project
Information subtree and explicitly related/reference subtrees, plus permitted
Personal nodes. Containment means `parent_id` within the seed library, never
every node sharing `root_page_id`, a library, or a referenced Project. Every
emitted node retains its own current ACL. A denied/archived intermediate does
not erase an independently readable descendant; no private ancestor identity
is added to the document projection. Canonical relationship labels must match
the strict Project Information identity. The compact ContextBuilder identity
index opts out of subtree expansion and remains a bounded navigation index.

An explicit `project` selector retains the Project-only legacy selection;
omitting it in an enabled Project Context selects Project+Personal. These
selection modes are not permission grants. Search applies scope, current ACL
and tag filters before candidate limits. Exact identity/title matches precede
reciprocal-rank fusion of lexical and semantic lanes.

`docs_read(view="outline")` remains the compatibility/default navigation view,
including the specialized Inbox `revision`. It is not full document content.
Use `docs_read(view="document")` for ordered containment content, descendant
descriptions, editable Markdown/code, tags and named/identified typed Fields.
Its `segments` carry full node IDs, a kind, optional field/tag ID, and exact
character offsets. Concatenate text only within the same node/kind/field/tag.

Follow `next_cursor` with the same target, Project, depth and view while
`has_more` is true. For `view="edit"`, the cursor is an opaque, server-issued
checkpoint: submit the exact next token once, in order, and retry the exact
prior request after a lost response. Do not derive or modify it from
`read_fingerprint`; the server replays a committed page without advancing.
`coverage_complete` describes the constructed authorized
projection, not the number of pages an Agent has consumed. `coverage_reasons`
reports depth, node, content, field/tag, unsupported-format or traversal bounds.
The initial view caps at 500 nodes, depth 32, one million source characters,
200 stored fields and 100 tags per node. The default depth is 8. Attachments,
placements, backlinks and edges are explicitly outside this content view.

Every page rebuilds the live authorized projection; content, selected scope or
actor changes invalidate its cursor. `read_fingerprint` is a read pagination
fingerprint, not a graph snapshot or a write CAS token. The response expressly
sets `fingerprint_is_write_revision=false`. Generic node mutations do not
accept that fingerprint; the new changeset API uses a distinct edit write_token. Model-context shaping preserves complete segments and adjusts the
cursor to the first omitted segment, even with optional compression disabled.

## Docs semantic input migration

Docs index input version 4 contains only the node's own title, description,
typed content and same-library tags. Ancestor text is excluded because the
ancestor can have different permissions. Context is expanded after read
authorization instead. Qdrant no longer needs the plaintext `text` payload.

Search accepts only current input/model/dimension fingerprints. Existing points remain stored but are
unsearchable until the durable startup worker or `scripts/reindex_docs_qdrant.py`
reconciles them; lexical search continues during migration. Reconciliation forces legacy points through
upsert even when their body hash is unchanged, replacing the old payload. The
scoped semantic lane applies authorized payload node IDs before grouped Qdrant top-k
and checks live SQL eligibility again when hydrating, preserving vector order.
Bounded replenishment never turns relevance results into an exact corpus count.

Long record text produces overlapping deterministic spans. Search groups them
back to the canonical node. Source/model fingerprints avoid reusing incompatible
vectors; replacement succeeds before obsolete spans are removed. Bounded
embedding batches limit vector working memory. Candidate-ID materialization
still needs production-scale profiling.

Migration `20260908_0005` installs cross-writer revision and dirty-queue triggers.
A follow-on `20260908_0006` stores only durable edit-read session progress
(opaque tokens, page boundaries and revision/scope bindings), never canonical
body/plaintext responses. The final complete page creates the existing
`DocsReadLease` once and replays its token idempotently after a lost response.
A lifecycle-owned worker performs durable coalesced delivery and retries; Web,
Python and mobile sync changes all participate. `docs_read(view="edit")` issues
an actor/root/scope-bound write lease for `docs_mutate`. The atomic changeset
preserves unchanged IDs, checks library/policy revisions, and commits an
idempotent operation receipt with the graph updates. Generic mutations cannot
bypass Inbox/Mail/Memory or canonical Project ownership.
Migration `20260908_0007` makes the legacy fail-fast writer guard a no-op while
retaining those revision/index triggers. Agent mutations serialize with other
Agents, prelock their leased Docs/domain row set with `NOWAIT`, and return a
retryable conflict on contention; ordinary Task/Project/User/raw Docs writers
continue and are not rejected by Agent activity. `docs_overview` does not hold
this Agent advisory lock across long enumeration or page rendering.

`docs_search(expand=true)` supplies authorized local section context;
`docs_read(view="record")` reads one typed record and `view="neighborhood"`
provides bounded, ACL-filtered links and related records. `docs_overview`
maintains version-bound enumeration and evidence-delivery progress. Follow its
cursor and distinguish complete delivery from partial/budget-limited runs.
See `docs/adr/2026-09-08-docs-agent-contract.md` for rollout and limits.

## Counts and pagination

- `count` in Knowledge query output denotes matching documents; in the legacy
  Docs text header it denotes returned rows. Use `total_matches` for source totals.
- Continue using `next_offset` while `has_more` is true. Ordering has a unique
  ID tie-breaker. Offset pagination is deterministic for an unchanged corpus;
  concurrent edits can move rows between pages. It is not a historical snapshot.
- Multi-valued Knowledge tag/project groups are facets: one document can occur
  in several groups. Adding their counts can exceed the document total.
- Legacy Docs adapters lacking exact metadata report an unknown total. A
  bounded list's length is never promoted to an exact total.
- Model-context shaping preserves source counts separately from omitted rows
  and groups, including when optional context compression is disabled. UI/tool
  history retains the original output.
- Docs returns a fixed-position `query_metadata` JSON line before rendered
  content. Compression takes exact group totals only from that metadata;
  document text resembling a count header remains document text.

## Dates and synchronization

Knowledge's `document_date` uses frontmatter, filename calendar date, GROWI
metadata, then current-source mtime, in that order. Values are UTC and
`document_date_source` identifies the derivation. A modification time does not
prove when a decision or policy actually took effect.

Historical naive mtimes have no reliable timezone information. Forward migration
`20260908_0003` repairs the earlier backfill using retained semantic metadata;
ambiguous dates remain unknown until source sync yields trustworthy date evidence.
Inspect date-source groups when assessing timeline
coverage. Date-range filters exclude documents whose effective date is unknown.

Docs built-in dates refer to node creation/update or daily-note date, not
Knowledge semantic dates. Read the content before treating a node update as a
business event.

A source scan that reaches its work limit is incomplete. It must preserve
unvisited documents and report the partial result. It cannot establish deletions
or a complete refreshed corpus.

## Derived index and fallback

Dense embedding input deterministically includes source identity, document
title/path, tags and project/task references, heading path, chunk index and body.
`content_hash` hashes that exact input; `chunk_content_hash` remains body-only.
The embedding fingerprint additionally distinguishes model/input format.
Unchanged dense vectors can be reused; sparse input and RRF fusion remain intact.

Index synchronization validates embeddings and writes replacement points before
removing stale points. An incompatible or uninspectable collection is retained
and requires explicit operator migration. Source synchronization upgrades old
body-only embeddings and removes obsolete plaintext body payloads. Canonical
content is read through the authorized DB/source path.

Source ACLs also restrict Qdrant candidates before the relevance limit; database
authorization is rechecked when hydrating hits. Search recall is still bounded
by the configured relevance window and is not an exact count contract.

Lexical fallback scans eligible chunks exhaustively with a streaming cursor and
a bounded result heap. This prevents a newer candidate window from hiding old
matches, and bounds application working memory. Corpus-wide I/O and authorized
text decryption still cost time; the derived index remains the primary semantic
retrieval path.

## Verification

The database regression tests execute actual SQL over disposable schemas or
read-only CTE fixtures. They do not use the ordinary application database as a
fixture store. Use an isolated PostgreSQL 16+ instance and these explicit opt-ins:

- `AOITALK_KNOWLEDGE_TEST_DATABASE_URL`: Knowledge SQL and date-repair tests.
- `AOITALK_TEST_POSTGRES_DSN`: Docs SQL/point-ACL parity tests, read-only.
- `DOCS_QUERY_TEST_POSTGRES=1`: Docs query integration tests; their guard also
  verifies the task-owned local verification instance and provenance marker.

`tests/test_knowledge_index_lifecycle.py` uses in-memory Qdrant, including write
failures, cancellation, cross-loop teardown, unchanged embedding reuse, and
preservation of other sources. The Knowledge database suite also drives the
real public tool through concurrent thread/event-loop dispatch.
