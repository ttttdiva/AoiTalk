"""Independent subprocess supervisor for saved PC actions.

The supervisor owns the action process after the bridge WebSocket disappears.
It communicates only through the run's SQLite journal and fixed input/output
files.  No command, path, or input argument is accepted from the server.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .action_identity import (boot_id, descendant_pids, hash_executable, process_identity,
                              process_identity_matches)
from .action_journal import ActionJournal, TERMINAL_STATES
from .action_protocol import (ActionProtocolError, MAX_RESULT_BYTES, canonical_json,
                              sha256_file)


def _hidden_popen_kwargs(*, desktop_visible: bool = False) -> dict[str, Any]:
    """Return hidden helper flags, or visible flags for a desktop executable."""
    kwargs: dict[str, Any] = {"shell": False, "close_fds": True}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if desktop_visible:
            startup.wShowWindow = getattr(subprocess, "SW_SHOWNORMAL", 1)
            kwargs.update(startupinfo=startup, creationflags=0)
        else:
            startup.wShowWindow = subprocess.SW_HIDE
            kwargs.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
    return kwargs


def _command_for(path: Path) -> list[str] | str:
    """Build a command from a catalog path and its fixed extension only."""
    suffix = path.suffix.lower()
    if suffix in {".bat", ".cmd"}:
        comspec = os.environ.get("COMSPEC") or ("cmd.exe" if os.name == "nt" else "/bin/sh")
        # ``cmd /c`` has a special outer-quote grammar.  Passing a prequoted
        # token in an argv list makes ``list2cmdline`` escape those quotes and
        # cmd then treats the path as an unknown command.  Use one fixed
        # command-line string for the validated catalog path; no request input
        # is interpolated here.
        if os.name == "nt":
            return f'{subprocess.list2cmdline([comspec])} /d /s /c ""{path}""'
        return [comspec, str(path)]
    if suffix == ".ps1":
        powershell = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
        return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-File", str(path)]
    if suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def _safe_child_env(row: dict[str, Any]) -> dict[str, str]:
    env = {str(key): str(value) for key, value in os.environ.items()
           if isinstance(key, str) and isinstance(value, str)}
    env.update({
        "AOITALK_ACTION_RUN_ID": row["run_id"],
        "AOITALK_ACTION_REF": row["action_ref"],
        "AOITALK_ACTION_INPUT_FILE": row["input_path"],
        "AOITALK_ACTION_RESULT_FILE": row["output_path"],
        "AOITALK_ACTION_MODE": row["mode"],
    })
    # A subprocess must not interpret a server supplied command or path from
    # an inherited ad-hoc variable.  The fixed variables above are the only
    # action protocol variables and are overwritten unconditionally.
    return env


@contextlib.contextmanager
def _locked_action_file(path: Path):
    """Open the cataloged action while denying replacement/deletion on Windows.

    A catalog hash is a policy fence, but a BAT can otherwise be replaced after
    the pre-spawn hash and before the child exits.  ``CreateFileW`` with
    ``FILE_SHARE_READ`` keeps the exact file handle open for the whole action,
    so ordinary write/delete attempts fail until the supervisor has performed
    its post-exit hash check.  Non-Windows platforms retain the same lifetime
    and use a regular read handle; their post-exit hash remains the integrity
    fence.
    """
    stream = None
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.CreateFileW(str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None)
        invalid = ctypes.c_void_p(-1).value
        if not handle or handle == invalid:
            raise OSError("action_file_lock_unavailable")
        try:
            fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
            stream = os.fdopen(fd, "rb", buffering=0)
        except Exception:
            kernel.CloseHandle(handle)
            raise
    else:
        stream = path.open("rb")
    try:
        yield stream
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _hash_open_action(stream: Any) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while block := stream.read(1024 * 1024):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def _assert_catalog_path(path: Path) -> None:
    """Recheck the catalog path immediately before opening/spawning."""
    if path.is_symlink() or not path.is_file():
        raise ActionProtocolError("saved_action_path_invalid")
    for parent in (path, *path.parents):
        if parent == parent.anchor:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ActionProtocolError("saved_action_path_invalid")


def _dependency_entries(row: dict[str, Any]) -> list[tuple[Path, str]]:
    value = row.get("dependencies")
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ActionProtocolError("saved_action_dependencies_invalid")
    result: list[tuple[Path, str]] = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str)
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ActionProtocolError("saved_action_dependency_invalid")
        path = Path(item["path"])
        _assert_catalog_path(path)
        result.append((path, item["sha256"]))
    if len({path for path, _digest in result}) != len(result):
        raise ActionProtocolError("saved_action_dependency_duplicate")
    return result


def _capture_stream(journal: ActionJournal, run_id: str, stream: str, handle: Any) -> None:
    try:
        while True:
            raw = handle.readline()
            if not raw:
                break
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            journal.append_log(run_id, stream, line.rstrip("\r\n"))
    except Exception as exc:
        journal.append_log(run_id, "supervisor", "output_read_failed:" + type(exc).__name__)
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _probe_tcp(probe: dict[str, Any], root_pid: int) -> tuple[bool, dict[str, Any]]:
    try:
        with socket.create_connection((probe["host"], probe["port"]), timeout=probe["timeout_seconds"]):
            pass
    except OSError:
        return False, {"kind": "tcp", "reachable": False, "owned": False}
    owners: set[int] = set()
    try:
        import psutil  # type: ignore
        for connection in psutil.net_connections(kind="tcp"):
            local = connection.laddr
            port = getattr(local, "port", None) if local else None
            if port == probe["port"] and connection.pid:
                owners.add(int(connection.pid))
    except Exception:
        return False, {"kind": "tcp", "reachable": True, "owned": False}
    allowed = descendant_pids(root_pid)
    owned = bool(owners & allowed)
    return owned, {"kind": "tcp", "reachable": True, "owned": owned,
                   "owner_pids": sorted(owners & allowed)}


def _probe_process(probe: dict[str, Any], root_pid: int) -> tuple[bool, dict[str, Any]]:
    allowed = descendant_pids(root_pid)
    found: list[int] = []
    try:
        import psutil  # type: ignore
        for pid in sorted(allowed):
            try:
                process = psutil.Process(pid)
                name = process.name()
                exe = process.exe()
                if probe.get("name") and name != probe["name"] and Path(exe).name != probe["name"]:
                    continue
                expected = probe.get("exe_sha256")
                if expected and hash_executable(exe) != expected:
                    continue
                found.append(pid)
            except Exception:
                continue
    except Exception:
        identity = process_identity(root_pid)
        if identity and not probe.get("name") and not probe.get("exe_sha256"):
            found.append(root_pid)
    return bool(found), {"kind": "process", "owned": bool(found), "owner_pids": found}


def _probe_window(probe: dict[str, Any], root_pid: int) -> tuple[bool, dict[str, Any]]:
    if os.name != "nt":
        return False, {"kind": "window", "owned": False, "reason": "windows_required"}
    try:
        from pywinauto import Desktop  # type: ignore
        windows = Desktop(backend="uia").windows(title=probe["title"])
    except Exception:
        return False, {"kind": "window", "owned": False}
    allowed = descendant_pids(root_pid)
    found: list[int] = []
    for window in windows[:64]:
        try:
            pid = int(window.process_id())
        except Exception:
            continue
        if pid not in allowed:
            continue
        if probe.get("exe_sha256"):
            identity = process_identity(pid)
            if not identity or hash_executable(identity.get("exe", "")) != probe["exe_sha256"]:
                continue
        found.append(pid)
    return bool(found), {"kind": "window", "owned": bool(found), "owner_pids": found}


def probe_ready(probe: dict[str, Any], root_pid: int) -> tuple[bool, dict[str, Any]]:
    kind = probe.get("kind")
    if kind == "tcp":
        return _probe_tcp(probe, root_pid)
    if kind == "process":
        return _probe_process(probe, root_pid)
    if kind == "window":
        return _probe_window(probe, root_pid)
    return False, {"kind": "none", "owned": False}


def _capture_owned_processes(root_pid: int, evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Freeze creation identities for the launcher and the ready child tree."""
    pids = {root_pid}
    for value in evidence.get("owner_pids", ()) if isinstance(evidence, dict) else ():
        if type(value) is int and value > 0:
            pids.add(value)
    # Windows Python/command launchers can insert a short-lived shim whose
    # ancestry is not visible through the initial root query.  Walk each
    # observed owner for a few bounded passes so a ready grandchild is still
    # captured before the launcher exits.
    frontier = set(pids)
    for _ in range(3):
        discovered: set[int] = set()
        for pid in tuple(frontier):
            discovered.update(descendant_pids(pid))
        new = discovered - pids
        if not new:
            break
        pids.update(new)
        frontier = new
    records: list[dict[str, Any]] = []
    for pid in sorted(pids):
        identity = process_identity(pid)
        if identity is None or identity.get("created") is None:
            continue
        records.append({"pid": int(pid), "created": identity.get("created"), "boot_id": boot_id()})
    probe = None
    for value in evidence.get("owner_pids", ()) if isinstance(evidence, dict) else ():
        if type(value) is int and any(item["pid"] == value for item in records):
            probe = next(item for item in records if item["pid"] == value)
            break
    return records, probe


def _owned_process_records(row: dict[str, Any]) -> list[dict[str, Any]]:
    records = row.get("owned_processes")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)
                and type(item.get("pid")) is int and item.get("pid") > 0
                and item.get("created") is not None]
    pid = row.get("pid")
    if type(pid) is int and pid > 0 and row.get("process_started") is not None:
        return [{"pid": pid, "created": row.get("process_started"), "boot_id": row.get("boot_id")}]
    return []


def _owned_process_alive(record: dict[str, Any], *, current_boot: str) -> bool:
    return process_identity_matches(
        {"pid": record.get("pid"), "created": record.get("created")},
        boot=record.get("boot_id"), current_boot=current_boot,
    )


def _result_file(path: Path) -> tuple[Any, str | None]:
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > MAX_RESULT_BYTES:
            return None, "result_file_invalid"
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        canonical_json(value)
        return value, None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError, ValueError, ActionProtocolError):
        return None, "result_file_invalid"


def _terminate_owned(process: subprocess.Popen[Any] | None, row: dict[str, Any], journal: ActionJournal) -> bool:
    """Terminate the verified owned tree, including a detached ready child.

    A launcher may exit after handing a service/desktop process to the OS.  The
    journal therefore carries child creation identities captured at readiness.
    If any still-live PID no longer matches its immutable identity, fail closed
    before terminating anything.  There is deliberately no root-only fallback.
    """
    current_boot = boot_id()
    records = _owned_process_records(row)
    if not records:
        journal.append_log(row["run_id"], "supervisor", "stop_identity_unknown")
        journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
        return False
    # The finite action path may not have reached a readiness probe, but its
    # launcher can still own children.  Snapshot the live ancestry before
    # sending any terminate request.  Detached children are covered by the
    # persisted records captured at readiness below.
    root_pid = row.get("pid")
    if type(root_pid) is int and any(item.get("pid") == root_pid for item in records):
        root_identity = process_identity(root_pid)
        root_record = next(item for item in records if item.get("pid") == root_pid)
        if root_identity is not None:
            if root_identity.get("created") != root_record.get("created"):
                journal.append_log(row["run_id"], "supervisor", "stop_identity_unknown")
                journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
                return False
            frontier = {root_pid}
            for item in tuple(records):
                if _owned_process_alive(item, current_boot=current_boot):
                    frontier.add(int(item["pid"]))
            for _ in range(3):
                discovered: set[int] = set()
                for pid in tuple(frontier):
                    discovered.update(descendant_pids(pid))
                new = discovered - {int(item["pid"]) for item in records}
                if not new:
                    break
                for pid in sorted(new):
                    identity = process_identity(pid)
                    if identity is None or identity.get("created") is None:
                        journal.append_log(row["run_id"], "supervisor", "stop_child_identity_unknown")
                        journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
                        return False
                    records.append({"pid": int(pid), "created": identity.get("created"), "boot_id": current_boot})
                frontier = new
    alive_records: list[dict[str, Any]] = []
    for record in records:
        if record.get("boot_id") != current_boot:
            journal.append_log(row["run_id"], "supervisor", "stop_identity_unknown")
            journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
            return False
        current = process_identity(record.get("pid"))
        if current is None:
            continue
        if current.get("created") != record.get("created"):
            journal.append_log(row["run_id"], "supervisor", "stop_identity_unknown")
            journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
            return False
        alive_records.append(record)
    if not alive_records:
        return True
    try:
        import psutil  # type: ignore
        processes = []
        for record in alive_records:
            try:
                processes.append(psutil.Process(int(record["pid"])))
            except psutil.Error:
                continue
        if not processes:
            return True
        # Children first, then a launcher that is still alive.  The explicit
        # identity checks below keep this ordering from becoming a PID kill.
        def depth(item):
            value = 0
            seen = set()
            try:
                parent = item
                while parent.ppid() and parent.ppid() not in seen:
                    seen.add(parent.pid)
                    value += 1
                    parent = parent.parent()
                    if parent is None:
                        break
            except psutil.Error:
                pass
            return value
        for child in sorted(processes, key=depth, reverse=True):
            record = next((item for item in alive_records if item["pid"] == child.pid), None)
            if record and process_identity_matches(
                    {"pid": record["pid"], "created": record["created"]},
                    boot=current_boot, current_boot=current_boot):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
        _gone, alive = psutil.wait_procs(processes, timeout=1)
        for child in alive:
            record = next((item for item in alive_records if item["pid"] == child.pid), None)
            if record and process_identity_matches(
                    {"pid": record["pid"], "created": record["created"]},
                    boot=current_boot, current_boot=current_boot):
                try:
                    child.kill()
                except psutil.Error:
                    pass
        _gone, still_alive = psutil.wait_procs(alive, timeout=1)
        if still_alive:
            journal.append_log(row["run_id"], "supervisor", "stop_child_exit_unconfirmed")
            journal.mark_unknown_if_unobserved(row["run_id"], "child_exit_unconfirmed")
            return False
        return True
    except Exception:
        journal.append_log(row["run_id"], "supervisor", "stop_tree_unavailable")
        journal.mark_unknown_if_unobserved(row["run_id"], "child_exit_unconfirmed")
        return False


def run_supervisor(journal_path: str | os.PathLike[str], run_id: str) -> int:
    journal = ActionJournal(journal_path)
    row = journal.get(run_id)
    if row is None:
        return 2
    supervisor_identity = process_identity(os.getpid())
    supervisor_started = supervisor_identity.get("created") if supervisor_identity else None
    action_stack = None
    action_stream = None
    dependency_streams: list[tuple[Path, str, Any]] = []
    try:
        journal.update(run_id, status="starting", execution_status="starting",
                       supervisor_pid=os.getpid(), supervisor_started=supervisor_started,
                       supervisor_boot_id=boot_id())
        row = journal.get(run_id) or row
        if row["status"] in TERMINAL_STATES:
            return 0
        if row.get("stop_requested"):
            journal.update(run_id, status="stopped", execution_status="stopped")
            return 0
        path = Path(row["action_path"])
        try:
            _assert_catalog_path(path)
            action_stack = contextlib.ExitStack()
            action_stream = action_stack.enter_context(_locked_action_file(path))
            current_hash = _hash_open_action(action_stream)
            for dependency_path, dependency_hash in _dependency_entries(row):
                dependency_stream = action_stack.enter_context(_locked_action_file(dependency_path))
                if _hash_open_action(dependency_stream) != dependency_hash:
                    raise ActionProtocolError("saved_action_dependency_hash_mismatch")
                dependency_streams.append((dependency_path, dependency_hash, dependency_stream))
        except (ActionProtocolError, OSError) as exc:
            error = "action_dependency_hash_changed" if "dependency" in str(exc) else "action_path_unavailable"
            journal.update(run_id, status="unknown", execution_status="unknown", error=error)
            return 1
        if current_hash != row["action_sha256"]:
            journal.update(run_id, status="unknown", execution_status="unknown", error="action_hash_changed")
            return 1
        command = _command_for(path)
        env = _safe_child_env(row)
        kwargs = _hidden_popen_kwargs(
            desktop_visible=row["mode"] == "desktop" and path.suffix.lower() in {".exe", ".com"}
        )
        kwargs.update(cwd=str(path.parent), env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        journal.update(run_id, status="starting", execution_status="spawn_pending")
        process: subprocess.Popen[Any] | None = None
        try:
            process = subprocess.Popen(command, **kwargs)
        except Exception as exc:
            # A failed Popen has a known boundary: no child exists to replay.
            journal.update(run_id, status="failed", execution_status="failed",
                           exit_code=None, error="spawn_failed:" + type(exc).__name__)
            return 1
        child_identity = process_identity(process.pid)
        if child_identity is None or child_identity.get("created") is None:
            # A short finite action (especially a .bat launcher) may finish
            # before Windows allows a second process identity query.  The
            # spawn boundary is still known because Popen returned; consume
            # its exit/result once.  Service/desktop readiness remains
            # unconfirmed because no owned process identity was observed.
            code = process.poll()
            if code is not None and row["mode"] == "finite":
                try:
                    stdout, stderr = process.communicate(timeout=5)
                    for stream, raw in (("stdout", stdout), ("stderr", stderr)):
                        if raw:
                            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                            for line in text.splitlines():
                                journal.append_log(run_id, stream, line)
                    after_hash = _hash_open_action(action_stream)
                    result, result_error = _result_file(Path(row["output_path"]))
                    if after_hash != row["action_sha256"]:
                        journal.update(run_id, status="unknown", execution_status="unknown", error="action_hash_changed")
                        return 1
                    if any(_hash_open_action(stream) != digest for _path, digest, stream in dependency_streams):
                        journal.update(run_id, status="unknown", execution_status="unknown",
                                       error="action_dependency_hash_changed")
                        return 1
                    status = "succeeded" if code == 0 else "failed"
                    journal.update(run_id, status=status, execution_status=status, exit_code=code,
                                   ready=(code == 0), result_json=result, error=result_error)
                    return 0 if code == 0 else 1
                except Exception:
                    pass
            journal.update(run_id, status="unknown", execution_status="unknown", error="spawn_identity_unknown")
            return 1
        child_started = child_identity.get("created")
        child_boot = boot_id()
        journal.update(run_id, status="running", execution_status="running", pid=process.pid,
                       process_started=child_started, boot_id=child_boot,
                       owned_processes_json=[{"pid": process.pid, "created": child_started, "boot_id": child_boot}])
        streams = []
        for stream_name, handle in (("stdout", process.stdout), ("stderr", process.stderr)):
            if handle is not None:
                thread = threading.Thread(target=_capture_stream, args=(journal, run_id, stream_name, handle), daemon=True)
                thread.start()
                streams.append(thread)
        ready = False
        last_probe = 0.0
        launcher_finished = False
        while True:
            current = journal.get(run_id) or row
            if row["mode"] != "finite" and process.poll() is None:
                # Keep a durable ownership snapshot even before readiness. A
                # launcher can hand off to a child before its probe becomes
                # observable; stop must still be able to terminate that tree.
                observed, _probe = _capture_owned_processes(process.pid, {"owner_pids": []})
                prior = {item.get("pid"): item for item in _owned_process_records(current)}
                merged = {**prior, **{item.get("pid"): item for item in observed}}
                if merged and set(merged) != set(prior):
                    journal.update(run_id, owned_processes_json=list(merged.values()))
                    current = journal.get(run_id) or current
            if current.get("stop_requested"):
                if _terminate_owned(process, current, journal):
                    journal.update(run_id, status="stopped", execution_status="stopped",
                                   exit_code=process.poll(), ready=ready)
                    return 0
                return 1
            if launcher_finished:
                # A service/desktop launcher may have exited while its owned
                # child remains the actual runtime.  Keep the run live and
                # status-poll that immutable child identity until it exits.
                owned = _owned_process_records(current)
                if any(_owned_process_alive(item, current_boot=boot_id()) for item in owned):
                    time.sleep(0.2)
                    continue
                journal.update(run_id, status="unknown", execution_status="unknown",
                               exit_code=current.get("launcher_exit_code"), ready=True,
                               error="owned_process_exit_unconfirmed")
                return 1
            if row["mode"] != "finite" and not ready and time.monotonic() - last_probe >= 0.2:
                last_probe = time.monotonic()
                ready, evidence = probe_ready(row["probe"], process.pid)
                if ready:
                    owned, probe = _capture_owned_processes(process.pid, evidence)
                    journal.update(run_id, status="ready", execution_status="ready", ready=True,
                                   result_json={"probe": evidence}, owned_processes_json=owned,
                                   probe_pid=probe["pid"] if probe else None,
                                   probe_process_started=probe["created"] if probe else None,
                                   probe_boot_id=probe["boot_id"] if probe else None)
            code = process.poll()
            if code is not None:
                for thread in streams:
                    thread.join(timeout=2)
                # Verify the action file was not replaced while it ran.  No
                # result is marked successful after an unverified mutation.
                try:
                    after_hash = _hash_open_action(action_stream)
                except ActionProtocolError:
                    journal.update(run_id, status="unknown", execution_status="unknown", error="action_path_unavailable")
                    return 1
                if after_hash != row["action_sha256"]:
                    journal.update(run_id, status="unknown", execution_status="unknown", error="action_hash_changed")
                    return 1
                if any(_hash_open_action(stream) != digest for _path, digest, stream in dependency_streams):
                    journal.update(run_id, status="unknown", execution_status="unknown",
                                   error="action_dependency_hash_changed")
                    return 1
                result, result_error = _result_file(Path(row["output_path"]))
                current = journal.get(run_id) or row
                if current.get("stop_requested"):
                    journal.update(run_id, status="stopped", execution_status="stopped", exit_code=code,
                                   ready=ready, result_json=result, error=result_error)
                elif row["mode"] == "finite":
                    status = "succeeded" if code == 0 else "failed"
                    journal.update(run_id, status=status, execution_status=status, exit_code=code,
                                   ready=(code == 0), result_json=result, error=result_error)
                elif ready:
                    # Readiness belongs to the owned child, not to the
                    # launcher's exit code.  Preserve a running state when a
                    # launcher hands off to a live child; this also keeps stop
                    # able to terminate the child tree.
                    owned = _owned_process_records(current)
                    live_owned = [item for item in owned if _owned_process_alive(item, current_boot=boot_id())]
                    if live_owned:
                        monitor = live_owned[0]
                        journal.update(run_id, status="ready", execution_status="running",
                                       launcher_exit_code=code, pid=monitor["pid"],
                                       process_started=monitor["created"], boot_id=monitor["boot_id"],
                                       ready=True, result_json=result,
                                       error=result_error or ("launcher_exit:%s" % code if code else None))
                        launcher_finished = True
                        continue
                    journal.update(run_id, status="unknown", execution_status="unknown",
                                   launcher_exit_code=code, exit_code=code, ready=True,
                                   result_json=result, error=result_error or "owned_process_exit_unconfirmed")
                else:
                    owned = _owned_process_records(current)
                    live_owned = [item for item in owned if _owned_process_alive(item, current_boot=boot_id())]
                    if live_owned:
                        monitor = live_owned[0]
                        journal.update(run_id, status="running", execution_status="unconfirmed",
                                       launcher_exit_code=code, pid=monitor["pid"],
                                       process_started=monitor["created"], boot_id=monitor["boot_id"],
                                       ready=False, result_json=result,
                                       error=result_error or "readiness_unconfirmed")
                        launcher_finished = True
                        continue
                    # exit0 from a service/desktop launcher says only that the
                    # launcher exited; it does not prove readiness or causality.
                    journal.update(run_id, status="unknown", execution_status="unconfirmed",
                                   exit_code=code, ready=False, result_json=result,
                                   error=result_error or "readiness_unconfirmed")
                return 0 if code == 0 else 1
            time.sleep(0.2)
    except Exception as exc:
        try:
            journal.mark_unknown_if_unobserved(run_id, "supervisor_error:" + type(exc).__name__)
        except Exception:
            pass
        return 1
    finally:
        if action_stack is not None:
            try:
                action_stack.close()
            except Exception:
                pass
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AoiTalk saved action supervisor")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    return run_supervisor(args.journal, args.run_id)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())


__all__ = ["_command_for", "_hidden_popen_kwargs", "main", "probe_ready", "run_supervisor"]
