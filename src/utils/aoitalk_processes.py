"""Find and stop AoiTalk main.py processes of one checkout (one boot at a time).

A previous boot can close its ports and still keep running its background
workers (2026-10-30 13:30: PID 57800 survived its shutdown).  Restarts wait for
the process itself, and a new boot stops such a leftover before it starts.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _script_of(process: psutil.Process) -> Path | None:
    for argument in process.cmdline()[1:]:
        if argument.lower().endswith("main.py"):
            path = Path(argument)
            if not path.is_absolute():
                path = Path(process.cwd()) / path
            return path.resolve()
    return None


def main_processes(main_py: Path, *, exclude: set[int] = frozenset()) -> list[psutil.Process]:
    """Top-most processes running ``main_py``; a venv launcher hides its interpreter child."""
    target = main_py.resolve()
    found: dict[int, psutil.Process] = {}
    for process in psutil.process_iter(["name"]):
        if process.pid in exclude or not (process.info["name"] or "").lower().startswith("python"):
            continue
        try:
            script = _script_of(process)
        except (psutil.Error, OSError):
            continue
        if script is not None and _same_path(script, target):
            found[process.pid] = process
    tops = []
    for process in found.values():
        try:
            if process.ppid() not in found:
                tops.append(process)
        except psutil.Error:
            continue
    return tops


def own_lineage() -> set[int]:
    """This process and its ancestors (the venv launcher runs main.py too)."""
    pids = {os.getpid()}
    try:
        pids.update(parent.pid for parent in psutil.Process().parents())
    except psutil.Error:
        pass
    return pids


def kill_tree(process: psutil.Process, *, timeout: float = 15) -> None:
    try:
        members = [process, *process.children(recursive=True)]
    except psutil.NoSuchProcess:
        return
    for member in members:
        try:
            member.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(members, timeout=timeout)


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def stop_previous_boot(main_py: Path, ports: tuple[int, ...], *, grace_seconds: float = 90) -> list[int]:
    """Stop another boot of ``main_py`` that holds none of ``ports``.

    One that serves the ports is a running AoiTalk and is left to the
    existing-instance check.  One without them is either still starting (it
    binds within ``grace_seconds``) or a leftover that outlived its shutdown;
    the leftover is killed with its children.  Returns the killed PIDs.
    """
    deadline = time.monotonic() + grace_seconds
    others = main_processes(main_py, exclude=own_lineage())
    while others and time.monotonic() < deadline:
        if any(_listening(port) for port in ports):
            return []
        time.sleep(2)
        others = [process for process in others if process.is_running()]
    if not others or any(_listening(port) for port in ports):
        return []
    killed = [process.pid for process in others]
    logger.warning("前の boot の main.py (PID %s) がポートを閉じたまま残っていたため、終了させます", killed)
    for process in others:
        kill_tree(process)
    return killed
