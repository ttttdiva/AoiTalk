"""プロセス追跡・ポート管理ユーティリティ。

`service_manager` パッケージの子プロセス追跡リストと、ポート占有プロセスの停止・
ポート待機などの低レベルユーティリティを提供する。挙動は分割前の
`service_manager.py` と同一（機械的移設）。

補足: `_terminate_process_tree` / `stop_openai_compatible_local_servers` /
`_resolve_caddy_binary` は、テストが `service_manager._IS_WINDOWS` を monkeypatch
した状態で本体が実行される（=ファサード名前空間の値を参照する必要がある）ため、
本モジュールではなくパッケージ `__init__.py`（ファサード）側に定義している。
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

_child_processes: list[subprocess.Popen] = []
_openai_compatible_local_processes: list[subprocess.Popen] = []


def _track_child_process(
    proc: subprocess.Popen,
    *,
    openai_compatible_local: bool = False,
) -> None:
    _child_processes.append(proc)
    if openai_compatible_local:
        _openai_compatible_local_processes.append(proc)


def _remove_tracked_process(proc: subprocess.Popen) -> None:
    try:
        _child_processes.remove(proc)
    except ValueError:
        pass
    try:
        _openai_compatible_local_processes.remove(proc)
    except ValueError:
        pass


def _extract_port_from_netstat_address(address: str) -> int | None:
    """Extract the local port from a Windows netstat address column."""
    if not address:
        return None

    if address.startswith("["):
        _, sep, port_text = address.rpartition("]:")
    else:
        _, sep, port_text = address.rpartition(":")

    if not sep or not port_text.isdigit():
        return None
    return int(port_text)


def _listening_pid_for_netstat_line(line: str, port: int) -> str | None:
    """Return the PID when a Windows netstat TCP LISTENING line matches port."""
    parts = line.split()
    if len(parts) < 5 or parts[0].upper() != "TCP":
        return None

    local_address = parts[1]
    state = parts[3].upper()
    pid = parts[4]
    if state != "LISTENING":
        return None
    if _extract_port_from_netstat_address(local_address) != port:
        return None
    return pid if pid.isdigit() and int(pid) > 0 else None


def _kill_existing_on_port(port: int) -> None:
    """Stop existing listener on the given port before starting services."""
    if _IS_WINDOWS:
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            seen_pids = set()
            for line in result.stdout.splitlines():
                pid = _listening_pid_for_netstat_line(line, port)
                if pid and pid not in seen_pids:
                    seen_pids.add(pid)
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                    )
                    print(f"Stopped existing process on port {port} (PID {pid})")
        except Exception:
            pass
        return

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip().isdigit()]
        for pid in pids:
            try:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                print(f"Stopped existing process on port {port} (PID {pid})")
            except Exception:
                pass
        if pids:
            return
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _read_log_tail(path: Path, max_chars: int = 3000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "(log file not found)"
    except Exception as exc:
        return f"(failed to read log: {exc})"
    return text[-max_chars:]


def _service_log_dir(project_root: Path) -> Path:
    log_dir = project_root / "logs" / "services"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _is_port_open(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False
