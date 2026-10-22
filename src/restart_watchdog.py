"""Fallback watchdog for WebUI-triggered AoiTalk restarts.

The normal path is still run.bat observing backend exit code 42.  run.bat writes
one launcher-generation marker every time it enters its :start loop.  The
watchdog snapshots that marker before the backend exits and only starts a new
launcher when the marker fails to change, avoiding duplicate launches even when
normal startup is slow.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_LAUNCHER_MARKER = Path(".local") / "launcher-generation.txt"
_LAUNCHER_GRACE_SECONDS = 12.0
_PROBE_INTERVAL_SECONDS = 0.25


def _read_launcher_generation(project_root: Path) -> str | None:
    marker = project_root / _LAUNCHER_MARKER
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def wait_for_launcher_generation(
    project_root: Path,
    previous_generation: str | None,
    *,
    grace_seconds: float = _LAUNCHER_GRACE_SECONDS,
    probe_interval_seconds: float = _PROBE_INTERVAL_SECONDS,
) -> bool:
    """Return True once the existing run.bat enters a fresh :start cycle."""
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        current = _read_launcher_generation(project_root)
        if current is not None and current != previous_generation:
            return True
        time.sleep(
            min(
                probe_interval_seconds,
                max(0.0, deadline - time.monotonic()),
            )
        )

    current = _read_launcher_generation(project_root)
    return current is not None and current != previous_generation


def start_launcher(project_root: Path) -> None:
    run_bat = project_root / "run.bat"
    if not run_bat.is_file():
        return

    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(run_bat)],
        cwd=str(project_root),
        creationflags=creationflags,
        close_fds=True,
    )


def run_watchdog(
    project_root: Path,
    previous_generation: str | None,
) -> None:
    if wait_for_launcher_generation(project_root, previous_generation):
        return
    start_launcher(project_root)


def launch_restart_watchdog(project_root: Path) -> bool:
    """Spawn a detached watchdog process.  Returns False if spawning fails."""
    if os.name != "nt":
        return False

    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        executable = pythonw

    previous_generation = _read_launcher_generation(project_root) or ""

    creationflags = 0
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        subprocess.Popen(
            [
                str(executable),
                "-m",
                "src.restart_watchdog",
                "--project-root",
                str(project_root),
                "--previous-generation",
                previous_generation,
            ],
            cwd=str(project_root),
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--previous-generation", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    run_watchdog(
        args.project_root.resolve(),
        args.previous_generation or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
