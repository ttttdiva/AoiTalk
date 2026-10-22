"""Independent supervisor for immutable server App Action runtimes.

The supervisor is a separate process from FastAPI and the API worker.  It
reads only the server-owned SQLite journal, launches through
``app_job_isolation`` and communicates progress through that journal.  A
restart therefore has a status-only recovery path and never receives enough
request data to reconstruct a new command.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .app_action_server_journal import (
    ServerActionJournal,
    ServerActionJournalError,
    TERMINAL_STATES,
    runtime_tree_sha256,
)
from .app_job_isolation import AppJobIsolationError, spawn_isolated_process, stop_owned_job


MAX_RESULT_BYTES = 64 * 1024


def _hidden_popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "shell": False,
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startup
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def spawn_supervisor_process(
    journal_path: str | os.PathLike[str],
    run_id: str,
    *,
    python_executable: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.Popen[Any]:
    """Spawn the independent supervisor with no visible Windows console."""

    executable = python_executable or sys.executable
    repository_root = Path(cwd).resolve() if cwd is not None else Path(__file__).resolve().parents[2]
    command = [
        executable,
        "-m",
        "src.services.app_action_server_supervisor",
        "--journal",
        str(Path(journal_path).resolve()),
        "--run-id",
        str(run_id),
    ]
    return subprocess.Popen(command, cwd=str(repository_root), **_hidden_popen_kwargs())


def _safe_child_env(row: Mapping[str, Any]) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "LANG",
        "LC_ALL",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    runtime_root = str(row["runtime_root"])
    env.update(
        {
            "AOITALK_APP_RUN_ID": str(row["run_id"]),
            "AOITALK_APP_ACTION_REF": str(row["action_ref"]),
            "AOITALK_APP_RUNTIME_ROOT": runtime_root,
            "AOITALK_APP_OUTPUT_ROOT": str(Path(runtime_root).parent / "outputs"),
            "AOITALK_ACTION_OUTPUT_ROOT": str(Path(runtime_root).parent / "outputs"),
            "AOITALK_APP_OUTPUT_MANIFEST_FILE": str(row["output_manifest_path"]),
            "AOITALK_ACTION_OUTPUT_MANIFEST_FILE": str(row["output_manifest_path"]),
            # The file protocol is the source of truth.  Keep the legacy
            # inline value only for small inputs so Windows' environment block
            # limit cannot turn a valid run into an unbounded env expansion.
            "AOITALK_ACTION_INPUT_FILE": str(row["input_path"]),
            "AOITALK_ACTION_RESULT_FILE": str(row["output_path"]),
            "AOITALK_APP_INPUT_FILE": str(row["input_path"]),
            "AOITALK_APP_OUTPUT_FILE": str(row["output_path"]),
            "AOITALK_APP_RESULT_FILE": str(row["output_path"]),
            "AOITALK_APP_RUNNER": "aoitalk-isolated-server-action",
            "AOITALK_ACTION_FILE_PROTOCOL_VERSION": "1",
            "AOITALK_ACTION_FILE_ROOT": str(Path(row["input_path"]).parent / "inputs"),
            "AOITALK_ACTION_INPUT_FILES": json.dumps(
                row.get("input_files") or [], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    legacy_input = json.dumps(row.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
    if len(legacy_input.encode("utf-8")) <= 8 * 1024:
        env["AOITALK_APP_INPUT_JSON"] = legacy_input
    # Resolve the runtime interpreter before any inherited host path.  This is
    # the same repository convention used by the existing App Job runner.
    runtime_bins = [Path(sys.executable).resolve().parent]
    repo_root = Path(__file__).resolve().parents[2]
    project_venv_bin = repo_root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    if project_venv_bin.is_dir():
        runtime_bins.insert(0, project_venv_bin)
    env["PATH"] = os.pathsep.join([str(item) for item in runtime_bins] + ([env["PATH"]] if env.get("PATH") else []))
    return env


def _read_result(path: Path) -> tuple[Any, str | None]:
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > MAX_RESULT_BYTES:
            return None, "result_file_invalid"
        value = json.loads(path.read_text(encoding="utf-8"))
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return value, None
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return None, "result_file_invalid"


def _read_output_manifest(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > MAX_RESULT_BYTES:
            return [], "output_manifest_invalid"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or len(value) > 256:
            return [], "output_manifest_invalid"
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                return [], "output_manifest_invalid"
            normalized.append(dict(item))
        return normalized, None
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeError, ValueError, TypeError):
        return [], "output_manifest_invalid"


def run_server_action_supervisor(
    journal_path: str | os.PathLike[str],
    run_id: str,
    *,
    poll_seconds: float = 0.2,
) -> int:
    journal = ServerActionJournal(journal_path)
    try:
        row = journal.get(run_id)
        if row is None:
            return 2
        if row.get("status") in TERMINAL_STATES:
            return 0
        identity = None
        try:
            from pc_bridge.action_identity import boot_id, process_identity

            identity = process_identity(os.getpid())
            supervisor_started = identity.get("created") if identity else None
            supervisor_boot = boot_id()
        except Exception:
            supervisor_started = None
            supervisor_boot = f"process-{os.getpid()}"
        journal.update(
            run_id,
            status="starting",
            execution_status="starting",
            supervisor_pid=os.getpid(),
            supervisor_started=supervisor_started,
            supervisor_boot_id=supervisor_boot,
        )
        row = journal.get(run_id) or row
        if row.get("stop_requested"):
            journal.update(run_id, status="cancelled", execution_status="cancelled")
            return 0
        runtime_root = Path(str(row["runtime_root"])).resolve()
        try:
            if runtime_tree_sha256(runtime_root) != str(row["runtime_tree_sha256"]):
                journal.update(run_id, status="unknown", execution_status="unknown", error="runtime_hash_changed")
                return 1
        except (OSError, ServerActionJournalError):
            journal.update(run_id, status="unknown", execution_status="unknown", error="runtime_unavailable")
            return 1
        command = row.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
            journal.update(run_id, status="unknown", execution_status="unknown", error="command_invalid")
            return 1
        log_path = Path(str(row["log_path"])).resolve()
        output_path = Path(str(row["output_path"])).resolve()
        output_manifest_path = Path(str(row["output_manifest_path"])).resolve()
        input_path = Path(str(row["input_path"])).resolve()
        run_root = runtime_root.parent
        if any(path.parent != run_root for path in (input_path, output_path, output_manifest_path, log_path)):
            journal.update(run_id, status="unknown", execution_status="unknown", error="run_path_scope_invalid")
            return 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        if input_path.is_symlink():
            journal.update(run_id, status="unknown", execution_status="unknown", error="input_path_link_invalid")
            return 1
        input_payload = json.dumps(
            row.get("input") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        try:
            with input_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(input_payload)
        except FileExistsError:
            try:
                if input_path.read_text(encoding="utf-8") != input_payload:
                    journal.update(run_id, status="unknown", execution_status="unknown", error="input_path_changed")
                    return 1
            except (OSError, UnicodeError):
                journal.update(run_id, status="unknown", execution_status="unknown", error="input_path_unavailable")
                return 1
        file_root = input_path.parent / "inputs"
        file_root.mkdir(parents=True, exist_ok=True)
        (input_path.parent / "outputs").mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8", newline="")
        process = None
        try:
            try:
                process = spawn_isolated_process(
                    job_id=run_id,
                    argv=list(command),
                    cwd=runtime_root,
                    env=_safe_child_env(row),
                    log_file=log_handle,
                    config=row.get("isolation") if isinstance(row.get("isolation"), Mapping) else {},
                )
            except (AppJobIsolationError, OSError, ValueError) as exc:
                journal.update(
                    run_id,
                    status="failed",
                    execution_status="failed",
                    error="spawn_failed:" + type(exc).__name__,
                )
                return 1
            journal.update(run_id, status="running", execution_status="running", pid=process.pid)
            if process.stdin is not None:
                try:
                    process.stdin.write(
                        json.dumps(row.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
                    )
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            while True:
                current = journal.get(run_id) or row
                if current.get("stop_requested"):
                    stopped = stop_owned_job(run_id)
                    try:
                        code = process.wait(timeout=15)
                    except Exception:
                        code = process.poll()
                    if stopped or code is not None:
                        journal.update(run_id, status="cancelled", execution_status="cancelled", exit_code=code)
                        return 0
                    journal.mark_unknown_if_active(run_id, "stop_unconfirmed")
                    return 1
                code = process.poll()
                if code is not None:
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        pass
                    try:
                        if runtime_tree_sha256(runtime_root) != str(row["runtime_tree_sha256"]):
                            journal.update(run_id, status="unknown", execution_status="unknown", error="runtime_hash_changed", exit_code=code)
                            return 1
                    except (OSError, ServerActionJournalError):
                        journal.update(run_id, status="unknown", execution_status="unknown", error="runtime_unavailable", exit_code=code)
                        return 1
                    result, result_error = _read_result(output_path)
                    output_files, output_manifest_error = _read_output_manifest(output_manifest_path)
                    result_error = result_error or output_manifest_error
                    if code == 0 and result_error is None:
                        journal.update(
                            run_id,
                            status="succeeded",
                            execution_status="succeeded",
                            exit_code=code,
                            ready=True,
                            result_json=result,
                            output_files_json=output_files,
                            error=result_error,
                        )
                    else:
                        journal.update(
                            run_id,
                            status="failed",
                            execution_status="failed",
                            exit_code=code,
                            result_json=result,
                            output_files_json=output_files,
                            error=result_error or "process_failed",
                        )
                    return 0 if code == 0 else 1
                time.sleep(max(0.05, float(poll_seconds)))
        finally:
            try:
                log_handle.close()
            except OSError:
                pass
    except BaseException as exc:
        try:
            journal.mark_unknown_if_active(run_id, "supervisor_error:" + type(exc).__name__)
        except Exception:
            pass
        return 1
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AoiTalk server App Action supervisor")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    return run_server_action_supervisor(args.journal, args.run_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# Keep the short name parallel to ``pc_bridge.action_supervisor`` for tests and
# operational tooling; both names refer to the same status-only supervisor.
run_supervisor = run_server_action_supervisor


__all__ = [
    "_hidden_popen_kwargs",
    "main",
    "run_supervisor",
    "run_server_action_supervisor",
    "spawn_supervisor_process",
]
