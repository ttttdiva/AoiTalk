# ADR: ContextManifest shadow contract (current implementation)

- Status: Accepted (implemented)
- Date: 2026-08-30
- Owners: Work Intelligence Plane / context observability
- Companion: `2026-08-30-work-intelligence-plane-architecture-contract.md`

## Purpose and boundary

`ContextManifest` is a sanitized, one-way observation of a turn that has already
been authorized and assembled by `ContextBuilder`. It extends the existing
`context_snapshot` observation path and is persisted only as optional metadata
on an existing assistant `ConversationMessage`.

A Manifest is never:

* provider prompt input, tool input, model/router input, or a ContextPackage;
* an ACL decision, authorization bearer, scope capability, or resource ID;
* a memory mutation command or an execution plan/scope; or
* a replacement for Project, Task, Docs, Conversation, Memory, AgentRun, or
  provider state.

The Manifest can explain what was selected for Inference and correlate it with
an inspector. It cannot make an inaccessible row visible or make a stale row
current.

## Versions and compatibility

The canonical dimensions are:

```text
schema_version:   "1.0"
producer_version: "ws1-shadow-1" | "wi-core-1"
sanitizer_version: "1.0"
mode:             "shadow"
```

`ws1-shadow-1` is retained for historical rows produced before the typed Work
Intelligence sidecar existed. Its serialized shape remains valid and is not
rewritten. The current builder switches to `wi-core-1` only when a validated
Work Intelligence sidecar is present. A legacy `ws1-shadow-1` payload that
contains a sidecar is rejected; a `wi-core-1` payload must contain the exact
sidecar shape. Readers must explicitly recognize the three version dimensions
and each supported producer value, and reject or ignore unsupported
combinations; they must not guess a conversion.

`serialize_context_manifest()` omits the optional sidecar when it is `None`,
which preserves the exact legacy shape. `validate_context_manifest_metadata()`
checks exact keys, allowlists, canonical hashes, reproducibility hashes, and the
final Manifest hash before a payload is accepted.

## Source and lifecycle

The Manifest is built from already-resolved observations:

1. `TurnContext` supplies the immutable actor, turn reference, Project/Task/
   Session scope, explicit references, and Project Context flags.
2. The current `ContextBundle` supplies layer traces, authorized index
   provenance, scoped-memory lineage, and the transient
   `WorkIntelligenceResult` sidecar.
3. Provider request snapshots supply structural request/model/token observations.
4. The builder sanitizes, hashes, bounds, and validates the result; runtime
   timestamps/durations are not used as reproducibility inputs.

Provider metadata is produced after the current request observation exists. The
existing providers add the Manifest to their existing `get_generation_metadata`
or assistant memory-metadata seam. Native AgentLLM uses its existing assistant
message persistence path. Free Team validates and retains the selected target's
metadata. A provider without a safe structured observation source may omit the
Manifest; no provider calls `ContextBuilder` merely to manufacture telemetry.

Before a provider clears a bundle or resets a turn, the capture helper may put
one validated Manifest in the latest in-memory snapshot under a turn-identity
marker. The marker is stripped from persisted snapshots, and a capture is reused
only when its hashed turn identity matches the current `TurnContext`. There is
no client-level `last_manifest` state to copy across forks/resets; existing
conversation fork/reset code removes turn metadata at its normal branch boundary.

Manifest generation, serialization, and metadata attachment are optional
observability. An exception is logged generically and swallowed so a successful
chat turn remains successful.

## Canonical serialization and hashes

Stable JSON is UTF-8 with:

```text
ensure_ascii=False
sort_keys=True
separators=(",", ":")
allow_nan=False
```

`observation_ref_hash(kind, value)` uses the domain-separated payload:

```text
SHA256("aoitalk.context_manifest.ref.v1" + NUL + normalized_kind + NUL + value)
```

The serialized form is a lower-case `sha256:<64 hexadecimal characters>`
correlation hash. A hash is for reproducibility and lookup intersection only;
it must never be passed to an ACL, tool, memory mutation, router, or execution
API as an identifier or proof.

The transient `WorkIntelligenceCompiler` uses its own
`aoitalk.work_intelligence.ref.v1` domain for item/evidence/relationship
correlation hashes. The `wi-core-1` sidecar preserves those opaque values;
Manifest `ResourceRef`/`EvidenceRef` values use the
`aoitalk.context_manifest.ref.v1` domain. Inspector reference resolution
intersects Work Intelligence hashes with a fresh compile and never treats
either hash family as an ACL identifier.

The Manifest contains three reproducibility hashes:

* `turn`: sanitized `SubjectContext`;
* `context`: sanitized resources, evidence, policy decisions, layers, budget,
  omission counts, and (for `wi-core-1`) the Work Intelligence sidecar; and
* `requests`: sanitized provider request projections.

`manifest_hash` is the stable hash of the complete serialized payload excluding
itself. Source version/freshness values remain in the projection; wall-clock
capture timestamps and retrieval durations do not change the reproducible
projection.

## Canonical Manifest fields

The top-level payload contains:

* `subject`: hashed actor/turn references, only scope references that remain
  consistent with trusted `TurnContext`, and the include/strict/suppression/
  verified-attachment flags;
* `resources`: allowlisted kind, `ref_hash`, relation, source, version,
  freshness, and optional supersedes hash;
* `evidence`: opaque locator hashes with allowlisted kind/source/version;
* `policy_decisions`: an observation of `TurnContext`,
  `ProjectContextResolver`, `ConversationSession`, `Task`,
  `ProjectKnowledgeService`, or `DocsScope` decisions;
* `layers`: `ContextBundle` categories and sources with active/deferred/failed
  status, inclusion reason, retrieved/selected character counts, and explicit
  `budget_clip` transform when clipped;
* `requests`: bounded provider/model/context-window/token observations and
  sorted component categories, each with its own request hash;
* `bundle_char_budget`, `omission_reason_counts`, and `reproducibility_hashes`;
  and
* `manifest_hash`, plus optional `work_intelligence` described below.

No field is an authorization decision. `policy_decisions` records what an
existing authority reported; it does not become one.

## Work Intelligence sidecar (`wi-core-1`)

`ContextBuilder` invokes `WorkIntelligenceCompiler` after normal Project/
Task/Session re-authorization and scoped-memory retrieval. The compiler is
read-only and emits a bounded transient result. `_manifest_work_intelligence_projection`
accepts it only when the bundle and Project Context are trusted, calls
`to_dict(hashed=True)`, and retains only:

* items: kind, correlation hash, status/priority/score, version/freshness,
  uncertainty/conflict flags, and evidence-reference hashes;
* people: person hashes, evidence counts/scores, and uncertainty;
* evidence: kind, locator hash, source type, version/freshness, and uncertainty;
* relations: allowlisted relation type, subject/target hashes, evidence hashes,
  confidence, and uncertainty;
* aggregate freshness and bounded omission counts; and
* a structural Work Intelligence layer with selected character count, item
  count, and evidence count.

The sidecar never stores Work Intelligence titles, names, bodies, raw IDs,
query text, advisory-memory text, or compiler trace text. Its validator enforces
exact keys, finite numeric values, bounded arrays, allowlisted tokens, and
canonical `sha256:` hashes. Presence of the sidecar is included in the context
reproducibility hash and selects producer `wi-core-1`.

Work Intelligence semantics remain live and advisory: item/evidence
version/freshness, relationship evidence, weak/uncertain observations, and
`advisory_conflict` are descriptive only. Current Task/Docs state wins over an
advisory memory conflict. The compiler has no ambient cache; a later turn can
observe a changed row, while a persisted sidecar remains a historical
observation.

## Scope trust and ACL hardening

`_trusted_subject()` compares bundle debug identity and authorization markers
with immutable `TurnContext`. A Project/Task/Session mismatch, malformed
authorization marker, or unverified Task→Project binding prevents that scope
from entering the Manifest and increments a bounded omission count. Project
Context OFF removes Project/Task/Work Intelligence layers and their related
provenance before rendering and snapshotting. Explicit references are projected
only as hashed, allowlisted references; a hash never grants access.

Project Knowledge and accessible Docs indexes are projected only when their
manifest-only provenance hashes match the trusted Project hash. Scoped-memory
lineage is accepted only for the actor or matching session/Project/Task scope.
Docs visibility is established by the existing `DocsScope`/`docs_acl` path before
an index reaches the bundle. Manifest construction does not reimplement those
ACLs. Adjacent hardening similarly validates canonical Project Information
pointers, active Project-member assignees, project-bound group-chat users, and
conversation/Task/AgentRun origins before those values can reach a projection;
the Manifest only observes the resulting authorized state.

## Hash-only observation versus live inspector references

The persisted Manifest and API inspector are intentionally different products:

* The Manifest/inspector projection exposes only bounded hashes, structural
  counts, versions/freshness, relations, policy outcomes, and omission reasons;
  it does not expose prompt text, source bodies, hidden reasoning, raw IDs,
  credentials, provider continuation IDs, or exception text.
* `GET /api/python-proxy/conversations/{session_id}/context-snapshot` first
  authenticates the session/Project and validates the selected active-branch
  Manifest. It then calls
  `resolve_work_intelligence_references()` with the current actor, Project, and
  Session. That helper recompiles current authorized Work Intelligence and
  intersects its current item hashes with the historical sidecar hashes.
* Only still-live, still-selected rows receive display labels and route links
  (`/tasks/<id>` or `/docs/<id>`) in the response. Revoked, archived, deleted,
  or otherwise missing rows disappear; no inaccessible-item count or raw
  Manifest locator is returned. These links are an ephemeral authorized API
  projection, not persisted Manifest authority.

The frontend `WorkIntelligenceInspector` renders the structured inspector and
current authorized references, never a JSON dump of the canonical Manifest.
Binding requires the requested session/message and active branch. Correlation
hashes are displayed as non-authorizing diagnostics.

## Work Intelligence and provider request observations

`ContextBundle.render_with_trace()` keeps one shared budget and stable layer
order. It records selected/retrieved counts and item-level hash provenance;
`context_snapshot` records the exact provider payload shape, tools, and dynamic
context categories. Manifest generation never parses rendered provider text to
rediscover provenance and never feeds the Manifest back into a request.

The normal chat compiler path remains one canonical `ContextBuilder` path per
provider turn/request: Native AgentLLM, Gemini, Ollama, SGLang,
OpenAI-compatible local, and CLI use provider bridges around the same builder;
Free Team delegates to its selected target. Provider retries may issue more
requests while reusing the active bundle, but do not introduce a second
compiler. Strict tool-free/internal helper paths may intentionally omit chat
context and therefore produce no Work Intelligence sidecar. Provider-specific
transport/cache/model metadata remains owned by each provider. ChatGPT Web is a
Director-only interaction path, not an additional normal Manifest compiler.

## Config defaults and rollback

The fresh configuration defaults are literal booleans:

```yaml
work_intelligence:
  enabled: true
  rollback: false
  context_manifest:
    enabled: true
    shadow_mode: true
    persist_metadata: true
```

Compiler bounds default to `max_items=8`, `max_people=8`, `max_evidence=24`, and
`max_chars=3600` (with runtime clamps). Work Intelligence's rollout gate accepts
literal `True` only for `work_intelligence.enabled` (and optional
`work_intelligence.rollout.enabled`) and rejects a literal `rollback: true`.
Manifest persistence additionally requires all three literal context-manifest
booleans above. Missing or malformed values choose the legacy/no-persist path;
string values such as `"true"` do not enable it.

Immediate rollback is:

```text
work_intelligence.enabled = false
# or
work_intelligence.rollback = true
```

For Manifest metadata specifically, set
`work_intelligence.context_manifest.enabled=false` (or either companion flag
false). This stops new persistence without changing the existing prompt,
provider request, ACL, or memory mutation path. Existing manifests are inert
historical metadata and grant no authority; code reversion is optional.

## Failure, retention, and non-goals

Availability is fail-open for optional observation: Manifest construction,
validation, and metadata persistence failures do not turn a successful chat
request into a failure. Disclosure is fail-closed: unsupported versions,
unknown fields, untrusted scopes, invalid hashes, unsanitizable values, and
malformed provider metadata are omitted or rejected. Raw exceptions are never
stored in the Manifest.

This Manifest sanitizer is not a general source-content redactor. In
particular, the owner-scoped Project Steward collector applies bounds and
secret-like-key filtering to its structured evidence, but selected authorized
chat/Task/Docs text may still be sent to its configured read-only model; the
Manifest simply does not persist that text.

No new store, table, migration, graph DB, event bus, WorkRun, router, or
Capability Gateway is implied. The only durable Manifest representation is
existing assistant `ConversationMessage.message_metadata`; AgentRun result
sanitization retains only a validated hash/reference summary when it observes a
Manifest. Work Graph, richer ContextPackage/compiler migration, and other future
platform concepts require separate contracts and rollback decisions.
