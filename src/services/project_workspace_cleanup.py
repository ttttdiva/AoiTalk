"""Project workspace filesystem cleanup helpers."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from uuid import UUID


def get_project_workspace_path(
    project_id: UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the canonical project workspace path without creating it."""
    root_value = workspace_root or os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    root = Path(root_value).resolve()
    target = (root / "_projects" / f"project_{project_id}").resolve()

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Project workspace path escaped the workspace root.") from exc

    if target.parent.name != "_projects" or target.name != f"project_{project_id}":
        raise ValueError("Unexpected project workspace path.")

    return target


def remove_project_workspace(
    project_id: UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Delete a project's workspace directory if it exists.

    Returns True when a filesystem entry was removed, False when there was
    nothing to remove.
    """
    target = get_project_workspace_path(project_id, workspace_root=workspace_root)
    if not target.exists() and not target.is_symlink():
        return False

    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target, onerror=_remove_readonly)
    return True


def _remove_readonly(function, path, _exc_info) -> None:
    """Retry removal after making Windows read-only files writable."""
    os.chmod(path, stat.S_IWRITE)
    function(path)
