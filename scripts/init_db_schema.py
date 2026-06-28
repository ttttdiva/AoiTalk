"""AoiTalk DBスキーマの初期化/アップグレード (setup.bat / setup.sh から呼び出される)

- 新規DB (alembic_version なし): モデルから全テーブルを作成し、alembic stamp head する。
  (アプリ起動時の Base.metadata.create_all と同じ正規ルート)
- 既存DB (alembic_version あり): alembic upgrade head で増分マイグレーションを適用する。

接続情報は .env の POSTGRES_* から読み取る。
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
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from src.memory.models import Base  # noqa: E402

# ECC (モバイル同期) のモデルも同じ Base に登録されるため、create_all 前に import が必要
import src.models.ecc_models  # noqa: E402,F401


def main() -> int:
    url = URL.create(
        "postgresql",
        username=os.getenv("POSTGRES_USER", "aoitalk"),
        password=os.getenv("POSTGRES_PASSWORD", "aoitalk_password"),
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "aoitalk_memory"),
    )

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        has_alembic_version = inspector.has_table("alembic_version")
    except Exception as exc:
        print(f"[db-schema] ERROR: database connection failed: {exc}")
        return 1

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))

    if has_alembic_version:
        print("[db-schema] existing alembic_version found -> alembic upgrade head")
        command.upgrade(config, "head")
    else:
        print("[db-schema] fresh database -> create_all + alembic stamp head")
        Base.metadata.create_all(engine)
        command.stamp(config, "head")

    seed_default_character(engine)

    engine.dispose()
    print("[db-schema] done")
    return 0


def seed_default_character(engine) -> None:
    """characters テーブルが空の場合のみ、既定キャラクターを投入する。

    config_defaults.py の default_character (案件管理アシスタント) が DB に存在しないと
    アプリが起動できないため、新規セットアップではここでシードする。
    """
    from sqlalchemy.orm import Session

    from src.models.ecc_models import Character

    with Session(engine) as session:
        existing = session.query(Character).count()
        if existing > 0:
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
