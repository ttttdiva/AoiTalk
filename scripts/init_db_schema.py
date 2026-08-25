"""AoiTalk DB スキーマを Alembic の正規ルートで初期化・検証する。

``create_all`` と ``stamp head`` の組み合わせは、既存テーブルを未管理の
まま最新扱いにできるため使用しない。新規DBも既存DBも ``upgrade head``
だけで処理し、migration履歴が壊れている場合は非ゼロ終了する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402


def _alembic_config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    # ConfigParser treats percent signs as interpolation markers. URL.create
    # correctly escapes credentials, therefore double the signs only for the
    # Alembic config value.
    config.set_main_option(
        "sqlalchemy.url",
        url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def _verify_head(config: Config, engine) -> None:
    script = ScriptDirectory.from_config(config)
    expected_head = script.get_current_head()
    if not expected_head:
        raise RuntimeError("Alembic migration head is not defined")
    with engine.connect() as connection:
        current_heads = MigrationContext.configure(connection).get_current_heads()
    if tuple(current_heads) != (expected_head,):
        raise RuntimeError(
            "Alembic head verification failed: "
            f"expected=({expected_head},), current={current_heads}"
        )


def main() -> int:
    url = URL.create(
        "postgresql",
        username=os.getenv("POSTGRES_USER", "aoitalk"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "aoitalk_memory"),
    )

    engine = create_engine(url, pool_pre_ping=True)
    config = _alembic_config(url)
    try:
        print("[db-schema] alembic upgrade head")
        command.upgrade(config, "head")
        _verify_head(config, engine)
        seed_default_character(engine)
        print("[db-schema] migration head verified")
        return 0
    except Exception as exc:
        print(f"[db-schema] ERROR: migration failed or verification failed: {exc}")
        return 1
    finally:
        engine.dispose()


def seed_default_character(engine) -> None:
    """既定の案件管理キャラクターがなければ投入する。"""
    from sqlalchemy.orm import Session
    from sqlalchemy import and_, or_, select
    from src.models.ecc_models import Character

    with Session(engine) as session:
        if session.scalar(
            select(Character).where(
                or_(
                    Character.slug == "project_manager",
                    and_(
                        Character.name == "案件管理アシスタント",
                        Character.character_type == "assistant",
                    ),
                )
            )
        ):
            return
        session.add(
            Character(
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
        print("[db-schema] seeded default character: project_manager")


if __name__ == "__main__":
    sys.exit(main())
