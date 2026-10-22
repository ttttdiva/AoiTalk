"""Fixed broker bootstrap, preserving a Windows venv without its redirector.

CPython's own multiprocessing launcher uses the same base executable plus
__PYVENV_LAUNCHER__ contract (3.12.10 popen_spawn_win32.py, lines 55-60).
No existing Job Object is edited; the child still verifies its own ownership.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import subprocess
import sys

_WINDOWS = os.name == "nt"


def uses_windows_venv():
    base = getattr(sys, "_base_executable", sys.executable)
    return _WINDOWS and ntpath.normcase(base) != ntpath.normcase(sys.executable)


def python_launch_spec(arguments):
    """Use only this interpreter's metadata, never HTTP-supplied paths/commands."""
    executable = sys.executable
    environment = os.environ.copy()
    if _WINDOWS:
        environment.pop("__PYVENV_LAUNCHER__", None)
        if uses_windows_venv():
            executable = sys._base_executable
            if not os.path.isabs(executable) or not os.path.isfile(executable):
                raise RuntimeError("The Windows venv base interpreter is unavailable; coordinator was not started")
            environment["__PYVENV_LAUNCHER__"] = sys.executable
    return [executable, *arguments], environment


def launch_coordinator():
    root = Path(__file__).resolve().parents[2]
    logs = root / "logs" / "shared_local_runtime"
    logs.mkdir(parents=True, exist_ok=True)
    command, environment = python_launch_spec([
        "-m", "src.shared_local_runtime", "--serve", "--direct-python"])
    kwargs = {"cwd": str(root), "stdin": subprocess.DEVNULL,
              "close_fds": True, "env": environment}
    if _WINDOWS:
        # DETACHED_PROCESS would leave the coordinator without a console, so every
        # console child (strata.exe --version, server.py) gets a visible one that the
        # default terminal shows and focuses. A hidden console is inherited instead.
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    else:
        kwargs["start_new_session"] = True
    with (logs / "coordinator.log").open("ab") as output:
        # The caller must never add this process to AoiTalk's child-process list.
        # A denied breakaway is an error, never retried with weaker flags.
        return subprocess.Popen(command, stdout=output, stderr=output, **kwargs)
