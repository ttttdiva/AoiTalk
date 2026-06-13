"""Alembic migration helpers for the local PostgreSQL database."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def run_migrations(sync_database_url: str) -> bool:
    """Run Alembic upgrades against the configured database."""
    repo_root = Path(__file__).resolve().parents[2]
    alembic_ini = repo_root / "alembic.ini"
    script_location = repo_root / "alembic"

    if not alembic_ini.exists() or not script_location.exists():
        return False

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", sync_database_url)
    command.upgrade(config, "head")
    return True
