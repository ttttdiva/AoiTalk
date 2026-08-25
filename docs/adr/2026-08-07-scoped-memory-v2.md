# ADR: Scoped Memory v2 as the sole memory source of truth

- Status: Accepted
- Date: 2026-08-07
- Owners: Memory subsystem

## Context

Memory currently has overlapping persistence and retrieval paths:

- Dreaming extracts conversation facts into `context_memories`, historically as user-scoped rows.
- Agent Memory creates `agent_memory_root` and `agent_memory:<project_id>` Docs nodes, then writes one entry per child node.
- `ContextBuilder` reads both stores and previously created missing Agent Memory Docs during prompt construction.
- Project Information injects a canonical Docs page plus a broad set of project nodes, which can re-inject Agent Memory, inbox, mail, and file-reference content.
- `search_past_chats` concatenates Dreaming hits before conversation hits instead of ranking them together.

This produces duplicate context, write side effects on a read path, unclear scope ownership, and no uniform correction, lineage, evidence, or permission model.

## Decision

`context_memories` becomes the only writable memory source of truth through `ScopedMemoryService`.

1. All global, user, project, task, and session memory mutations pass through the same classify, sensitivity, dedupe, correction, lineage, audit, and indexing pipeline.
2. `ContextBuilder` and retrieval/tracing code are read-only. They fetch Scoped Memory once per build and never create Docs, commit, or update `last_used_at`.
3. Legacy Agent Memory Docs remain readable during migration behind a feature flag, but are system-managed, hidden from the normal sidebar, non-revivable, and not writable by generic Docs tools or REST operations.
4. Project Information context is limited to the canonical subtree and nodes explicitly opted into the project-information domain. Managed Agent Memory, inbox, mail, and workspace-file domains are excluded.
5. Promotion to Project Information is an explicit operation. It uses `DocsGraphService` and records
   source references. The former `body_json.verbatim_blocks` representation is legacy migration input
   only; visible content is materialized as editable typed blocks by the ClipIngest migration and is
   never promoted automatically by this ADR.
6. `search_past_chats` ranks Scoped Memory and conversation results with one comparable score.

## Rollout flags

- `SCOPED_MEMORY_V2_ENABLED` selects the v2 service.
- `LEGACY_AGENT_MEMORY_READ_ENABLED` permits temporary read-only legacy context.
- `LEGACY_AGENT_MEMORY_WRITE_ENABLED` defaults to false. Only an explicit system migration may bypass it.

## Migration and rollback

Inventory and migration are separate. Inventory and default migration mode are read-only. `--apply` creates Scoped Memory rows with a migration id and evidence linking the original node, path, and revision; it does not delete Docs or auto-promote Project Information. Reruns are idempotent.

Rollback disables `SCOPED_MEMORY_V2_ENABLED` and re-enables legacy reads. Applied rows can be located by migration id and soft-forgotten using the rollback mapping; legacy Docs remain intact.

## Consequences

Memory writes become explainable and scope-safe, while legacy documents can be migrated without destructive classification. Generic Docs mutations now reject managed domains and callers must use the owning domain tool.
