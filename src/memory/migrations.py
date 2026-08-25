"""Alembic migration helpers for the local PostgreSQL database."""

from __future__ import annotations

import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import and_, create_engine, or_, select
from sqlalchemy.orm import Session

from ..models.ecc_models import Character
from ..utils.startup_timing import get_startup_timer


_startup_timer = get_startup_timer()


def run_migrations(sync_database_url: str) -> bool:
    """Run and verify Alembic upgrades against the configured database.

    Runtime code must never silently replace a failed migration with
    ``Base.metadata.create_all``.  That path can leave an apparently usable
    but version-inconsistent database, which is especially dangerous for an
    Enterprise deployment.
    """
    repo_root = Path(__file__).resolve().parents[2]
    alembic_ini = repo_root / "alembic.ini"
    script_location = repo_root / "alembic"

    if not alembic_ini.exists() or not script_location.exists():
        raise FileNotFoundError(
            f"Alembic files are missing: ini={alembic_ini}, scripts={script_location}"
        )

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(script_location))
    # Alembic's Config uses ConfigParser interpolation. SQLAlchemy URLs
    # percent-encode credentials, so escape percent signs before handing the
    # URL to Alembic (the engine itself still receives the original URL).
    config.set_main_option(
        "sqlalchemy.url", sync_database_url.replace("%", "%%")
    )
    with _startup_timer.phase("startup.database.migrations.alembic_upgrade"):
        command.upgrade(config, "head")

    script = ScriptDirectory.from_config(config)
    with _startup_timer.phase("startup.database.migrations.expected_head"):
        expected_head = script.get_current_head()
    if not expected_head:
        raise RuntimeError("Alembic migration head is not defined")

    with _startup_timer.phase("startup.database.migrations.revision_verify"):
        verify_engine = create_engine(sync_database_url, pool_pre_ping=True)
        try:
            with verify_engine.connect() as connection:
                current_heads = MigrationContext.configure(connection).get_current_heads()
        finally:
            verify_engine.dispose()

    if tuple(current_heads) != (expected_head,):
        raise RuntimeError(
            "Alembic migration verification failed: "
            f"expected=({expected_head},), current={current_heads}"
        )
    with _startup_timer.phase("startup.database.migrations.default_character"):
        _ensure_default_character(sync_database_url)
    return True


def _ensure_default_character(sync_database_url: str) -> None:
    """Ensure a fresh Linux/Enterprise database has a usable chat character."""
    seed_engine = create_engine(sync_database_url, pool_pre_ping=True)
    try:
        with Session(seed_engine) as session:
            existing = session.scalar(
                select(Character).where(
                    or_(
                        Character.slug == "project_manager",
                        and_(
                            Character.name == "案件管理アシスタント",
                            Character.character_type == "assistant",
                        ),
                    )
                )
            )
            if existing is not None:
                return
            session.add(
                Character(
                    id=uuid.uuid4(),
                    name="案件管理アシスタント",
                    slug="project_manager",
                    character_type="assistant",
                    system_prompt=(
                        "通常チャットに答えつつ、ユーザーが案件・タスク・WBS・進捗・予定・台帳などの"
                        "管理作業を求めた時だけ、AoiTalk の Project を案件の基準IDとして扱って支援する。\n"
                        "日本語で簡潔、実務的、結論先出しで応答する。"
                    ),
                    greeting="案件まわり、確認する？",
                    invalid_content_reply="その内容は扱えない。別の形で整理しよう。",
                    fallback_reply="うまく整理できなかった。対象案件か、やりたい操作をもう少し具体的に教えて。",
                    goodbye_reply="また必要になったら呼んで。",
                    recognition_aliases=[
                        "案件管理",
                        "案件管理アシスタント",
                        "プロジェクト管理",
                        "進行管理",
                        "PM",
                    ],
                    is_enabled=True,
                )
            )
            session.commit()
    finally:
        seed_engine.dispose()
