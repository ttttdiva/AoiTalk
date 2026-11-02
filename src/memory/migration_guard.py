"""Keep worktree checkouts from migrating the shared AoiTalk database.

A checkout under ``.worktrees/`` finds the main repository's ``.env`` through
``load_dotenv()`` and would otherwise upgrade or downgrade the database the
running AoiTalk uses.  Such a run is refused unless it targets another
database, an isolated ``POSTGRES_SCHEMA``, or is explicitly allowed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ALLOW_ENV = "AOITALK_ALLOW_WORKTREE_MIGRATION"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _target(host: object, port: object, database: object) -> tuple[str, str, str]:
    host = str(host or "localhost").lower()
    return ("localhost" if host in _LOOPBACK else host, str(port or 5432), str(database or ""))


def refuse_shared_database_from_worktree(
    code_root: Path,
    database_url: str | None,
    schema: str | None,
    environ: Mapping[str, str] = os.environ,
) -> None:
    parts = code_root.resolve().parts
    if ".worktrees" not in parts or environ.get(ALLOW_ENV) == "1" or schema or not database_url:
        return
    main_root = Path(*parts[: parts.index(".worktrees")])
    shared = dotenv_values(main_root / ".env")
    if not shared.get("POSTGRES_DB"):
        return
    url = make_url(database_url)
    if _target(url.host, url.port, url.database) == _target(
        shared.get("POSTGRES_HOST"), shared.get("POSTGRES_PORT"), shared.get("POSTGRES_DB")
    ):
        raise RuntimeError(
            f"{code_root} は worktree です。本体の .env の DB（稼働中の AoiTalk）への migration は拒否します。"
            f" 別の DB か POSTGRES_SCHEMA を指定してください（意図して行うときだけ {ALLOW_ENV}=1）。"
        )
