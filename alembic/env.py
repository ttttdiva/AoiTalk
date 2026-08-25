from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from src.memory.models import Base

# .env から POSTGRES_* を読み込み、alembic.ini の sqlalchemy.url を上書きする。
# これにより Windows と Linux/WSL2 で別の DB 接続先を .env だけで切り替え可能。
load_dotenv()

config = context.config

if config.config_file_name is not None:
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


def run_migrations_offline() -> None:
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
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
