"""Relaunch run.bat visibly when its parent has no visible console."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Sequence


RELAUNCHED_EXIT_CODE = 10


def _parent_console_is_visible(parent_pid: int) -> bool:
    if os.name != "nt":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype = wintypes.BOOL
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.FreeConsole.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    if not kernel32.AttachConsole(parent_pid):
        return False
    try:
        window = kernel32.GetConsoleWindow()
        return bool(window and user32.IsWindowVisible(window))
    finally:
        kernel32.FreeConsole()


def _launch_visible_console(
    batch_file: Path,
    arguments: Sequence[str],
    *,
    popen: Callable[..., object] = subprocess.Popen,
) -> bool:
    if os.name != "nt":
        return False
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if not creation_flags:
        return False

    command = subprocess.list2cmdline(["call", str(batch_file), *arguments])
    command_processor = os.environ.get("COMSPEC", "cmd.exe")
    try:
        popen(
            [command_processor, "/d", "/c", command],
            cwd=str(batch_file.parent),
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def ensure_visible_console(
    batch_file: Path,
    arguments: Sequence[str],
    *,
    parent_pid: int,
    console_visible: Callable[[int], bool],
    launch: Callable[[Path, Sequence[str]], bool],
) -> int:
    if console_visible(parent_pid):
        return 0
    return RELAUNCHED_EXIT_CODE if launch(batch_file, arguments) else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return 2

    batch_file = Path(arguments.pop(0)).resolve()
    if not batch_file.is_file():
        return 2
    if os.name != "nt":
        return 0

    return ensure_visible_console(
        batch_file,
        arguments,
        parent_pid=os.getppid(),
        console_visible=_parent_console_is_visible,
        launch=_launch_visible_console,
    )


if __name__ == "__main__":
    raise SystemExit(main())
