"""Read project-scoped agent instructions safely."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from .project_workspace_cleanup import get_project_workspace_path

logger = logging.getLogger(__name__)
MAX_AGENTS_BYTES = 16 * 1024


def load_project_agents_instructions(project_id: str, *, workspace_root: str | Path | None = None) -> str | None:
    workspace = get_project_workspace_path(UUID(str(project_id)), workspace_root=workspace_root)
    path = workspace / ".agents" / "AGENTS.md"
    if not path.is_file():
        return None
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError:
        logger.warning("workspace 外を指す AGENTS.md を拒否しました: %s", path)
        return None
    raw = path.read_bytes()
    if len(raw) > MAX_AGENTS_BYTES:
        logger.warning("%s は16KBを超えたため切り詰めます", path)
        raw = raw[:MAX_AGENTS_BYTES]
    return raw.decode("utf-8", errors="replace")
