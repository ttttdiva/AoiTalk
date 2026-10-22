"""Stable machine and process identity helpers for saved action runs."""
from __future__ import annotations

import hashlib
import os
import platform
import socket
import time
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def boot_id() -> str:
    """Return a value that changes on an OS reboot when available."""
    value = _read_text(Path("/proc/sys/kernel/random/boot_id"))
    if value:
        return value[:128]
    if os.name == "nt":
        try:
            import ctypes
            tick = ctypes.windll.kernel32.GetTickCount64()
            # Minute granularity avoids a boundary race while still changing
            # on every normal reboot.  It is only an identity fence, not a
            # wall-clock timestamp.
            return f"boot-{int((time.time() - float(tick) / 1000.0) // 60)}"
        except Exception:
            pass
    try:
        import psutil  # type: ignore
        return f"boot-{int(psutil.boot_time() * 1000)}"
    except Exception:
        # This is deliberately process-local on platforms without a boot ID;
        # a restart then reconciles an in-flight run as unknown.
        return f"process-{os.getpid()}"


def stable_machine_id() -> str:
    """Derive a stable opaque machine id without exposing hardware secrets."""
    candidates = []
    if os.name == "nt":
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                candidates.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
        except Exception:
            pass
    else:
        candidates.append(_read_text(Path("/etc/machine-id")))
    candidates.append(platform.node())
    candidates.append(socket.gethostname())
    material = "\x1f".join(value for value in candidates if value)
    if not material:
        material = f"pid:{os.getpid()}"
    return "m-" + hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def process_identity(pid: int | None) -> dict[str, Any] | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        import psutil  # type: ignore
        process = psutil.Process(pid)
        with process.oneshot():
            created = float(process.create_time())
            exe = process.exe() or ""
            parent = process.ppid()
        return {"pid": pid, "created": created, "exe": exe, "parent_pid": int(parent)}
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                               ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                               ctypes.POINTER(wintypes.FILETIME)]
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    created = wintypes.FILETIME()
                    exited = wintypes.FILETIME()
                    system = wintypes.FILETIME()
                    user = wintypes.FILETIME()
                    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                  ctypes.byref(system), ctypes.byref(user)):
                        return None
                    ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    exe = ""
                    if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        exe = buffer.value
                    return {"pid": pid, "created": ticks, "exe": exe, "parent_pid": None}
                finally:
                    kernel.CloseHandle(handle)
        except Exception:
            pass
    if os.name != "nt":
        stat = Path(f"/proc/{pid}/stat")
        if stat.is_file():
            try:
                fields = stat.read_text(encoding="ascii", errors="ignore").split()
                # Linux field 22 is process start ticks; boot_id is paired
                # with the current host identity in the journal.
                return {"pid": pid, "created": float(fields[21]), "exe": os.readlink(f"/proc/{pid}/exe"),
                        "parent_pid": int(fields[3])}
            except (OSError, ValueError, IndexError):
                pass
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return {"pid": pid, "created": None, "exe": "", "parent_pid": None}


def process_identity_matches(expected: dict[str, Any] | None, *, boot: str | None = None,
                             current_boot: str | None = None) -> bool:
    if not isinstance(expected, dict):
        return False
    if boot is not None and current_boot is not None and boot != current_boot:
        return False
    current = process_identity(expected.get("pid"))
    if current is None:
        return False
    if expected.get("created") is not None and current.get("created") is not None:
        return float(expected["created"]) == float(current["created"])
    # A pid without an immutable creation time is never safe to use for stop
    # or readiness.  Status can report unknown instead.
    return False


def process_started(pid: int | None) -> float | None:
    identity = process_identity(pid)
    return None if identity is None else identity.get("created")


def descendant_pids(root_pid: int) -> set[int]:
    """Return root and descendants where process ancestry is observable."""
    try:
        import psutil  # type: ignore
        root = psutil.Process(root_pid)
        return {int(root.pid), *(int(item.pid) for item in root.children(recursive=True))}
    except Exception:
        return {root_pid} if process_identity(root_pid) else set()


def hash_executable(path: str | os.PathLike[str]) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


__all__ = ["boot_id", "descendant_pids", "hash_executable", "process_identity",
           "process_identity_matches", "process_started", "stable_machine_id"]
