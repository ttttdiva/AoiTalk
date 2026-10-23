"""Docs hierarchy visibility invalidation for optional Sync v3 revision 0003.

Only visibility-affecting edits walk a subtree. UNION on node identity bounds
malformed cycles; set-based dependent selection avoids per-descendant queries.
All emissions commit/rollback with the originating business transaction.
"""
DOCS_VISIBILITY_TRIGGERS = (
    ("knowledge_nodes", "sync_v3_dep_docs_visibility", ("UPDATE", "DELETE")),
    ("knowledge_node_shares", "sync_v3_dep_docs_share", ("INSERT", "UPDATE", "DELETE")),
)

DOCS_VISIBILITY_SQL = """
CREATE OR REPLACE FUNCTION sync_v3_emit_docs_visibility(p_id uuid)
RETURNS void LANGUAGE sql AS $fn$
  WITH RECURSIVE affected(id) AS (
    SELECT p_id
    UNION
    SELECT n.id FROM knowledge_nodes n JOIN affected a ON n.parent_id = a.id
  ), affected_tags(id) AS (
    SELECT DISTINCT supertag_id FROM knowledge_node_supertags WHERE node_id IN (SELECT id FROM affected)
  ), affected_fields(id) AS (
    SELECT id FROM knowledge_fields WHERE supertag_id IN (SELECT id FROM affected_tags)
    UNION SELECT field_id FROM knowledge_supertag_fields WHERE supertag_id IN (SELECT id FROM affected_tags)
    UNION SELECT field_id FROM knowledge_field_values WHERE node_id IN (SELECT id FROM affected)
  ), entries(t, id, library) AS (
    SELECT 'knowledge_nodes', n.id::text, n.docs_library_id
      FROM knowledge_nodes n WHERE n.id IN (SELECT id FROM affected)
    UNION
    SELECT 'knowledge_node_supertags', r.node_id::text || ':' || r.supertag_id::text, n.docs_library_id
      FROM knowledge_node_supertags r JOIN knowledge_nodes n ON n.id = r.node_id
      WHERE r.node_id IN (SELECT id FROM affected)
    UNION
    SELECT 'knowledge_supertags', r.id::text, r.docs_library_id
      FROM knowledge_supertags r WHERE r.id IN (SELECT id FROM affected_tags)
    UNION
    SELECT 'knowledge_fields', r.id::text, r.docs_library_id
      FROM knowledge_fields r WHERE r.id IN (SELECT id FROM affected_fields)
    UNION
    SELECT 'knowledge_supertag_fields', r.supertag_id::text || ':' || r.field_id::text, t.docs_library_id
      FROM knowledge_supertag_fields r JOIN knowledge_supertags t ON t.id = r.supertag_id
      WHERE r.supertag_id IN (SELECT id FROM affected_tags)
    UNION
    SELECT 'knowledge_field_values', r.node_id::text || ':' || r.field_id::text, n.docs_library_id
      FROM knowledge_field_values r JOIN knowledge_nodes n ON n.id = r.node_id
      WHERE r.node_id IN (SELECT id FROM affected) OR r.target_node_id IN (SELECT id FROM affected)
    UNION
    SELECT 'knowledge_node_placements', r.id::text, n.docs_library_id
      FROM knowledge_node_placements r JOIN knowledge_nodes n ON n.id = r.node_id
      WHERE r.node_id IN (SELECT id FROM affected) OR r.parent_node_id IN (SELECT id FROM affected)
    UNION
    SELECT 'knowledge_edges', r.id::text, n.docs_library_id
      FROM knowledge_edges r JOIN knowledge_nodes n ON n.id = r.source_node_id
      WHERE r.source_node_id IN (SELECT id FROM affected) OR r.target_node_id IN (SELECT id FROM affected)
  )
  SELECT sync_v3_emit(t, id, 'U', NULL, NULL, library, NULL) FROM entries
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_dep_docs_visibility()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  PERFORM sync_v3_require_read_committed();
  IF TG_OP = 'DELETE' THEN
    PERFORM sync_v3_emit_docs_visibility(OLD.id);
    RETURN OLD;
  END IF;
  IF OLD.parent_id IS DISTINCT FROM NEW.parent_id
     OR OLD.docs_library_id IS DISTINCT FROM NEW.docs_library_id
     OR OLD.project_id IS DISTINCT FROM NEW.project_id
     OR OLD.archived_at IS DISTINCT FROM NEW.archived_at
     OR OLD.system_key IS DISTINCT FROM NEW.system_key
     OR OLD.node_type IS DISTINCT FROM NEW.node_type
     OR OLD.is_explicit_blank IS DISTINCT FROM NEW.is_explicit_blank THEN
    PERFORM sync_v3_emit_docs_visibility(NEW.id);
  END IF;
  RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_dep_docs_share()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  PERFORM sync_v3_require_read_committed();
  IF TG_OP <> 'INSERT' THEN
    PERFORM sync_v3_emit_docs_visibility(OLD.node_id);
  END IF;
  IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND OLD.node_id IS DISTINCT FROM NEW.node_id) THEN
    PERFORM sync_v3_emit_docs_visibility(NEW.node_id);
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$fn$;
"""


def build_docs_visibility_sql() -> str:
    from .schema_sql import _create_triggers

    return DOCS_VISIBILITY_SQL + "\n".join(
        _create_triggers(table, name, name, events)
        for table, name, events in DOCS_VISIBILITY_TRIGGERS
    )
