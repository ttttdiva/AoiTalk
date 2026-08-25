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
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"ポート {port} の使用状況を確認できませんでした"
                + (f": {detail}" if detail else "")
            )

        pids = {
            pid
            for line in result.stdout.splitlines()
            if (pid := _listening_pid_for_netstat_line(line, port))
        }
        kill_errors: list[str] = []
        for pid in sorted(pids, key=int):
            kill_result = subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if kill_result.returncode != 0:
                detail = (kill_result.stderr or kill_result.stdout or "").strip()
                kill_errors.append(f"PID {pid}: {detail or 'taskkill failed'}")

        if kill_errors:
            recheck = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if recheck.returncode != 0:
                detail = (recheck.stderr or recheck.stdout or "").strip()
                raise RuntimeError(
                    f"ポート {port} の停止結果を確認できませんでした"
                    + (f": {detail}" if detail else "")
                )
            remaining_pids = {
                pid
                for line in recheck.stdout.splitlines()
                if (pid := _listening_pid_for_netstat_line(line, port))
            }
            failed_pids = {
                error.split(":", 1)[0].removeprefix("PID ")
                for error in kill_errors
            }
            if failed_pids & remaining_pids:
                raise RuntimeError(
                    f"ポート {port} を解放できませんでした"
                    f" ({'; '.join(kill_errors)})"
                )

        if not _wait_for_port_closed(
            "127.0.0.1",
            port,
            timeout_seconds=5,
        ):
            detail = "; ".join(kill_errors)
            raise RuntimeError(
                f"ポート {port} を解放できませんでした"
                + (f" ({detail})" if detail else "")
            )

        for pid in sorted(pids, key=int):
            print(f"Stopped existing process on port {port} (PID {pid})")
        return

    kill_errors: list[str] = []
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
                kill_result = subprocess.run(
                    ["kill", "-9", pid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if kill_result.returncode != 0:
                    detail = (kill_result.stderr or kill_result.stdout or "").strip()
                    kill_errors.append(f"PID {pid}: {detail or 'kill failed'}")
            except Exception as exc:
                kill_errors.append(f"PID {pid}: {exc}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        kill_errors.append(f"lsof: {exc}")

    try:
        fuser_result = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if fuser_result.returncode not in (0, 1):
            detail = (fuser_result.stderr or fuser_result.stdout or "").strip()
            kill_errors.append(f"fuser: {detail or 'fuser failed'}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        kill_errors.append(f"fuser: {exc}")

    if not _wait_for_port_closed("127.0.0.1", port, timeout_seconds=5):
        detail = "; ".join(kill_errors)
        raise RuntimeError(
            f"ポート {port} を解放できませんでした"
            + (f" ({detail})" if detail else "")
        )


def _read_log_tail(path: Path, max_chars: int = 3000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "(log file not found)"
    except Exception as exc:
        return f"(failed to read log: {exc})"
    return text[-max_chars:]


def _web_log_dir(project_root: Path) -> Path:
    """HTTP 境界ログ（frontend / Caddy）用ディレクトリ。"""
    log_dir = project_root / "logs" / "web"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _models_log_dir(project_root: Path) -> Path:
    """ローカル LLM サーバーログ用ディレクトリ。"""
    log_dir = project_root / "logs" / "models"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _service_log_dir(project_root: Path) -> Path:
    """後方互換: 旧 logs/services は web へ誘導する。"""
    return _web_log_dir(project_root)


def _is_port_open(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    if timeout_seconds <= 0:
        return False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with socket.create_connection(
                (host, port),
                timeout=min(1.0, remaining),
            ):
                return True
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))


def _wait_for_port_closed(host: str, port: int, timeout_seconds: float) -> bool:
    """Wait until no listener accepts connections on the target port."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_port_open(host, port, timeout_seconds=0.25):
            return True
        time.sleep(0.1)
    return not _is_port_open(host, port, timeout_seconds=0.25)


def _wait_for_process_port(
    proc: subprocess.Popen,
    host: str,
    port: int,
    timeout_seconds: float,
) -> bool:
    """Wait for a stable listener while ensuring its launcher stays alive."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if _is_port_open(host, port, timeout_seconds=0.25):
            stable_until = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < stable_until:
                if proc.poll() is not None:
                    return False
                if not _is_port_open(host, port, timeout_seconds=0.25):
                    break
                time.sleep(0.1)
            else:
                return True
        time.sleep(0.1)
    return False
