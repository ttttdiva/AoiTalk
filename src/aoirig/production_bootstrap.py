"""Create or resume only the explicitly requested authoring tool's Python env.

This is installation plumbing, not avatar production. Dependencies and inference
readiness remain the versioned production CLI's responsibility. Existing envs are
never deleted, cleared, recreated, or silently replaced.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from ..services.storage_roots import assert_no_links
from ..services.storage_io import StorageError

ProcessRunner = Callable[..., Awaitable[tuple[int, bytes]]]
STATE_FILE = ".aoitalk-python-bootstrap.json"


class BootstrapError(ValueError):
    """Explicit environment preparation could not safely continue."""


def _safe_path(path: Path, *, allow_missing: bool) -> None:
    try:
        assert_no_links(path, allow_missing=allow_missing)
    except StorageError as exc:
        raise BootstrapError("制作環境にsymlink / junction / reparse pointは使用できません") from exc


def _read_state(path: Path) -> dict:
    _safe_path(path, allow_missing=False)
    if path.stat().st_size > 4096:
        raise BootstrapError("Python環境の作成記録が不正です")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BootstrapError("Python環境の作成記録が不正です")
    return value


def _write_state(path: Path, value: dict) -> None:
    _safe_path(path, allow_missing=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_tool_python(root: Path, run: ProcessRunner) -> Path:
    """Called only under the admin's installation lock after tool identity checks.

    `run` is the existing bounded, allowlisted subprocess adapter. No shell, pip,
    package URL, user model, alternate path or credential is accepted here.
    """
    _safe_path(root, allow_missing=False)
    root = root.resolve()
    if not root.is_dir():
        raise BootstrapError("制作ツールの導入先がありません")
    relative_python = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for name in (".venv", "venv"):
        environment = root / name
        executable = environment / relative_python
        _safe_path(environment, allow_missing=True)
        _safe_path(executable, allow_missing=True)
        if executable.is_file():
            return executable
    environment = root / ".venv"
    state_path = root / STATE_FILE
    expected = {"identity": "aoitalk-production-python", "schema_version": 1,
                "root": str(root), "environment": ".venv"}
    if environment.exists():
        if not state_path.is_file():
            raise BootstrapError("既存の未完成Python環境は自動変更しません。作成記録を確認してください")
        state = _read_state(state_path)
        if any(state.get(key) != value for key, value in expected.items()) or state.get("status") not in {"creating", "failed"}:
            raise BootstrapError("既存Python環境の作成記録が一致しません")
    else:
        # A differently named incomplete user venv is not ours to replace.
        if (root / "venv").exists():
            raise BootstrapError("既存venvのPythonが見つかりません。別環境は勝手に追加しません")
        if state_path.exists():
            state = _read_state(state_path)
            if any(state.get(key) != value for key, value in expected.items()):
                raise BootstrapError("Python環境の作成記録が一致しません")
    state = {**expected, "status": "creating"}
    _write_state(state_path, state)
    try:
        # --copies prevents implicit executable symlinks in a newly owned env.
        code, _ = await run([sys.executable, "-m", "venv", "--copies", str(environment)], root, timeout=300)
        executable = environment / relative_python
        _safe_path(executable, allow_missing=False)
        if code != 0 or not executable.is_file():
            raise BootstrapError("制作専用Python環境の準備に失敗しました。既存データは保持しています")
        _write_state(state_path, {**expected, "status": "ready"})
        return executable
    except BaseException:
        _write_state(state_path, {**expected, "status": "failed"})
        raise
