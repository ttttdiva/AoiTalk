from __future__ import annotations

import os
import logging
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from src.memory.config import postgres_search_path, validate_isolated_schema_heads
from src.memory.models import Base

# .env から POSTGRES_* を読み込み、alembic.ini の sqlalchemy.url を上書きする。
# これにより Windows と Linux/WSL2 で別の DB 接続先を .env だけで切り替え可能。
load_dotenv()

config = context.config

if config.config_file_name is not None and not any(
    getattr(handler, "_aoitalk_application_handler", False)
    for handler in logging.getLogger().handlers
):
    # Keep application loggers active when tests/runtime invoke Alembic in
    # process.  The default ``disable_existing_loggers=True`` silences
    # ``src.bot.discord_bot`` after the first migration and makes subsequent
    # Discord ingress diagnostics (including reply failures) disappear.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_pg_host = os.getenv("POSTGRES_HOST")
_pg_port = os.getenv("POSTGRES_PORT")
_pg_user = os.getenv("POSTGRES_USER")
_pg_password = os.getenv("POSTGRES_PASSWORD")
_pg_db = os.getenv("POSTGRES_DB")
if all([_pg_host, _pg_port, _pg_user, _pg_password, _pg_db]):
    _safe_user = quote_plus(str(_pg_user))
    _safe_password = quote_plus(str(_pg_password))
    config.set_main_option(
        "sqlalchemy.url",
        # ConfigParser interpolation treats `%` specially; doubling it here
        # makes the value survive until engine_from_config reads the option.
        f"postgresql://{_safe_user}:{_safe_password}@{_pg_host}:{_pg_port}/{_pg_db}".replace(
            "%", "%%"
        ),
    )

target_metadata = Base.metadata


def _configured_postgres_schema() -> str | None:
    if "postgres_schema" in config.attributes:
        return config.attributes["postgres_schema"]
    return os.getenv("POSTGRES_SCHEMA")


def _schema_safe_revisions() -> set[str]:
    """Return revisions on a descendant path from the safe baseline."""

    baseline = "20260830_0001"
    script = ScriptDirectory.from_config(config)
    allowed = {baseline}
    for head in script.get_heads():
        stack: list[tuple[str, list[str]]] = [(head, [])]
        while stack:
            revision_id, path = stack.pop()
            revision = script.get_revision(revision_id)
            if revision is None:
                continue
            next_path = [*path, revision.revision]
            if revision.revision == baseline:
                allowed.update(next_path)
                continue
            down = revision.down_revision
            if isinstance(down, str):
                stack.append((down, next_path))
            elif isinstance(down, tuple):
                stack.extend((item, next_path) for item in down)
    return allowed


def run_migrations_offline() -> None:
    if postgres_search_path(_configured_postgres_schema()) is not None:
        raise RuntimeError(
            "offline Alembic is disabled with POSTGRES_SCHEMA because the "
            "pre-provisioned baseline cannot be verified"
        )
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Keep migration sessions in the same optional per-worktree schema as
    # runtime connections.  ``None`` preserves the historical empty
    # ``connect_args`` behavior; the schema value is validated before it is
    # interpolated into psycopg2's ``options`` string.
    search_path = postgres_search_path(_configured_postgres_schema())
    engine_kwargs = {
        "prefix": "sqlalchemy.",
        "poolclass": pool.NullPool,
        "future": True,
    }
    if search_path is not None:
        engine_kwargs["connect_args"] = {"options": f"-c search_path={search_path}"}

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        **engine_kwargs,
    )

    with connectable.connect() as connection:
        if search_path is not None:
            current_heads = MigrationContext.configure(connection).get_current_heads()
            validate_isolated_schema_heads(
                tuple(current_heads),
                safe_revisions=_schema_safe_revisions(),
            )
            # SQLAlchemy 2 autobegins for the read-only alembic_version
            # preflight above. End that transaction before Alembic takes
            # ownership of the migration transaction; otherwise the outer
            # connection context rolls the DDL and revision update back.
            if connection.in_transaction():
                connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
