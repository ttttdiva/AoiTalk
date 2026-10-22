"""Index bookkeeping report, lifecycle schema preparation and pinned purge.

The report has no service/model initialization, canonical DB connection, vector
mutation or purge. It consumes an explicitly supplied metadata/count snapshot;
omission is unknown, never canonical absence or a zero vector count. Purge only
replays a fresh report's candidate rows after re-verifying them under locks.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import sqlite3
import time


# Side-table keys preserve the existing positional reconciliation INSERTs.
TABLES = {
    "embedding_sources": (("source_key",), "status", ("ready", "excluded", "failed")),
    "embedding_attempts": (("actor", "source_key", "identity"), "status", ("excluded", "failed")),
    "media_jobs": (("actor", "kind", "source_id"), "state", ("done", "failed")),
    "media_reconcile_sources": (("kind", "source_id"), "state", ("removed",)),
}
META = "media_index_lifecycle"


def _key(columns, prefix=""):
    return " || ':' || ".join(f"quote({prefix}{column})" for column in columns)


def prepare_lifecycle_schema(db: sqlite3.Connection, *, now: float | None = None):
    """Opt-in additive migration, to be called by each existing SQLite writer.

    Caller owns the transaction. Old rows start retention NOW regardless of
    updated_at/due/cursor timestamps. This function never verifies cleanup.
    """
    now = time.time() if now is None else now
    schema = {(row[0], row[1]) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger')")}
    already_tracked = ("table", META) in schema
    db.execute(f"""CREATE TABLE IF NOT EXISTS {META} (
        table_name TEXT NOT NULL, row_key TEXT NOT NULL,
        first_tracked_at REAL NOT NULL, changed_at REAL NOT NULL,
        terminal_at REAL, removed_at REAL, cleanup_verified_at REAL,
        PRIMARY KEY(table_name,row_key))""")
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, (columns, state, terminal) in TABLES.items():
        if table not in existing:
            continue
        # Constructors are also used by notifications. Reopening a migrated
        # queue must not backfill/scan its complete source inventory each time.
        if already_tracked and all(("trigger", f"maintenance_{table}_{action}") in schema
                                   for action in ("insert", "update", "delete")):
            continue
        states = ",".join(f"'{value}'" for value in terminal)
        key = _key(columns)
        db.execute(f"""INSERT OR IGNORE INTO {META}
            (table_name,row_key,first_tracked_at,changed_at,terminal_at,removed_at)
            SELECT ?,{key},?,?,CASE WHEN {state} IN ({states}) THEN ? END,
            CASE WHEN {state}='removed' THEN ? END FROM {table}""",
                   (table, now, now, now, now))
        for action in ("INSERT", "UPDATE"):
            # Any rewrite invalidates old cleanup evidence, including a new
            # job generation and a same-status source UPSERT.
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS maintenance_{table}_{action.lower()}
                AFTER {action} ON {table} BEGIN
                INSERT INTO {META}
                    (table_name,row_key,first_tracked_at,changed_at,terminal_at,removed_at)
                VALUES ('{table}',{_key(columns, 'NEW.')},unixepoch('now'),unixepoch('now'),
                    CASE WHEN NEW.{state} IN ({states}) THEN unixepoch('now') END,
                    CASE WHEN NEW.{state}='removed' THEN unixepoch('now') END)
                ON CONFLICT(table_name,row_key) DO UPDATE SET
                    changed_at=excluded.changed_at,terminal_at=excluded.terminal_at,
                    removed_at=excluded.removed_at,cleanup_verified_at=NULL;
                END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS maintenance_{table}_delete
            AFTER DELETE ON {table} BEGIN
            DELETE FROM {META} WHERE table_name='{table}' AND row_key={_key(columns, 'OLD.')};
            END""")


def record_cleanup_verified(db: sqlite3.Connection, source_key: str, identity: str, *,
                            remaining: int, now: float | None = None):
    """Writer hook AFTER excluded mark and an exact zero count under source lock.

    Intended for _withdraw_source integration, not unsupported-file exclusion.
    It records evidence only; it does not remove vectors or management records.
    """
    if type(remaining) is not int or remaining != 0:
        raise ValueError("cleanup must have an exact zero count")
    result = db.execute(f"""UPDATE {META} SET cleanup_verified_at=?
        WHERE table_name='embedding_sources' AND row_key=quote(?) AND EXISTS (
            SELECT 1 FROM embedding_sources WHERE source_key=? AND identity=? AND status='excluded')""",
                        (time.time() if now is None else now, source_key, source_key, identity))
    if result.rowcount != 1:
        raise ValueError("tracked excluded source with matching identity required")


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _read_database(path: Path):
    # mode=ro rejects missing paths; query_only also guards against accidental SQL
    # writes. Do not use immutable=1: it can hide committed WAL contents.
    path = path.resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        metadata = {(row["table_name"], row["row_key"]): dict(row)
                    for row in db.execute(f"SELECT * FROM {META}")} if META in tables else {}
        rows, counts = [], {}
        for table in (META, "media_reconcile_cursors"):
            if table in tables:
                counts[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table, (columns, state, _) in TABLES.items():
            if table not in tables:
                continue
            counts[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for row in db.execute(f"SELECT *,{_key(columns)} AS maintenance_key FROM {table}"):
                item = dict(row)
                item.update(table=table, lifecycle=metadata.get((table, item.pop("maintenance_key"))))
                if "source_key" not in item:
                    item["source_key"] = f"{item['kind']}:{item['source_id']}"
                item["record_state"] = item[state]
                rows.append(item)
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        disk = {"page_size": page_size, "page_count": db.execute("PRAGMA page_count").fetchone()[0],
                "freelist_count": db.execute("PRAGMA freelist_count").fetchone()[0]}
    disk["file_bytes"] = {suffix or "database": Path(str(path) + suffix).stat().st_size
                          if Path(str(path) + suffix).exists() else 0 for suffix in ("", "-wal", "-shm")}
    return rows, {"logical_rows": counts, "sqlite": disk, "timestamps_available": META in tables}


def maintenance_report(*, ledger: Path, queue: Path, evidence: dict | None = None,
                       retention_days=30, max_evidence_age_seconds=3600, now=None):
    """Classify each management record; never authorizes or performs a purge.

    Both DBs are required so an unseen pending/running job cannot be overlooked.
    Snapshot schema is documented in docs/media_index_maintenance.md.
    """
    if not _timestamp(retention_days) or not _timestamp(max_evidence_age_seconds):
        raise ValueError("positive retention and evidence age required")
    now = time.time() if now is None else now
    evidence = {} if evidence is None else evidence
    if not isinstance(evidence, dict) or any(not isinstance(evidence.get(name, {}), dict)
                                            for name in ("canonical", "vectors")):
        raise ValueError("evidence must contain canonical/vectors mappings")
    if any(not isinstance(value, dict) for name in ("canonical", "vectors")
           for value in evidence.get(name, {}).values()):
        raise ValueError("evidence entries must be objects")
    rows, databases = [], {}
    for name, path in (("ledger", Path(ledger)), ("queue", Path(queue))):
        records, databases[name] = _read_database(path)
        rows.extend(records)
    required = {"ledger": {"embedding_sources", "embedding_attempts"},
                "queue": {"media_jobs", "media_reconcile_sources"}}
    schema_complete = all(required[name] <= database["logical_rows"].keys()
                          for name, database in databases.items())
    snapshot_at = evidence.get("observed_at")
    fresh = _timestamp(snapshot_at) and 0 <= now - snapshot_at <= max_evidence_age_seconds
    groups = {}
    for row in rows:
        groups.setdefault(row["source_key"], []).append(row)
    results = []
    for key, group in sorted(groups.items()):
        canonical = evidence.get("canonical", {}).get(key, {})
        vectors = evidence.get("vectors", {}).get(key, {})
        reasons = set()
        if not schema_complete:
            reasons.add("inventory_schema_incomplete")
        if not key.startswith("workspace_file:"):
            reasons.add("outside_workspace_file_scope")
        if not fresh:
            reasons.add("evidence_missing_or_stale")
        if canonical.get("state") not in {"deleted", "missing"}:
            reasons.add("canonical_live_or_unknown")
        if not _timestamp(canonical.get("changed_at")):
            reasons.add("canonical_timestamp_unknown")
        zero = (type(vectors.get("remaining")) is int and vectors["remaining"] == 0
                and vectors.get("all_collections") is True and _timestamp(vectors.get("verified_at")))
        if not zero:
            reasons.add("vectors_remaining_or_unknown")
        elif not 0 <= now - vectors["verified_at"] <= max_evidence_age_seconds:
            reasons.add("vector_evidence_missing_or_stale")
        sources = [row for row in group if row["table"] == "embedding_sources"]
        identities = vectors.get("identities")
        if not isinstance(identities, list) or not all(row["identity"] in identities for row in sources):
            reasons.add("vector_identity_unverified")
        if not sources or not all(row["lifecycle"] and
                                 _timestamp(row["lifecycle"].get("cleanup_verified_at")) for row in sources):
            reasons.add("cleanup_unverified")
        dates = [canonical.get("changed_at", 0)]
        for row in group:
            state = row["record_state"]
            if state in {"pending", "running", "failed"}:
                reasons.add(f"protected_{state}")
            allowed = {"embedding_sources": {"excluded"}, "embedding_attempts": {"excluded"},
                       "media_jobs": {"done"}, "media_reconcile_sources": {"removed"}}[row["table"]]
            if state not in allowed:
                reasons.add("active_or_unresolved_record")
            lifecycle = row["lifecycle"]
            timestamp = "removed_at" if row["table"] == "media_reconcile_sources" else "terminal_at"
            if lifecycle is None or not all(_timestamp(lifecycle.get(field))
                                             for field in ("first_tracked_at", "changed_at", timestamp)):
                reasons.add("lifecycle_migration_or_timestamp_required")
            else:
                dates.extend(lifecycle[field] for field in ("first_tracked_at", "changed_at", timestamp))
                if lifecycle.get("cleanup_verified_at"):
                    dates.append(lifecycle["cleanup_verified_at"])
        valid_dates = [date for date in dates if _timestamp(date)]
        latest = max(valid_dates, default=now)
        # A new exact-count snapshot must cover every observed generation and
        # canonical deletion. Independent DBs do not provide an atomic purge view.
        if zero and (not _timestamp(snapshot_at) or vectors["verified_at"] < latest
                     or vectors["verified_at"] > snapshot_at):
            reasons.add("vector_evidence_precedes_change")
        if now - latest < retention_days * 86400:
            reasons.add("retention_not_elapsed")
        results.append({"source_key": key, "classification": "protected" if reasons else "candidate",
                        "reasons": sorted(reasons), "canonical": canonical.get("state", "unknown"),
                        "canonical_changed_at": canonical.get("changed_at"),
                        "vectors_remaining": vectors.get("remaining"), "retention_started_at": latest,
                        "records": [{"table": row["table"], "state": row["record_state"],
                                     "identity": row.get("identity"), "actor": row.get("actor"),
                                     "lifecycle": row["lifecycle"]} for row in group]})
    return {"read_only": True, "purge_supported": False, "observed_at": now,
            "retention_days": retention_days, "databases": databases,
            "shared_control_records": {"media_reconcile_cursors": "protected: shared scan checkpoints",
                                       META: "lifecycle side rows belong to their source record"},
            "summary": dict(Counter(row["classification"] for row in results)), "sources": results,
            "disk_note": "Logical row eligibility is not reclaimable disk bytes; no DELETE, VACUUM or checkpoint is run."}


# Bounded like reconciliation pages. A plan pins each record's lifecycle
# changed_at, which every writer UPSERT/new job generation bumps via trigger.
MAX_PURGE_SOURCES = 100
DATABASE_TABLES = {"ledger": ("embedding_sources", "embedding_attempts"),
                   "queue": ("media_jobs", "media_reconcile_sources")}


def source_file_lock_path(ledger_path, source_key: str) -> Path:
    """Cross-process per-source lock shared with MediaIndexService.sync_source."""
    ledger_path = Path(ledger_path)
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]
    return ledger_path.with_name(f"{ledger_path.name}.{digest}.lock")


def purge_plan(report: dict, *, max_sources=MAX_PURGE_SOURCES):
    """Read-only: copy at most 100 candidates and the exact rows the report saw."""
    if not 1 <= max_sources <= MAX_PURGE_SOURCES:
        raise ValueError("Purge plan must be bounded to 100 sources")
    sources = [{
        "source_key": source["source_key"], "canonical": source["canonical"],
        "canonical_changed_at": source["canonical_changed_at"],
        "records": [{"table": record["table"], "row_key": record["lifecycle"]["row_key"],
                     "state": record["state"], "changed_at": record["lifecycle"]["changed_at"]}
                    for record in source["records"]],
    } for source in report["sources"] if source["classification"] == "candidate" and not source["reasons"]]
    return {"version": 1, "observed_at": report["observed_at"], "retention_days": report["retention_days"],
            "sources": sources[:max_sources], "remaining_candidates": max(0, len(sources) - max_sources)}


def _source_where(table, source_key):
    if table in DATABASE_TABLES["ledger"]:
        return "t.source_key=?", (source_key,)
    kind, _, source_id = source_key.partition(":")
    return "t.kind=? AND t.source_id=?", (kind, source_id)


def _verify_unchanged(db, name, source):
    """Exactly the planned rows with the same state/generation; any drift skips."""
    if META not in {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return "lifecycle_migration_required"
    for table in DATABASE_TABLES[name]:
        columns, state, _ = TABLES[table]
        planned = {record["row_key"]: record for record in source["records"] if record["table"] == table}
        where, args = _source_where(table, source["source_key"])
        rows = db.execute(f"""SELECT {_key(columns, 't.')},t.{state},m.changed_at,m.cleanup_verified_at
            FROM {table} t LEFT JOIN {META} m ON m.table_name=? AND m.row_key={_key(columns, 't.')}
            WHERE {where}""", (table, *args)).fetchall()
        if {row[0] for row in rows} != set(planned):
            return "records_changed"
        for row_key, state, changed_at, verified_at in rows:
            if state != planned[row_key]["state"] or changed_at != planned[row_key]["changed_at"]:
                return "generation_changed"
            if table == "embedding_sources" and not _timestamp(verified_at):
                return "cleanup_unverified"
    return None


def _delete_planned(db, name, source):
    for record in source["records"]:
        if record["table"] not in DATABASE_TABLES[name]:
            continue
        columns = TABLES[record["table"]][0]
        where, args = _source_where(record["table"], source["source_key"])
        # Lifecycle side rows go with their source row via the delete trigger.
        db.execute(f"DELETE FROM {record['table']} AS t WHERE {where} AND {_key(columns, 't.')}=?",
                   (*args, record["row_key"]))


async def _delete_canonical_tombstone(session, source):
    """Only the deleted Files tombstone the plan saw; restore/re-delete skips."""
    from sqlalchemy import delete
    import uuid
    from ..memory.models import WorkspaceFileEntry as E

    # deleted_at is naive UTC (scanner clock=datetime.utcnow).
    planned = datetime.fromtimestamp(source["canonical_changed_at"], timezone.utc).replace(tzinfo=None)
    result = await session.execute(delete(E).where(
        E.id == uuid.UUID(source["source_key"].partition(":")[2]), E.status == "deleted",
        E.deleted_at.is_not(None), E.deleted_at <= planned + timedelta(seconds=1)))
    return result.rowcount == 1


async def apply_purge_plan(plan: dict, *, ledger, queue, canonical_session_factory=None,
                           execute=False, max_plan_age_seconds=3600, lock_timeout=30, now=None):
    """Re-verify each planned source under locks; delete only when execute=True.

    Per source: sync_source FileLock -> BEGIN IMMEDIATE on both SQLite DBs ->
    verify pinned rows -> canonical tombstone conditional delete+commit ->
    SQLite delete+commit. A crash between the commits leaves rows whose
    canonical is now missing; a later report/plan collects them.
    """
    from filelock import FileLock

    now = time.time() if now is None else now
    if plan.get("version") != 1 or not _timestamp(plan.get("observed_at")):
        raise ValueError("unsupported purge plan")
    if not 0 <= now - plan["observed_at"] <= max_plan_age_seconds:
        raise ValueError("purge plan is stale; regenerate evidence and report")
    if len(plan["sources"]) > MAX_PURGE_SOURCES:
        raise ValueError("Purge plan must be bounded to 100 sources")
    paths = {"ledger": Path(ledger), "queue": Path(queue)}
    result = {"execute": bool(execute), "purged": [], "verified": [], "skipped": {}}
    for source in plan["sources"]:
        key = source["source_key"]
        if (not key.startswith("workspace_file:") or source["canonical"] not in {"deleted", "missing"}
                or (source["canonical"] == "deleted" and not _timestamp(source.get("canonical_changed_at")))):
            result["skipped"][key] = "not_a_purge_candidate"
            continue
        if source["canonical"] == "deleted" and execute and canonical_session_factory is None:
            result["skipped"][key] = "canonical_session_required"
            continue
        lock = FileLock(str(source_file_lock_path(paths["ledger"], key)), thread_local=False)
        await asyncio.to_thread(lock.acquire, timeout=lock_timeout)
        try:
            with closing(sqlite3.connect(paths["ledger"], timeout=30, isolation_level=None)) as ledger_db, \
                    closing(sqlite3.connect(paths["queue"], timeout=30, isolation_level=None)) as queue_db:
                dbs = {"ledger": ledger_db, "queue": queue_db}
                try:
                    for db in dbs.values():
                        db.execute("BEGIN IMMEDIATE")
                    reason = next((found for name, db in dbs.items()
                                   if (found := _verify_unchanged(db, name, source))), None)
                    if reason is None and execute and source["canonical"] == "deleted":
                        session = await canonical_session_factory()
                        try:
                            if await _delete_canonical_tombstone(session, source):
                                await session.commit()
                            else:
                                await session.rollback()
                                reason = "canonical_tombstone_changed"
                        finally:
                            await session.close()
                    if reason is not None:
                        result["skipped"][key] = reason
                    elif not execute:
                        result["verified"].append(key)
                    else:
                        for name, db in dbs.items():
                            _delete_planned(db, name, source)
                        queue_db.execute("COMMIT")
                        ledger_db.execute("COMMIT")
                        result["purged"].append(key)
                finally:
                    for db in dbs.values():
                        if db.in_transaction:
                            db.execute("ROLLBACK")
        finally:
            await asyncio.to_thread(lock.release)
    return result
