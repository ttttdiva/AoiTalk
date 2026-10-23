"""Sync v3 の schema / key / flag readiness 判定。

このモジュールは schema を変更しない。運用入口（``scripts/sync_v3_schema.py``）と
runtime の v3 gate が同じ catalog inventory を使うための共有実装である。
秘密値、receipt 本文、暗号文は読み出さず、判定結果にはオブジェクト名だけを返す。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from ..security.field_crypto import FieldCryptoError, probe_data_key_readonly
from . import contract, receipts, schema_sql


# The optional schema has its own Alembic tree and version table.  Keeping these
# names here makes the CLI, migration, and runtime readiness check one contract.
SYNC_V3_MIGRATION_REVISION = "20261023_0002"  # frozen identity imported by revision 0002
SYNC_V3_LATEST_REVISION = "20261023_0003"
SYNC_V3_VERSION_TABLE = "sync_v3_alembic_version"

_FALSE_FLAGS = {"", "0", "false", "no", "off"}

# ``format_type`` output from PostgreSQL is stable for these contract types.  The
# expected nullability is part of the readiness contract; nullable receipt identity
# fields are never inferred or repaired from existing rows.
_TABLE_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "sync_change_log": {
        "seq": ("bigint", True),
        "txid": ("xid8", True),
        "commit_seq": ("bigint", False),
        "committed_at": ("timestamp with time zone", False),
        "entity_table": ("text", True),
        "entity_id": ("text", True),
        "op": ("character(1)", True),
        "owner_user_id": ("uuid", False),
        "project_id": ("uuid", False),
        "docs_library_id": ("uuid", False),
        "space_id": ("uuid", False),
        "changed_at": ("timestamp with time zone", True),
    },
    "sync_entity_versions": {
        "entity_table": ("text", True),
        "entity_id": ("text", True),
        "version": ("bigint", True),
    },
    "sync_change_log_state": {
        "id": ("smallint", True),
        "min_retained_commit_seq": ("bigint", True),
    },
    "sync_op_receipts": {
        "user_id": ("uuid", True),
        "op_id": ("uuid", True),
        "received_at": ("timestamp with time zone", True),
        "result": ("jsonb", True),
        "device_id": ("uuid", True),
        "op_seq": ("bigint", True),
    },
    "sync_device_floors": {
        "user_id": ("uuid", True),
        "device_id": ("uuid", True),
        "floor": ("bigint", True),
        "updated_at": ("timestamp with time zone", True),
    },
}

_TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "sync_change_log": ("seq",),
    "sync_entity_versions": ("entity_table", "entity_id"),
    "sync_change_log_state": ("id",),
    "sync_op_receipts": ("user_id", "op_id"),
    "sync_device_floors": ("user_id", "device_id"),
}

_INDEX_EXPECTATIONS: dict[str, tuple[str, str, str]] = {
    "ix_sync_change_log_pos": ("sync_change_log", "(commit_seq, seq)", ""),
    "ix_sync_change_log_uncommitted": ("sync_change_log", "(txid)", "commit_seq IS NULL"),
    "ix_sync_change_log_owner": ("sync_change_log", "(owner_user_id, commit_seq)", ""),
    "ix_sync_change_log_project": ("sync_change_log", "(project_id, commit_seq)", ""),
    "ix_sync_change_log_library": ("sync_change_log", "(docs_library_id, commit_seq)", ""),
    "ix_sync_change_log_space": ("sync_change_log", "(space_id, commit_seq)", ""),
    "ix_sync_change_log_committed_at": ("sync_change_log", "(committed_at)", ""),
    "ix_sync_change_log_entity": ("sync_change_log", "(entity_table, entity_id, seq desc)", ""),
    "ix_sync_op_receipts_received_at": ("sync_op_receipts", "(received_at)", ""),
    "ix_sync_op_receipts_device_seq": (
        "sync_op_receipts", "(user_id, device_id, op_seq)", ""
    ),
}

_REQUIRED_SEQUENCES = frozenset({"sync_commit_seq", "sync_change_log_seq_seq"})


def _normalize_function_signature(value: str) -> str:
    name, raw_args = value.split("(", 1)
    raw_args = raw_args.rsplit(")", 1)[0].strip()
    if not raw_args:
        return f"{name}()"
    args: list[str] = []
    for raw_arg in raw_args.split(","):
        pieces = raw_arg.strip().split()
        # pg_get_function_identity_arguments includes declared argument names;
        # the contract signatures intentionally carry types only.
        if len(pieces) > 1 and pieces[0].startswith("p_"):
            pieces = pieces[1:]
        args.append(" ".join(pieces).replace("character", "char"))
    return f"{name}({','.join(args)})"


def _function_signatures() -> frozenset[str]:
    names = {
        _normalize_function_signature(f"{name}()")
        for name in schema_sql._function_names()
    }
    names.add(_normalize_function_signature("sync_assign_commit_seq()"))
    names.update(_normalize_function_signature(item) for item in schema_sql._HELPER_SIGNATURES)
    return frozenset(names)


def _trigger_targets() -> frozenset[tuple[str, str]]:
    # Trigger names are table-local (task child triggers deliberately share names).
    return frozenset((*schema_sql._trigger_pairs(), ("sync_change_log", "zz_sync_commit_order")))


def _trigger_functions() -> dict[tuple[str, str], str]:
    functions: dict[tuple[str, str], str] = {
        (table, f"sync_v3_log_{table}_{suffix}"): f"sync_v3_log_{table}"
        for table in schema_sql._ROW_TRIGGER_TABLES
        for suffix in ("ins", "upd", "del")
    }
    suffixes = {"INSERT": "ins", "UPDATE": "upd", "DELETE": "del"}
    for table, name, function, events in schema_sql._DEPENDENCY_TRIGGERS:
        for event in events:
            functions[(table, f"{name}_{suffixes[event]}")] = function
    functions[("sync_change_log", "zz_sync_commit_order")] = "sync_assign_commit_seq"
    return functions


_REQUIRED_FUNCTIONS = _function_signatures()
_REQUIRED_TRIGGERS = _trigger_targets()
_REQUIRED_TRIGGER_FUNCTIONS = _trigger_functions()
_SOURCE_TABLES = frozenset(
    tuple(schema_sql._ROW_TRIGGER_TABLES)
    + tuple(item[0] for item in schema_sql._DEPENDENCY_TRIGGERS)
)


@dataclass(frozen=True)
class _TriggerRow:
    table: str
    function: str
    function_in_schema: bool
    definition: str
    enabled: str
    timing: str
    events: frozenset[str]
    row_level: bool
    is_constraint: bool
    deferrable: bool
    initially_deferred: bool


# PostgreSQL's pg_trigger.tgtype bit layout is stable and lets readiness check
# timing/event/row shape without relying on display wording in pg_get_triggerdef.
_TRIGGER_ROW = 1
_TRIGGER_BEFORE = 2
_TRIGGER_INSERT = 4
_TRIGGER_DELETE = 8
_TRIGGER_UPDATE = 16
_TRIGGER_TRUNCATE = 32
_TRIGGER_INSTEAD = 64


def _trigger_timing(tgtype: int) -> str:
    if tgtype & _TRIGGER_INSTEAD:
        return "INSTEAD"
    return "BEFORE" if tgtype & _TRIGGER_BEFORE else "AFTER"


def _trigger_events(tgtype: int) -> frozenset[str]:
    events = {
        event
        for bit, event in (
            (_TRIGGER_INSERT, "INSERT"),
            (_TRIGGER_UPDATE, "UPDATE"),
            (_TRIGGER_DELETE, "DELETE"),
            (_TRIGGER_TRUNCATE, "TRUNCATE"),
        )
        if tgtype & bit
    }
    return frozenset(events)


def _expected_trigger_shape(
    name: str,
) -> tuple[str, frozenset[str], bool, bool, bool, bool]:
    """Return timing/events/row/constraint requirements for a contract trigger."""

    if name == "zz_sync_commit_order":
        return "AFTER", frozenset({"INSERT"}), True, True, True, True
    suffix = name.rsplit("_", 1)[-1]
    event_by_suffix = {"ins": "INSERT", "upd": "UPDATE", "del": "DELETE"}
    event = event_by_suffix.get(suffix)
    if event is None:  # pragma: no cover - required trigger names are generated
        return "", frozenset(), False, False, False, False
    timing = "BEFORE" if event == "DELETE" else "AFTER"
    return timing, frozenset({event}), True, False, False, False


@dataclass(frozen=True)
class SchemaReadiness:
    """Secret-free readiness snapshot used by both CLI and runtime."""

    ready: bool
    flag_enabled: bool
    key_ready: bool
    missing: tuple[str, ...] = ()
    wrong: tuple[str, ...] = ()
    schema_revision: str | None = None
    revision_ok: bool = True
    error: str | None = None

    @property
    def enabled(self) -> bool:
        """Effective gate value after schema/key readiness and the explicit flag."""

        return self.ready and self.flag_enabled

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "enabled": self.enabled,
            "flag_enabled": self.flag_enabled,
            "key_ready": self.key_ready,
            "schema_revision": self.schema_revision,
            "revision_ok": self.revision_ok,
            "missing": list(self.missing),
            "wrong": list(self.wrong),
            **({"error": self.error} if self.error else {}),
        }


def _normalize_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_FLAGS
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return bool(value)


def _sync_v3_flag_leaf(connection: Connection) -> tuple[Any, bool]:
    """Return the leaf and whether it is persisted; never mutate config."""

    present = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = 'app_config_settings' "
            "AND c.relkind IN ('r', 'p'))"
        )
    ).scalar_one()
    if not present:
        return None, False
    value = connection.execute(
        text(
            "SELECT value -> 'sync_v3_enabled' "
            "FROM app_config_settings WHERE key = 'global'"
        )
    ).scalar_one_or_none()
    if value is None:
        return None, False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            # JSON string leaves are returned without quotes by some drivers.
            pass
    return value, True


def read_sync_v3_flag(connection: Connection) -> bool:
    """Read only the global ``sync_v3_enabled`` leaf; absent means disabled."""

    value, _persisted = _sync_v3_flag_leaf(connection)
    return _normalize_flag(value)


def read_sync_v3_flag_explicit_false(connection: Connection) -> bool:
    """Return true only when the global leaf is persisted as JSON/boolean false."""

    value, persisted = _sync_v3_flag_leaf(connection)
    # An absent table/row/leaf remains an effective runtime false, but it is not
    # authorization for the destructive optional schema apply.
    return persisted and value is False


def _relation(connection: Connection, name: str) -> str | None:
    return connection.execute(
        text(
            "SELECT c.relkind::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = :name"
        ),
        {"name": name},
    ).scalar_one_or_none()


def _columns(connection: Connection, table: str) -> dict[str, tuple[str, bool]]:
    rows = connection.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = :table "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": table},
    ).all()
    return {str(row[0]): (str(row[1]), bool(row[2])) for row in rows}


def _catalog_snapshot(
    connection: Connection,
) -> tuple[dict[str, str], dict[str, dict[str, tuple[str, bool]]]]:
    """Read required relations and all their columns in one catalog query."""

    names = tuple(
        {
            *(_TABLE_COLUMNS.keys()),
            *_REQUIRED_SEQUENCES,
            *(_INDEX_EXPECTATIONS.keys()),
            SYNC_V3_VERSION_TABLE,
        }
    )
    literals = ", ".join("'" + name.replace("'", "''") + "'" for name in names)
    rows = connection.execute(
        text(
            "SELECT c.relname, c.relkind::text, a.attname, "
            "format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attribute a ON a.attrelid = c.oid "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            f"WHERE n.nspname = current_schema() AND c.relname IN ({literals})"
        )
    ).all()
    relations: dict[str, str] = {}
    columns: dict[str, dict[str, tuple[str, bool]]] = {}
    for row in rows:
        relation = str(row[0])
        relations[relation] = str(row[1])
        if row[2] is not None:
            columns.setdefault(relation, {})[str(row[2])] = (str(row[3]), bool(row[4]))
    return relations, columns


def _primary_key(connection: Connection, table: str) -> tuple[str, ...] | None:
    row = connection.execute(
        text(
            "SELECT pg_get_constraintdef(con.oid) "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = :table "
            "AND con.contype = 'p'"
        ),
        {"table": table},
    ).scalar_one_or_none()
    if not row:
        return None
    match = re.search(r"\(([^)]*)\)", str(row))
    if not match:
        return ()
    return tuple(item.strip().strip('"') for item in match.group(1).split(","))


def _primary_keys(connection: Connection) -> dict[str, tuple[str, ...]]:
    names = ", ".join("'" + table.replace("'", "''") + "'" for table in _TABLE_COLUMNS)
    rows = connection.execute(
        text(
            "SELECT c.relname, array_agg(a.attname ORDER BY keys.ordinality) "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY keys(attnum, ordinality) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = keys.attnum "
            f"WHERE n.nspname = current_schema() AND con.contype = 'p' AND c.relname IN ({names}) "
            "GROUP BY c.relname, con.oid"
        )
    ).all()
    return {str(row[0]): tuple(str(item) for item in row[1]) for row in rows}


def _index_rows(connection: Connection) -> dict[str, tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT c.relname, t.relname, pg_get_indexdef(c.oid) "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE n.nspname = current_schema()"
        )
    ).all()
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _function_rows(connection: Connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname LIKE 'sync_%'"
        )
    ).scalars()
    result: set[str] = set()
    for value in rows:
        # pg_get_function_identity_arguments spells ``character`` as ``character``;
        # the contract uses SQL's short ``char`` spelling.
        result.add(_normalize_function_signature(str(value)))
    return result


def _trigger_rows(connection: Connection) -> dict[tuple[str, str], _TriggerRow]:
    rows = connection.execute(
        text(
            "SELECT t.tgname, c.relname, t.tgenabled::text, t.tgtype, "
            "(t.tgconstraint <> 0), COALESCE(con.condeferrable, false), "
            "COALESCE(con.condeferred, false), pg_get_triggerdef(t.oid), "
            "p.proname, (pn.nspname = current_schema()) "
            "FROM pg_trigger t "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace pn ON pn.oid = p.pronamespace "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_constraint con ON con.oid = t.tgconstraint "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal"
        )
    ).all()
    result: dict[tuple[str, str], _TriggerRow] = {}
    for row in rows:
        tgtype = int(row[3])
        result[(str(row[1]), str(row[0]))] = _TriggerRow(
            table=str(row[1]),
            function=str(row[8]),
            function_in_schema=bool(row[9]),
            definition=str(row[7]),
            enabled=str(row[2]),
            timing=_trigger_timing(tgtype),
            events=_trigger_events(tgtype),
            row_level=bool(tgtype & _TRIGGER_ROW),
            is_constraint=bool(row[4]),
            deferrable=bool(row[5]),
            initially_deferred=bool(row[6]),
        )
    return result


def _revision(connection: Connection) -> str | None:
    if _relation(connection, SYNC_V3_VERSION_TABLE) not in {"r", "p"}:
        return None
    rows = connection.execute(
        text(f"SELECT version_num FROM {SYNC_V3_VERSION_TABLE}")
    ).scalars().all()
    if len(rows) != 1:
        return ",".join(sorted(str(item) for item in rows)) if rows else None
    return str(rows[0])


def inspect_connection(
    connection: Connection,
    *,
    check_key: bool = False,
    require_revision: bool = False,
    include_docs_visibility: bool = False,
) -> SchemaReadiness:
    """Inspect without writes. Revision 0002 uses the frozen base inventory;
    runtime and the CLI additionally require the revision 0003 inventory.
    """

    missing: set[str] = set()
    wrong: set[str] = set()
    relations, columns = _catalog_snapshot(connection)
    primary_keys = _primary_keys(connection)

    for table, expected in _TABLE_COLUMNS.items():
        relation = relations.get(table)
        if relation not in {"r", "p"}:
            missing.add(f"table:{table}")
            continue
        actual = columns.get(table, {})
        for name, (expected_type, expected_not_null) in expected.items():
            if name not in actual:
                missing.add(f"column:{table}.{name}")
                continue
            actual_type, actual_not_null = actual[name]
            if actual_type != expected_type or actual_not_null != expected_not_null:
                wrong.add(f"column:{table}.{name}")
        primary_key = primary_keys.get(table)
        if primary_key != _TABLE_PRIMARY_KEYS[table]:
            wrong.add(f"primary_key:{table}")

    if relations.get("sync_change_log_state") in {"r", "p"}:
        state_row = connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM sync_change_log_state WHERE id = 1)")
        ).scalar_one()
        if not state_row:
            missing.add("row:sync_change_log_state(id=1)")

    indexes = _index_rows(connection)
    for name, (table, columns, predicate) in _INDEX_EXPECTATIONS.items():
        row = indexes.get(name)
        if row is None:
            missing.add(f"index:{name}")
            continue
        actual_table, definition = row
        normalized = re.sub(r"\s+", " ", definition.lower()).replace('"', "")
        expected_columns = re.sub(r"\s+", " ", columns.lower())
        if actual_table != table or expected_columns not in normalized:
            wrong.add(f"index:{name}")
            continue
        if predicate and predicate.lower() not in normalized:
            wrong.add(f"index:{name}")

    for sequence in _REQUIRED_SEQUENCES:
        if relations.get(sequence) != "S":
            missing.add(f"sequence:{sequence}")

    functions = _function_rows(connection)
    for signature in _REQUIRED_FUNCTIONS:
        if signature not in functions:
            missing.add(f"function:{signature}")

    trigger_functions = dict(_REQUIRED_TRIGGER_FUNCTIONS)
    if include_docs_visibility:
        from .docs_visibility_sql import DOCS_VISIBILITY_TRIGGERS
        for signature in ("sync_v3_emit_docs_visibility(uuid)", "sync_v3_dep_docs_visibility()", "sync_v3_dep_docs_share()"):
            if signature not in functions:
                missing.add(f"function:{signature}")
        suffixes = {"INSERT": "ins", "UPDATE": "upd", "DELETE": "del"}
        for table, name, events in DOCS_VISIBILITY_TRIGGERS:
            for event in events:
                trigger_functions[(table, f"{name}_{suffixes[event]}")] = name
    triggers = _trigger_rows(connection)
    for (table, name), expected_function in trigger_functions.items():
        row = triggers.get((table, name))
        if row is None:
            missing.add(f"trigger:{table}.{name}")
            continue
        (
            expected_timing,
            expected_events,
            expected_row,
            expected_constraint,
            expected_deferrable,
            expected_deferred,
        ) = _expected_trigger_shape(name)
        if (
            row.table != table
            or row.enabled != "O"
            or row.timing != expected_timing
            or row.events != expected_events
            or row.row_level != expected_row
            or row.is_constraint != expected_constraint
            or row.deferrable != expected_deferrable
            or row.initially_deferred != expected_deferred
            or row.function != expected_function
            or not row.function_in_schema
        ):
            wrong.add(f"trigger:{table}.{name}")

    schema_revision = _revision(connection)
    # Object inventory is the forward-compatible runtime proof.  The CLI adds
    # Alembic ancestry validation, while a later child revision must not disable
    # a complete v3 schema.
    revision_ok = True
    if require_revision and schema_revision is None:
        missing.add(f"{SYNC_V3_VERSION_TABLE}:{SYNC_V3_MIGRATION_REVISION}")
        revision_ok = False

    flag_enabled = read_sync_v3_flag(connection)
    key_ready = False
    error: str | None = None
    if check_key:
        try:
            key_ready = bool(probe_data_key_readonly())
        except (FieldCryptoError, Exception):
            # ``probe_data_key_readonly`` already sanitizes its exception.  Do not
            # leak provider errors, paths, key IDs or key material into a report.
            key_ready = False

    ready = not missing and not wrong and (key_ready if check_key else True)
    ready = ready and revision_ok
    return SchemaReadiness(
        ready=ready,
        flag_enabled=flag_enabled,
        key_ready=key_ready,
        missing=tuple(sorted(missing)),
        wrong=tuple(sorted(wrong)),
        schema_revision=schema_revision,
        revision_ok=revision_ok,
        error=error,
    )


async def inspect_readiness(
    session: AsyncSession,
    *,
    check_key: bool = True,
    require_revision: bool = False,
    short_circuit_disabled: bool = False,
) -> SchemaReadiness:
    """Async wrapper used by request gates; all DB work remains read-only."""

    if short_circuit_disabled:
        flag_enabled = await session.run_sync(read_sync_v3_flag)
        if not flag_enabled:
            return SchemaReadiness(
                ready=False,
                flag_enabled=False,
                key_ready=False,
                revision_ok=True,
            )
    return await session.run_sync(
        lambda connection: inspect_connection(
            connection, check_key=False, require_revision=require_revision, include_docs_visibility=True
        )
    ) if not check_key else await _inspect_with_key(session, require_revision=require_revision)


async def _inspect_with_key(session: AsyncSession, *, require_revision: bool) -> SchemaReadiness:
    report = await session.run_sync(
        lambda connection: inspect_connection(
            connection, check_key=False, require_revision=require_revision, include_docs_visibility=True
        )
    )
    try:
        report_key_ready = bool(await asyncio.to_thread(probe_data_key_readonly))
    except Exception:
        report_key_ready = False
    return SchemaReadiness(
        ready=report.ready and report_key_ready,
        flag_enabled=report.flag_enabled,
        key_ready=report_key_ready,
        missing=report.missing,
        wrong=report.wrong,
        schema_revision=report.schema_revision,
        revision_ok=report.revision_ok,
        error=report.error,
    )


def receipt_upgrade_issues(connection: Connection) -> tuple[str, ...]:
    """Return safe, secret-free blockers before receipt retirement DDL."""

    relation = _relation(connection, "sync_op_receipts")
    if relation not in {"r", "p"}:
        return ()
    columns = _columns(connection, "sync_op_receipts")
    row_count = int(connection.execute(text("SELECT count(*) FROM sync_op_receipts")).scalar_one())
    issues: list[str] = []
    for name, expected_type in (("device_id", "uuid"), ("op_seq", "bigint")):
        actual = columns.get(name)
        if actual is None:
            if row_count:
                issues.append(f"legacy_receipts_missing:{name}")
            continue
        if actual[0] != expected_type:
            issues.append(f"wrong_type:sync_op_receipts.{name}")
            continue
        if row_count:
            null_count = int(
                connection.execute(
                    text(f"SELECT count(*) FROM sync_op_receipts WHERE {name} IS NULL")
                ).scalar_one()
            )
            if null_count:
                issues.append(f"legacy_receipts_null:{name}")
    return tuple(sorted(issues))


def migration_preflight(connection: Connection) -> None:
    """Reject unsafe apply before any migration statement mutates the database."""

    if read_sync_v3_flag(connection):
        raise RuntimeError("sync_v3_enabled must be false before schema apply")
    if not read_sync_v3_flag_explicit_false(connection):
        raise RuntimeError(
            "sync_v3_enabled=false must be persisted in the global app config before schema apply"
        )

    receipt_issues = receipt_upgrade_issues(connection)
    if receipt_issues:
        raise RuntimeError(
            "sync_op_receipts contains legacy rows without explicit device_id/op_seq; "
            "repair policy is required before migration (" + ", ".join(receipt_issues) + ")"
        )

    missing_sources = sorted(
        table for table in _SOURCE_TABLES if _relation(connection, table) not in {"r", "p"}
    )
    if missing_sources:
        raise RuntimeError("sync-v3 source tables are missing: " + ", ".join(missing_sources))

    current = inspect_connection(connection, check_key=False, require_revision=False)
    # Missing objects are the migration's work.  An object with an incompatible
    # shape cannot be repaired by this migration without risking business data.
    if current.wrong:
        raise RuntimeError(
            "sync-v3 existing objects have incompatible definitions: "
            + ", ".join(current.wrong)
        )


def migration_receipt_hardening_sql() -> str:
    """Make pre-existing empty nullable columns satisfy the retirement contract."""

    return (
        "ALTER TABLE sync_op_receipts ALTER COLUMN device_id SET NOT NULL;\n"
        "ALTER TABLE sync_op_receipts ALTER COLUMN op_seq SET NOT NULL;\n"
    )


def source_tables() -> tuple[str, ...]:
    return tuple(sorted(_SOURCE_TABLES))

