import os
import signal
import socket
import subprocess
import sys
import time
import json
import logging
from pathlib import Path
from urllib.request import urlopen

_IS_WINDOWS = sys.platform == "win32"
logger = logging.getLogger(__name__)

_child_processes: list[subprocess.Popen] = []
_LUCE_DFLASH_MODEL_IDS = {
    "luce-dflash",  # Lucebox server alias exposed by /v1/models.
    "qwen3.6-27b",
    "qwen3.6-27b-dflash",
}
_QWOPUS_MODEL_IDS = {
    "qwopus3.6-35b-a3b",
}


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


def _resolve_caddy_binary(project_root: Path) -> str:
    if _IS_WINDOWS:
        bundled = project_root / "caddy" / "caddy.exe"
        if bundled.exists():
            return str(bundled)
        return "caddy.exe"
    bundled = project_root / "caddy" / "caddy"
    if bundled.exists():
        return str(bundled)
    return "caddy"


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


def _config_get(config: object | None, key: str, default: object = None) -> object:
    if config is None:
        return default

    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass

    cursor: object = config
    for part in key.split("."):
        if isinstance(cursor, dict):
            if part not in cursor:
                return default
            cursor = cursor[part]
            continue

        value = getattr(cursor, part, None)
        if value is None:
            return default
        cursor = value
    return cursor


def _config_bool(config: object | None, key: str, default: bool = False) -> bool:
    value = _config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _should_start_luce_dflash(config: object | None) -> bool:
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return False

    model = str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip().lower()
    return model in _LUCE_DFLASH_MODEL_IDS


def _openai_compatible_local_model(config: object | None) -> str:
    return str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip().lower()


def _should_start_qwopus_llama_server(config: object | None) -> bool:
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return False

    if not _config_bool(config, "openai_compatible_local.qwopus.auto_start", True):
        return False

    return _openai_compatible_local_model(config) in _QWOPUS_MODEL_IDS


def _is_port_open(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _local_openai_model_ids(base_url: str = "http://127.0.0.1:8080/v1") -> set[str]:
    try:
        with urlopen(f"{base_url.rstrip('/')}/models", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return set()

    model_ids: set[str] = set()
    for item in payload.get("data", []):
        if isinstance(item, dict) and item.get("id"):
            model_ids.add(str(item["id"]).strip().lower())
    return model_ids


def _is_selected_local_model_running(model_ids: set[str], expected_ids: set[str]) -> bool:
    return bool(model_ids.intersection(expected_ids))


def ensure_openai_compatible_local_server(config: object | None) -> bool:
    """Start the selected bundled local OpenAI-compatible server if needed.

    Local LLM startup is best-effort. AoiTalk itself should keep starting even
    when the selected model is still loading or the helper process fails to
    launch; generation-time health handling reports that state to the user.
    """
    project_root = Path(__file__).resolve().parent.parent

    if _should_start_luce_dflash(config):
        if _is_selected_local_model_running(
            _local_openai_model_ids(), _LUCE_DFLASH_MODEL_IDS
        ):
            return False
        try:
            _start_luce_dflash(project_root)
        except Exception as exc:
            logger.warning(
                "Luce DFlash server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    if _should_start_qwopus_llama_server(config):
        if _is_selected_local_model_running(_local_openai_model_ids(), _QWOPUS_MODEL_IDS):
            return False
        try:
            _start_qwopus_llama_server(project_root)
        except Exception as exc:
            logger.warning(
                "Qwopus llama-server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    return False


def _start_luce_dflash(project_root: Path) -> None:
    script_path = project_root / "scripts" / "start_luce_dflash.ps1"
    if not script_path.exists():
        raise RuntimeError(f"Luce DFlash startup script not found: {script_path}")

    if _IS_WINDOWS:
        dflash_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _child_processes.append(dflash_proc)
    else:
        dflash_proc = subprocess.Popen(
            [
                "pwsh",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            start_new_session=True,
        )
        _child_processes.append(dflash_proc)

    print(f"Started Luce DFlash launcher (PID {dflash_proc.pid})")


def _start_qwopus_llama_server(project_root: Path) -> None:
    script_path = project_root / "scripts" / "start_qwopus_llama_server.ps1"
    if not script_path.exists():
        raise RuntimeError(f"Qwopus startup script not found: {script_path}")

    if _IS_WINDOWS:
        qwopus_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _child_processes.append(qwopus_proc)
    else:
        qwopus_proc = subprocess.Popen(
            [
                "pwsh",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            start_new_session=True,
        )
        _child_processes.append(qwopus_proc)

    print(f"Started Qwopus llama-server launcher (PID {qwopus_proc.pid})")


def _npm_command() -> str:
    return "npm.cmd" if _IS_WINDOWS else "npm"


def _next_bin_path(frontend_dir: Path) -> Path:
    bin_name = "next.cmd" if _IS_WINDOWS else "next"
    return frontend_dir / "node_modules" / ".bin" / bin_name


def _ensure_frontend_dependencies(project_root: Path, log_path: Path) -> None:
    """Repair missing npm executable links before starting Next.js."""
    frontend_dir = project_root / "frontend"
    if _next_bin_path(frontend_dir).exists():
        return

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(
            "Next.js executable link is missing; running npm ci before startup.\n"
        )
        log_file.flush()
        result = subprocess.run(
            [_npm_command(), "ci"],
            cwd=str(frontend_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Frontend dependencies are not ready and npm ci failed.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    if not _next_bin_path(frontend_dir).exists():
        raise RuntimeError(
            "npm ci completed, but Next.js executable link is still missing.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )


def _wait_for_port(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _read_env_value_from_dotenv(project_root: Path, key: str) -> str | None:
    """`.env` から単一キーの値を読む（dotenv 未ロードの起動経路向けフォールバック）。"""
    env_path = project_root / ".env"
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    return None


def _build_frontend_env(project_root: Path) -> dict[str, str]:
    """Next.js 子プロセスへ渡す環境変数を構築する。

    BFF の Python API proxy は `process.env.INTERNAL_API_KEY` を必須とする
    （frontend/src/lib/server/python-api-proxy.ts）。ここで明示的に注入し、
    フロント側での `.env` 手読みフォールバックを不要にする。
    """
    env = dict(os.environ)
    if not env.get("INTERNAL_API_KEY"):
        value = _read_env_value_from_dotenv(project_root, "INTERNAL_API_KEY")
        if value:
            env["INTERNAL_API_KEY"] = value
        else:
            logger.warning(
                "INTERNAL_API_KEY が環境変数にも .env にも見つかりません。"
                "Next.js から Python API への内部委譲が失敗します。"
            )
    return env


def start_services(config: object | None = None) -> None:
    """Start frontend, Caddy, and provider-specific local services."""
    project_root = Path(__file__).resolve().parent.parent

    _kill_existing_on_port(3000)
    _kill_existing_on_port(6002)
    _kill_existing_on_port(3002)

    should_start_dflash = _should_start_luce_dflash(config)
    should_start_qwopus = _should_start_qwopus_llama_server(config)
    local_server_launch_started = ensure_openai_compatible_local_server(config)

    frontend_log_path = _service_log_dir(project_root) / "frontend.log"
    _ensure_frontend_dependencies(project_root, frontend_log_path)
    frontend_env = _build_frontend_env(project_root)

    if _IS_WINDOWS:
        frontend_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-Command",
                (
                    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                    f"if (Test-Path '{frontend_log_path}') {{ Remove-Item '{frontend_log_path}' }}; "
                    f"npm run start -- -p 3002 -H 0.0.0.0 2>&1 | "
                    f"ForEach-Object {{ Write-Host $_; Add-Content -Path '{frontend_log_path}' -Value $_ -Encoding UTF8 }}"
                ),
            ],
            cwd=str(project_root / "frontend"),
            env=frontend_env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _child_processes.append(frontend_proc)

        if not _wait_for_port("127.0.0.1", 3002, timeout_seconds=45):
            raise RuntimeError(
                "Next.js frontend did not start listening on 127.0.0.1:3002.\n"
                f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
            )

        caddy_proc = subprocess.Popen(
            [_resolve_caddy_binary(project_root), "run", "--config", "Caddyfile"],
            cwd=str(project_root / "caddy"),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _child_processes.append(caddy_proc)
    else:
        frontend_cmd = (
            f"npm run start -- -p 3002 -H 0.0.0.0 2>&1 | "
            f"tee {str(frontend_log_path)!r}"
        )
        frontend_proc = subprocess.Popen(
            ["bash", "-c", frontend_cmd],
            cwd=str(project_root / "frontend"),
            env=frontend_env,
            start_new_session=True,
        )
        _child_processes.append(frontend_proc)

        if not _wait_for_port("127.0.0.1", 3002, timeout_seconds=45):
            raise RuntimeError(
                "Next.js frontend did not start listening on 127.0.0.1:3002.\n"
                f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
            )

        caddy_proc = subprocess.Popen(
            [_resolve_caddy_binary(project_root), "run", "--config", "Caddyfile"],
            cwd=str(project_root / "caddy"),
            start_new_session=True,
        )
        _child_processes.append(caddy_proc)

    if should_start_dflash and local_server_launch_started:
        print(
            f"Started Frontend (PID {frontend_proc.pid}) / "
            f"Caddy (PID {caddy_proc.pid}) / Luce DFlash starting"
        )
    elif should_start_qwopus and local_server_launch_started:
        print(
            f"Started Frontend (PID {frontend_proc.pid}) / "
            f"Caddy (PID {caddy_proc.pid}) / Qwopus llama-server starting"
        )
    else:
        print(f"Started Frontend (PID {frontend_proc.pid}) / Caddy (PID {caddy_proc.pid})")


def kill_services() -> None:
    """Stop child service processes started by start_services."""
    for proc in _child_processes:
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                )
            else:
                try:
                    pgid = os.getpgid(proc.pid)
                except ProcessLookupError:
                    continue

                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                except Exception:
                    pass

                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass
    _child_processes.clear()
