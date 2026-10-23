"""Plan, verify, or explicitly apply the Sync v3 schema migration.

The default operation is read-only ``plan``.  ``apply`` targets only the
versioned Sync v3 revision and never changes the feature flag.  Output is JSON
with a password-free target identifier and object names only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402

from src.memory.config import validate_postgres_schema  # noqa: E402
from src.sync_v3.schema_readiness import (  # noqa: E402
    SYNC_V3_LATEST_REVISION,
    SYNC_V3_VERSION_TABLE,
    inspect_connection,
    migration_preflight,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("plan", "verify", "apply"),
        nargs="?",
        default="plan",
        help="plan/verify are read-only; apply is an explicit migration operation",
    )
    parser.add_argument(
        "--url",
        help="PostgreSQL URL; otherwise AOITALK_SYNCV3_DATABASE_URL or POSTGRES_* is used",
    )
    parser.add_argument("--schema", help="optional PostgreSQL schema/search_path")
    return parser


def _database_url(argument: str | None) -> URL:
    raw = argument or os.getenv("AOITALK_SYNCV3_DATABASE_URL") or os.getenv("DATABASE_URL")
    if raw:
        url = make_url(raw)
        if url.drivername.endswith("+asyncpg"):
            url = url.set(drivername="postgresql+psycopg2")
        elif url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg2")
        return url
    return URL.create(
        "postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER", "aoitalk"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "aoitalk_memory"),
    )


def _target_identifier(url: URL, schema: str | None) -> str:
    host = url.host or "local"
    port = f":{url.port}" if url.port else ""
    database = url.database or ""
    suffix = f" schema={schema}" if schema else ""
    return f"{host}{port}/{database}{suffix}"


def _alembic_config(url: URL, schema: str | None) -> Config:
    config = Config(str(ROOT / "alembic_sync_v3.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic_sync_v3"))
    config.set_main_option("version_table", SYNC_V3_VERSION_TABLE)
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    config.attributes["postgres_schema"] = schema
    return config


def _revision_descends_from(script: ScriptDirectory, head: str, target: str) -> bool:
    pending = [head]
    seen: set[str] = set()
    while pending:
        revision = pending.pop()
        if revision in seen:
            continue
        seen.add(revision)
        if revision == target:
            return True
        item = script.get_revision(revision)
        if item is None:
            return False
        down = item.down_revision
        if isinstance(down, str):
            pending.append(down)
        elif isinstance(down, tuple):
            pending.extend(down)
    return False


def _revision_ancestry_ok(heads: tuple[str, ...]) -> bool:
    if not heads:
        return False
    config = Config(str(ROOT / "alembic_sync_v3.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic_sync_v3"))
    script = ScriptDirectory.from_config(config)
    return all(
        _revision_descends_from(script, head, SYNC_V3_LATEST_REVISION)
        for head in heads
    )


def _read_report(url: URL, *, schema: str | None, require_revision: bool):
    schema = validate_postgres_schema(schema)
    connect_args = {"options": f"-c search_path={schema}"} if schema else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            report = inspect_connection(connection, check_key=True, require_revision=False, include_docs_visibility=True)
            heads = tuple(
                MigrationContext.configure(
                    connection, opts={"version_table": SYNC_V3_VERSION_TABLE}
                ).get_current_heads()
            )
            current = ",".join(sorted(heads)) if heads else None
            if require_revision and not _revision_ancestry_ok(heads):
                missing = (*report.missing, f"alembic_revision_ancestry:{SYNC_V3_LATEST_REVISION}")
                report = replace(report, ready=False, revision_ok=False, missing=tuple(sorted(set(missing))))
            return report, current
    finally:
        engine.dispose()


def _run_apply(url: URL, *, schema: str | None):
    # The migration itself repeats this guard inside its transaction.  Keeping a
    # read-only preflight here gives operators a concrete failure before Alembic
    # starts and still leaves all writes to the explicit ``apply`` operation.
    schema = validate_postgres_schema(schema)
    connect_args = {"options": f"-c search_path={schema}"} if schema else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            migration_preflight(connection)
    finally:
        engine.dispose()

    config = _alembic_config(url, schema)
    command.upgrade(config, SYNC_V3_LATEST_REVISION)
    return _read_report(url, schema=schema, require_revision=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    schema = validate_postgres_schema(args.schema or os.getenv("POSTGRES_SCHEMA"))
    try:
        url = _database_url(args.url)
        target = _target_identifier(url, schema)
        if args.operation == "apply":
            report, current = _run_apply(url, schema=schema)
        else:
            report, current = _read_report(
                url, schema=schema, require_revision=args.operation == "verify"
            )
        payload = {
            "operation": args.operation,
            "target": target,
            "migration_revision": SYNC_V3_LATEST_REVISION,
            "current_revision": current,
            "report": report.as_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if args.operation == "verify":
            return 0 if report.ready else 1
        if args.operation == "apply":
            # Apply succeeds with the feature flag still false or a missing key;
            # the report keeps that readiness state visible and v3 remains off.
            return 0 if not report.missing and not report.wrong else 1
        return 0
    except Exception as exc:  # noqa: BLE001
        # Exception text from a driver can contain connection details.  The
        # catalog report above carries the concrete safe object names; failures
        # here intentionally expose only a stable class marker.
        print(
            json.dumps(
                {
                    "operation": args.operation,
                    "status": "error",
                    "error": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
