"""Cross-writer Docs revisions, durable indexing and Agent protocol state.

Revision ID: 20260908_0005
Revises: 20260908_0004

No canonical content is rewritten. A statement-level writer guard protects all
participating domain tables. Agent planning/model work happens outside this
short database critical section. Library revisions deliberately conflict
conservatively rather than miss child/Field/placement changes. The trigger
uses a non-blocking advisory attempt and raises SQLSTATE 40001 when another
transaction owns the Agent lock; this prevents a legacy row-lock-first writer
from forming an advisory/row-lock deadlock cycle.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260908_0005"
down_revision = "20260908_0004"
branch_labels = None
depends_on = None

CONTENT_TABLES = (
    "docs_libraries", "knowledge_nodes", "knowledge_fields", "knowledge_field_values",
    "knowledge_supertags", "knowledge_node_supertags", "knowledge_supertag_fields",
    "knowledge_node_placements", "knowledge_edges", "knowledge_attachments", "tasks", "project_qa_entries",
)
POLICY_TABLES = ("users", "projects", "project_members", "knowledge_node_shares", "project_knowledge_refs")


def upgrade():
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = bind.dialect.identifier_preparer.quote(schema)
    definitions = {
        "docs_authority_state": "id integer PRIMARY KEY CHECK(id=1), policy_revision bigint NOT NULL DEFAULT 0",
        "docs_library_revisions": "library_id uuid PRIMARY KEY, revision bigint NOT NULL DEFAULT 0",
        "docs_index_queue": "library_id uuid PRIMARY KEY, requested_revision bigint NOT NULL DEFAULT 0, applied_revision bigint NOT NULL DEFAULT -1, updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "docs_read_leases": "id uuid PRIMARY KEY, actor_id uuid NOT NULL, root_id uuid NOT NULL, library_id uuid NOT NULL, revision bigint NOT NULL, policy_revision bigint NOT NULL, node_ids json NOT NULL, scope_binding varchar(64) NOT NULL, expires_at timestamp NOT NULL",
        "docs_mutation_receipts": "operation_id uuid PRIMARY KEY, actor_id uuid NOT NULL, root_id uuid NOT NULL, request_hash varchar(64) NOT NULL, result_json json NOT NULL, created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "docs_coverage_runs": "id uuid PRIMARY KEY, actor_id uuid NOT NULL, scope_binding varchar(64) NOT NULL, state_json json NOT NULL, expires_at timestamp NOT NULL",
    }
    for table, columns in definitions.items():
        op.execute(f"CREATE TABLE IF NOT EXISTS {quoted}.{table} ({columns})")
    op.execute("INSERT INTO docs_authority_state(id) VALUES(1) ON CONFLICT DO NOTHING")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_read_leases_actor_id ON docs_read_leases(actor_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_read_leases_expires_at ON docs_read_leases(expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_coverage_runs_expires_at ON docs_coverage_runs(expires_at)")
    op.execute(f"""
CREATE OR REPLACE FUNCTION {quoted}.docs_write_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
BEGIN
    -- Existing human/domain writers can arrive here while already holding a
    -- row lock (for example after SELECT ... FOR UPDATE).  Agent mutations
    -- take this transaction lock before their row locks.  Waiting here would
    -- therefore create the classic advisory -> row / row -> advisory cycle.
    -- Fail fast with PostgreSQL's retryable serialization state instead.  The
    -- Docs tools already surface 40001 as retryable, and raw callers can retry
    -- the whole transaction without leaving a partial revision behind.
    IF NOT pg_try_advisory_xact_lock(1146045267, 1) THEN
      RAISE EXCEPTION 'Docs writer lock is busy; retry the transaction'
        USING ERRCODE = '40001', HINT = 'Retry the complete Docs write transaction';
    END IF;
    RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION {quoted}.docs_touch_library(lib uuid) RETURNS void
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
DECLARE rev bigint;
BEGIN
    IF lib IS NULL THEN RETURN; END IF;
    INSERT INTO docs_library_revisions(library_id, revision) VALUES(lib, 1)
      ON CONFLICT(library_id) DO UPDATE SET revision=docs_library_revisions.revision+1
      RETURNING revision INTO rev;
    INSERT INTO docs_index_queue(library_id, requested_revision) VALUES(lib, rev)
      ON CONFLICT(library_id) DO UPDATE SET requested_revision=EXCLUDED.requested_revision,
        updated_at=CURRENT_TIMESTAMP;
END $$;
CREATE OR REPLACE FUNCTION {quoted}.docs_row_libraries(tab text, row_data jsonb) RETURNS SETOF uuid
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
DECLARE key text; lib uuid;
BEGIN
    IF row_data IS NULL THEN RETURN; END IF;
    IF tab='docs_libraries' THEN RETURN NEXT (row_data->>'id')::uuid; END IF;
    IF row_data->>'docs_library_id' IS NOT NULL THEN RETURN NEXT (row_data->>'docs_library_id')::uuid; END IF;
    FOREACH key IN ARRAY ARRAY['node_id','source_node_id','target_node_id','parent_id','parent_node_id','knowledge_node_id'] LOOP
      IF row_data->>key IS NOT NULL THEN
        SELECT docs_library_id INTO lib FROM knowledge_nodes WHERE id=(row_data->>key)::uuid;
        IF FOUND THEN RETURN NEXT lib; END IF;
      END IF;
    END LOOP;
    IF row_data->>'field_id' IS NOT NULL THEN
      SELECT docs_library_id INTO lib FROM knowledge_fields WHERE id=(row_data->>'field_id')::uuid;
      IF FOUND THEN RETURN NEXT lib; END IF;
    END IF;
    IF row_data->>'supertag_id' IS NOT NULL THEN
      SELECT docs_library_id INTO lib FROM knowledge_supertags WHERE id=(row_data->>'supertag_id')::uuid;
      IF FOUND THEN RETURN NEXT lib; END IF;
    END IF;
    IF tab='project_qa_entries' THEN
      SELECT n.docs_library_id INTO lib FROM projects p JOIN knowledge_nodes n ON n.id=p.knowledge_node_id
        WHERE p.id=(row_data->>'project_id')::uuid;
      IF FOUND THEN RETURN NEXT lib; END IF;
    END IF;
END $$;
CREATE OR REPLACE FUNCTION {quoted}.docs_record_change() RETURNS trigger
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
DECLARE before_row jsonb; after_row jsonb; lib uuid; keys text[];
BEGIN
    IF TG_OP!='INSERT' THEN before_row=to_jsonb(OLD); END IF;
    IF TG_OP!='DELETE' THEN after_row=to_jsonb(NEW); END IF;
    IF TG_OP='UPDATE' AND before_row=after_row THEN RETURN NULL; END IF;
    IF TG_ARGV[0]='policy' THEN
      keys = CASE TG_TABLE_NAME
        WHEN 'users' THEN ARRAY['role','is_active','session_version','is_password_reset_required']
        WHEN 'projects' THEN ARRAY['owner_id','deleted_at','is_completed','knowledge_node_id','name']
        WHEN 'project_members' THEN ARRAY['project_id','user_id','role','permissions']
        WHEN 'knowledge_node_shares' THEN ARRAY['node_id','user_id','project_id','permission']
        WHEN 'project_knowledge_refs' THEN ARRAY['project_id','knowledge_node_id','relation_type']
        ELSE ARRAY[]::text[] END;
      IF TG_OP!='UPDATE' OR EXISTS(SELECT 1 FROM unnest(keys) k WHERE before_row->k IS DISTINCT FROM after_row->k) THEN
        UPDATE docs_authority_state SET policy_revision=policy_revision+1 WHERE id=1;
      END IF;
    END IF;
    IF TG_OP='UPDATE' AND TG_TABLE_NAME IN ('tasks','projects') THEN
      keys = CASE TG_TABLE_NAME
        WHEN 'tasks' THEN ARRAY['title','description','status','priority','start_at','end_at','project_id','knowledge_node_id','deleted_at']
        ELSE ARRAY['name','owner_id','deleted_at','is_completed','knowledge_node_id'] END;
      IF NOT EXISTS(SELECT 1 FROM unnest(keys) k WHERE before_row->k IS DISTINCT FROM after_row->k) THEN
        RETURN NULL;
      END IF;
    END IF;
    FOR lib IN SELECT DISTINCT value FROM (
      SELECT docs_row_libraries(TG_TABLE_NAME,before_row) value
      UNION ALL SELECT docs_row_libraries(TG_TABLE_NAME,after_row) value
    ) candidates WHERE value IS NOT NULL ORDER BY value LOOP
      PERFORM docs_touch_library(lib);
    END LOOP;
    RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION {quoted}.docs_record_truncate() RETURNS trigger
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
DECLARE lib uuid;
BEGIN
    UPDATE docs_authority_state SET policy_revision=policy_revision+1 WHERE id=1;
    FOR lib IN SELECT library_id FROM docs_library_revisions ORDER BY library_id LOOP
      PERFORM docs_touch_library(lib);
    END LOOP;
    RETURN NULL;
END $$;
""")
    for table in CONTENT_TABLES + POLICY_TABLES:
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"{schema}.{table}"}).scalar() is None:
            continue
        kind = "policy" if table in POLICY_TABLES else "content"
        op.execute(f"CREATE TRIGGER docs_write_guard BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {quoted}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {quoted}.docs_write_guard()")
        op.execute(f"CREATE TRIGGER docs_record_change AFTER INSERT OR UPDATE OR DELETE ON {quoted}.{table} FOR EACH ROW EXECUTE FUNCTION {quoted}.docs_record_change('{kind}')")
        op.execute(f"CREATE TRIGGER docs_record_truncate AFTER TRUNCATE ON {quoted}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {quoted}.docs_record_truncate()")
    op.execute("INSERT INTO docs_library_revisions(library_id) SELECT id FROM docs_libraries ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO docs_index_queue(library_id,requested_revision) SELECT library_id,revision FROM docs_library_revisions ON CONFLICT DO NOTHING")


def downgrade():
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = bind.dialect.identifier_preparer.quote(schema)
    for table in CONTENT_TABLES + POLICY_TABLES:
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"{schema}.{table}"}).scalar() is not None:
            for trigger in ("docs_write_guard", "docs_record_change", "docs_record_truncate"):
                op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {quoted}.{table}")
    for function in ("docs_record_truncate()", "docs_record_change()", "docs_row_libraries(text,jsonb)", "docs_touch_library(uuid)", "docs_write_guard()"):
        op.execute(f"DROP FUNCTION IF EXISTS {quoted}.{function}")
    for table in ("docs_coverage_runs", "docs_mutation_receipts", "docs_read_leases", "docs_index_queue", "docs_library_revisions", "docs_authority_state"):
        op.execute(f"DROP TABLE IF EXISTS {quoted}.{table}")
