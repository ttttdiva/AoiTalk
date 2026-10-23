"""Alembic environment for the explicit Sync v3 schema operation."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.memory.config import postgres_search_path
from src.sync_v3.schema_readiness import SYNC_V3_VERSION_TABLE


config = context.config


def _configured_postgres_schema() -> str | None:
    value = config.attributes.get("postgres_schema")
    if value is not None:
        return value
    return None


def _version_table() -> str:
    return config.get_main_option("version_table") or SYNC_V3_VERSION_TABLE


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=_version_table(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
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
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table=_version_table(),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
