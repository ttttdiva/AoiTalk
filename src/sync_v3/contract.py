"""Sync v3 共有契約（改訂3）。S1/S2/S3で同一内容。変更は統合タスクのみ。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

CONTRACT_REVISION = 3
PROTOCOL_VERSION = 3
MAX_OPS_PER_REQUEST = 50
MAX_OPS_BODY_BYTES = 512 * 1024
CHANGES_PAGE_LIMIT = 500
BOOTSTRAP_PAGE_LIMIT = 500
PAGE_BYTE_BUDGET = 2 * 1024 * 1024
CHANGE_LOG_RETENTION_DAYS = 30
MAX_SEQ = 9223372036854775807
SYNC_DISABLED_CODE = "sync_disabled"
CURSOR_EXPIRED_CODE = "cursor_expired"
SYNC_COMMIT_LOCK_KEY = 4163026101

SYNC_TABLES: tuple[str, ...] = (
    "spaces", "projects", "tasks", "task_occurrences", "time_entries",
    "conversation_sessions", "conversation_messages",
    "record_tables", "record_fields", "record_rows",
    "knowledge_nodes", "knowledge_supertags", "knowledge_node_supertags",
    "knowledge_supertag_fields", "knowledge_fields", "knowledge_field_values",
    "knowledge_node_placements", "knowledge_edges",
)
DOCS_TABLES: tuple[str, ...] = SYNC_TABLES[10:]
SCOPE_TABLES: dict[str, tuple[str, ...]] = {
    "user": ("conversation_sessions", "conversation_messages"),
    "space": ("spaces",),
    "project": ("projects", "tasks", "task_occurrences", "time_entries",
                "record_tables", "record_fields", "record_rows"),
    "docs": DOCS_TABLES,
}

OpAction = Literal["create", "update", "delete", "restore", "reorder"]
OpStatus = Literal["applied", "applied_partial", "rejected", "retry"]
OpCode = Literal["field_conflict", "entity_deleted", "forbidden", "invalid",
                 "unsupported", "server_error"]
LoadState = Literal["row", "missing", "hidden"]

# 1変更1行。UPDATEでscope列が変わった場合は旧scope列の行と新scope列の行を2行記録する。
# commit_seq はcommit直前に下の SYNC_COMMIT_ORDER_SQL が設定する。NULLの行は未commit。
SYNC_CHANGE_LOG_DDL = """
CREATE SEQUENCE IF NOT EXISTS sync_commit_seq;
CREATE TABLE IF NOT EXISTS sync_change_log (
  seq bigserial PRIMARY KEY,
  txid xid8 NOT NULL DEFAULT pg_current_xact_id(),
  commit_seq bigint NULL,
  committed_at timestamptz NULL,
  entity_table text NOT NULL,
  entity_id text NOT NULL,
  op char(1) NOT NULL CHECK (op IN ('U','D')),
  owner_user_id uuid NULL,
  project_id uuid NULL,
  docs_library_id uuid NULL,
  space_id uuid NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sync_change_log_pos ON sync_change_log (commit_seq, seq) WHERE commit_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_uncommitted ON sync_change_log (txid) WHERE commit_seq IS NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_owner ON sync_change_log (owner_user_id, commit_seq) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_project ON sync_change_log (project_id, commit_seq) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_library ON sync_change_log (docs_library_id, commit_seq) WHERE docs_library_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_space ON sync_change_log (space_id, commit_seq) WHERE space_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sync_change_log_committed_at ON sync_change_log (committed_at) WHERE commit_seq IS NOT NULL;
CREATE TABLE IF NOT EXISTS sync_entity_versions (
  entity_table text NOT NULL,
  entity_id text NOT NULL,
  version bigint NOT NULL,
  PRIMARY KEY (entity_table, entity_id)
);
CREATE TABLE IF NOT EXISTS sync_change_log_state (
  id smallint PRIMARY KEY CHECK (id = 1),
  min_retained_commit_seq bigint NOT NULL
);
INSERT INTO sync_change_log_state (id, min_retained_commit_seq) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
"""

# commit直前に全体lockの下でcommit順の番号を振る。lockはcommitが可視化された後に解放されるため、
# commit_seq=N が見えるスナップショットでは N 未満も必ず見える。
SYNC_COMMIT_ORDER_SQL = """
CREATE OR REPLACE FUNCTION sync_assign_commit_seq() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  assigned bigint;
BEGIN
  IF coalesce(current_setting('aoitalk.sync_commit_seq', true), '') = '' THEN
    PERFORM pg_advisory_xact_lock(4163026101);
    assigned := nextval('sync_commit_seq');
    PERFORM set_config('aoitalk.sync_commit_seq', assigned::text, true);
  ELSE
    assigned := current_setting('aoitalk.sync_commit_seq')::bigint;
  END IF;
  WITH updated AS (
    UPDATE sync_change_log
       SET commit_seq = assigned, committed_at = clock_timestamp()
     WHERE commit_seq IS NULL AND txid = pg_current_xact_id()
     RETURNING entity_table, entity_id
  )
  INSERT INTO sync_entity_versions (entity_table, entity_id, version)
  SELECT DISTINCT entity_table, entity_id, assigned FROM updated
  ON CONFLICT (entity_table, entity_id) DO UPDATE SET version = EXCLUDED.version;
  RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS zz_sync_commit_order ON sync_change_log;
CREATE CONSTRAINT TRIGGER zz_sync_commit_order
  AFTER INSERT ON sync_change_log
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION sync_assign_commit_seq();
"""

# result には status / code / conflicts だけを保存する。entity と row_version は
# 応答時にcommit後の新しい読取で毎回作る（受付と効果は同一トランザクションでcommit）。
SYNC_OP_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS sync_op_receipts (
  user_id uuid NOT NULL,
  op_id uuid NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  result jsonb NOT NULL,
  PRIMARY KEY (user_id, op_id)
);
CREATE INDEX IF NOT EXISTS ix_sync_op_receipts_received_at ON sync_op_receipts (received_at);
"""

# 行本体の読込と必ず同じ REPEATABLE READ READ ONLY トランザクションで実行する。無ければ版0。
ENTITY_VERSIONS_SQL = (
    "SELECT entity_id, version FROM sync_entity_versions "
    "WHERE entity_table = :table AND entity_id = ANY(:ids)"
)
HEAD_SQL = (
    "SELECT greatest("
    "coalesce((SELECT max(commit_seq) FROM sync_change_log WHERE commit_seq IS NOT NULL), 0), "
    "(SELECT min_retained_commit_seq FROM sync_change_log_state WHERE id = 1))"
)
V3_TABLES_PRESENT_SQL = (
    "SELECT to_regclass('sync_change_log') IS NOT NULL "
    "AND to_regclass('sync_entity_versions') IS NOT NULL "
    "AND to_regclass('sync_op_receipts') IS NOT NULL"
)


@dataclass(frozen=True)
class ChangeRef:
    entity_table: str
    entity_id: str
    op: Literal["U", "D"]
    commit_seq: int
    seq: int


@dataclass(frozen=True)
class ScopeFilter:
    user_id: UUID
    project_ids: tuple[UUID, ...]
    docs_library_ids: tuple[UUID, ...]
    space_ids: tuple[UUID, ...]


class ChangeFeedReader(Protocol):
    async def read_head(self, session: AsyncSession) -> int: ...

    async def min_retained_commit_seq(self, session: AsyncSession) -> int: ...

    async def read_changes(
        self, session: AsyncSession, *, scope: ScopeFilter,
        after_commit_seq: int, after_seq: int, limit: int,
    ) -> list[ChangeRef]: ...


def encode_cursor(commit_seq: int, seq: int) -> str:
    return f"{commit_seq}:{seq}"


def decode_cursor(value: str) -> tuple[int, int]:
    commit_seq, seq = value.split(":", 1)
    parsed = (int(commit_seq), int(seq))
    if parsed[0] < 0 or parsed[1] < 0:
        raise ValueError("invalid cursor")
    return parsed


def head_cursor(head: int) -> str:
    return encode_cursor(head, MAX_SEQ)


def cursor_expired(cursor: tuple[int, int], min_retained_commit_seq: int) -> bool:
    return min_retained_commit_seq > 0 and cursor < (min_retained_commit_seq, MAX_SEQ)


def docs_scope_key(library_id: str, project_id: str | None) -> str:
    return f"{library_id}|project:{project_id or ''}"
