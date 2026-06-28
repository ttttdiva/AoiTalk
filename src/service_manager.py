import importlib.util
import hashlib
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
import json
import logging
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from src.llm.openai_compatible_local_profiles import (
    EXO_BASE_URL,
    MLX_LM_BASE_URL,
    is_macos,
    local_server_profile_for_model,
    normalize_openai_compatible_base_url,
    openai_compatible_local_base_url,
)

_IS_WINDOWS = sys.platform == "win32"
logger = logging.getLogger(__name__)

_child_processes: list[subprocess.Popen] = []
_openai_compatible_local_processes: list[subprocess.Popen] = []
_LUCE_DFLASH_MODEL_IDS = {
    "luce-dflash",  # Lucebox server alias exposed by /v1/models.
    "qwen3.6-27b",
    "qwen3.6-27b-dflash",
}
_QWOPUS_MODEL_IDS = {
    "qwopus3.6-35b-a3b",
}
_FRONTEND_BUILD_FINGERPRINT_VERSION = 1
_FRONTEND_BUILD_FINGERPRINT_REL_PATH = Path(".next") / "aoitalk-build-fingerprint.json"
_FRONTEND_BUILD_EXCLUDED_DIR_NAMES = {
    ".git",
    ".next",
    ".turbo",
    "coverage",
    "node_modules",
    "playwright-report",
    "test-results",
}
_FRONTEND_BUILD_EXCLUDED_FILE_NAMES = {
    ".DS_Store",
}
_FRONTEND_BUILD_INPUT_SUFFIXES = {
    ".cjs",
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".md",
    ".mdx",
    ".mjs",
    ".png",
    ".scss",
    ".svg",
    ".ts",
    ".tsx",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
    ".yaml",
    ".yml",
}
_FRONTEND_STATIC_REF_PATTERN = re.compile(
    r"(?:/_next/|_next/)?static/[^\s\"'`<>)\]}]+"
)
_DEFAULT_AI_ROOT = Path(Path(__file__).resolve().anchor or ".") / "AI"
_DEFAULT_QWOPUS_MODEL_PATH = (
    _DEFAULT_AI_ROOT
    / "models"
    / "qwopus"
    / "models"
    / "Qwopus3.6-35B-A3B-v1-Q4_K_M.gguf"
)
_DEFAULT_LUCE_DFLASH_ROOT = _DEFAULT_AI_ROOT / "lucebox-hub" / "dflash"
_DEFAULT_LUCE_DFLASH_TARGET_MODEL = (
    _DEFAULT_AI_ROOT / "models" / "luce-dflash" / "models" / "Qwen3.6-27B-Q4_K_M.gguf"
)
_DEFAULT_LUCE_DFLASH_DRAFT_MODEL = (
    _DEFAULT_AI_ROOT
    / "models"
    / "luce-dflash"
    / "models"
    / "draft"
    / "dflash-draft-3.6-q8_0.gguf"
)


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


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass

    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
        except Exception:
            pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    except Exception:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
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


def stop_openai_compatible_local_servers() -> int:
    """Stop local OpenAI-compatible server processes started by AoiTalk."""
    processes = list(_openai_compatible_local_processes)
    for proc in processes:
        _terminate_process_tree(proc)
        _remove_tracked_process(proc)
    return len(processes)


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


def _openai_compatible_local_model_id(config: object | None) -> str:
    return str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()


def _openai_compatible_local_model(config: object | None) -> str:
    return _openai_compatible_local_model_id(config).lower()


def _should_start_qwopus_llama_server(config: object | None) -> bool:
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return False

    if not _config_bool(config, "openai_compatible_local.qwopus.auto_start", True):
        return False

    return _openai_compatible_local_model(config) in _QWOPUS_MODEL_IDS


def _selected_openai_compatible_profile(config: object | None) -> dict[str, str] | None:
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return None
    return local_server_profile_for_model(_openai_compatible_local_model_id(config))


def _should_start_exo_server(config: object | None) -> bool:
    profile = _selected_openai_compatible_profile(config)
    if not profile or profile.get("server") != "exo":
        return False
    if not _config_bool(config, "openai_compatible_local.exo.auto_start", True):
        return False
    return is_macos()


def _should_start_mlx_lm_server(config: object | None) -> bool:
    profile = _selected_openai_compatible_profile(config)
    if not profile or profile.get("server") != "mlx-lm":
        return False
    if not _config_bool(config, "openai_compatible_local.mlx_lm.auto_start", True):
        return False
    return is_macos()


def _openai_compatible_local_base_url(config: object | None) -> str:
    return openai_compatible_local_base_url(
        config,
        model=_openai_compatible_local_model_id(config),
    )


def _env_or_default(env_name: str, default: str | Path) -> str:
    return str(os.getenv(env_name) or default)


def _parse_positive_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _require_existing_path(path: str | Path, label: str, setting_hint: str) -> str:
    value = str(path)
    if not value or not Path(value).exists():
        raise RuntimeError(f"{label} not found: {value}. Set {setting_hint}.")
    return value


def _require_existing_file(path: str | Path, label: str, setting_hint: str) -> str:
    value = str(path)
    if not value or not Path(value).is_file():
        raise RuntimeError(f"{label} not found: {value}. Set {setting_hint}.")
    return value


def _resolve_optional_executable(value: str, label: str, setting_hint: str) -> str:
    if Path(value).exists():
        return value
    if shutil.which(value):
        return value
    raise RuntimeError(f"{label} not found: {value}. Set {setting_hint}.")


def _qwopus_launch_args(config: object | None = None) -> list[str]:
    model_path = _require_existing_file(
        _env_or_default("QWOPUS_MODEL_PATH", _DEFAULT_QWOPUS_MODEL_PATH),
        "Qwopus GGUF",
        "QWOPUS_MODEL_PATH",
    )
    args = ["-ModelPath", model_path]
    llama_server_exe = os.getenv("LLAMA_SERVER_EXE")
    if llama_server_exe:
        args.extend(
            [
                "-LlamaServerExe",
                _resolve_optional_executable(
                    llama_server_exe,
                    "llama-server executable",
                    "LLAMA_SERVER_EXE",
                ),
            ]
        )
    return args


def _luce_dflash_launch_args(project_root: Path) -> list[str]:
    repo_root = Path(
        _env_or_default("LUCE_DFLASH_ROOT", _DEFAULT_LUCE_DFLASH_ROOT)
    )
    target_model = _require_existing_file(
        _env_or_default("LUCE_DFLASH_TARGET_MODEL", _DEFAULT_LUCE_DFLASH_TARGET_MODEL),
        "Luce DFlash target GGUF",
        "LUCE_DFLASH_TARGET_MODEL",
    )
    draft_model = _require_existing_path(
        _env_or_default("LUCE_DFLASH_DRAFT_MODEL", _DEFAULT_LUCE_DFLASH_DRAFT_MODEL),
        "Luce DFlash draft model path",
        "LUCE_DFLASH_DRAFT_MODEL",
    )
    project_venv_python = project_root / "venv" / "Scripts" / "python.exe"
    python_candidate = (
        project_venv_python if project_venv_python.is_file() else sys.executable
    )
    python_exe = _require_existing_file(
        python_candidate,
        "Python",
        "a project venv or the current Python runtime",
    )
    cuda_root = _env_or_default("CUDA_PATH", "")

    _require_existing_path(repo_root, "Luce DFlash root", "LUCE_DFLASH_ROOT")
    _require_existing_file(
        repo_root / "scripts" / "server.py",
        "Luce DFlash server.py",
        "LUCE_DFLASH_ROOT",
    )
    _require_existing_file(
        repo_root / "build" / "test_dflash.exe",
        "test_dflash.exe",
        "LUCE_DFLASH_ROOT",
    )
    _require_existing_file(
        Path(cuda_root) / "bin" / "x64" / "cublas64_13.dll",
        "CUDA 13.2 cublas DLL",
        "CUDA_PATH",
    )

    return [
        "-RepoRoot",
        str(repo_root),
        "-TargetModel",
        target_model,
        "-DraftModel",
        draft_model,
        "-PythonExe",
        python_exe,
        "-CudaRoot",
        cuda_root,
    ]


def _split_launch_command(command: str, label: str) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"{label} command is invalid: {exc}") from exc
    if not args:
        raise RuntimeError(f"{label} command is empty.")
    return args


def _configured_command(config: object | None, env_name: str, config_key: str) -> str:
    return str(os.getenv(env_name) or _config_get(config, config_key, "") or "").strip()


def _configured_path(config: object | None, env_name: str, config_key: str) -> str:
    return str(os.getenv(env_name) or _config_get(config, config_key, "") or "").strip()


def _base_url_host_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(normalize_openai_compatible_base_url(base_url))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def _is_openai_compatible_local_server_running(base_url: str) -> bool:
    normalized_url = normalize_openai_compatible_base_url(base_url)
    if _local_openai_model_ids(normalized_url):
        return True
    host, port = _base_url_host_port(normalized_url)
    return _is_port_open(host, port, timeout_seconds=0.5)


def _profile_base_url_for_model(
    config: object | None,
    model: str | None = None,
    base_url: str | None = None,
    *,
    prefer_profile: bool = False,
) -> str:
    if base_url:
        return normalize_openai_compatible_base_url(base_url)
    selected_model = model or _openai_compatible_local_model_id(config)
    profile = local_server_profile_for_model(selected_model) if prefer_profile else None
    if profile:
        return normalize_openai_compatible_base_url(profile["base_url"])
    return openai_compatible_local_base_url(config, model=selected_model)


def _exo_launch_plan(
    config: object | None,
    project_root: Path,
) -> tuple[list[str], Path]:
    command = _configured_command(
        config,
        "EXO_COMMAND",
        "openai_compatible_local.exo.command",
    )
    root_value = _configured_path(
        config,
        "EXO_ROOT",
        "openai_compatible_local.exo.root",
    )
    cwd = Path(root_value).expanduser() if root_value else project_root
    if command:
        return _split_launch_command(command, "exo"), cwd

    exo_exe = shutil.which("exo")
    if exo_exe:
        return [exo_exe], cwd

    uv_exe = shutil.which("uv")
    if root_value and uv_exe:
        return [uv_exe, "run", "exo"], cwd

    raise RuntimeError(
        f"exo launcher not found for {EXO_BASE_URL}. Install exo on PATH or set "
        "EXO_COMMAND / openai_compatible_local.exo.command."
    )


def _mlx_lm_launch_plan(
    config: object | None,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[list[str], Path]:
    command = _configured_command(
        config,
        "MLX_LM_COMMAND",
        "openai_compatible_local.mlx_lm.command",
    )
    project_root = Path(__file__).resolve().parent.parent
    if command:
        return _split_launch_command(command, "MLX LM"), project_root

    model_id = str(model or _openai_compatible_local_model_id(config)).strip()
    if not model_id:
        raise RuntimeError("MLX LM model is not configured.")

    resolved_base_url = _profile_base_url_for_model(config, model_id, base_url)
    host, port = _base_url_host_port(resolved_base_url)
    mlx_lm_server = shutil.which("mlx_lm.server")
    if mlx_lm_server:
        args = [mlx_lm_server]
    elif importlib.util.find_spec("mlx_lm") is not None:
        args = [sys.executable, "-m", "mlx_lm.server"]
    else:
        raise RuntimeError(
            f"MLX LM server not found for {MLX_LM_BASE_URL}. Install mlx-lm "
            "or set MLX_LM_COMMAND / openai_compatible_local.mlx_lm.command."
        )

    args.extend(["--model", model_id, "--port", str(port)])
    if host not in {"127.0.0.1", "localhost", "::1"}:
        args.extend(["--host", host])
    return args, project_root


def validate_openai_compatible_local_launch_selection(
    config: object | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    selected_provider = str(
        provider or _config_get(config, "llm_provider", "") or ""
    ).strip().lower()
    if selected_provider != "openai_compatible_local":
        return

    selected_model_id = str(model or _openai_compatible_local_model_id(config)).strip()
    selected_model = selected_model_id.lower()
    project_root = Path(__file__).resolve().parent.parent
    if selected_model in _LUCE_DFLASH_MODEL_IDS:
        _luce_dflash_launch_args(project_root)
    elif selected_model in _QWOPUS_MODEL_IDS and _config_bool(
        config, "openai_compatible_local.qwopus.auto_start", True
    ):
        _qwopus_launch_args(config)
    else:
        profile = local_server_profile_for_model(selected_model_id)
        if not profile:
            return
        resolved_base_url = _profile_base_url_for_model(
            config,
            selected_model_id,
            base_url,
            prefer_profile=True,
        )
        if _is_openai_compatible_local_server_running(resolved_base_url):
            return
        if profile.get("server") == "exo" and _config_bool(
            config,
            "openai_compatible_local.exo.auto_start",
            True,
        ):
            _exo_launch_plan(config, project_root)
        elif profile.get("server") == "mlx-lm" and _config_bool(
            config,
            "openai_compatible_local.mlx_lm.auto_start",
            True,
        ):
            _mlx_lm_launch_plan(
                config,
                model=selected_model_id,
                base_url=resolved_base_url,
            )


def _is_port_open(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _local_openai_model_ids(base_url: str = "http://127.0.0.1:8080/v1") -> set[str]:
    try:
        normalized_base_url = normalize_openai_compatible_base_url(base_url)
        with urlopen(f"{normalized_base_url}/models", timeout=2.0) as response:
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


def ensure_openai_compatible_local_server(
    config: object | None,
    *,
    raise_on_launch_error: bool = False,
) -> bool:
    """Start the selected bundled local OpenAI-compatible server if needed.

    Local LLM startup is best-effort. AoiTalk itself should keep starting even
    when the selected model is still loading or the helper process fails to
    launch; generation-time health handling reports that state to the user.
    """
    project_root = Path(__file__).resolve().parent.parent
    base_url = _openai_compatible_local_base_url(config)

    if _should_start_luce_dflash(config):
        if _is_selected_local_model_running(
            _local_openai_model_ids(base_url), _LUCE_DFLASH_MODEL_IDS
        ):
            return False
        try:
            _start_luce_dflash(project_root)
        except Exception as exc:
            if raise_on_launch_error:
                raise
            logger.warning(
                "Luce DFlash server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    if _should_start_qwopus_llama_server(config):
        if _is_selected_local_model_running(
            _local_openai_model_ids(base_url), _QWOPUS_MODEL_IDS
        ):
            return False
        try:
            _start_qwopus_llama_server(project_root, config=config)
        except Exception as exc:
            if raise_on_launch_error:
                raise
            logger.warning(
                "Qwopus llama-server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    if _should_start_exo_server(config):
        resolved_base_url = _profile_base_url_for_model(config)
        if _is_openai_compatible_local_server_running(resolved_base_url):
            return False
        try:
            _start_exo_server(project_root, config=config)
        except Exception as exc:
            if raise_on_launch_error:
                raise
            logger.warning(
                "exo server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    if _should_start_mlx_lm_server(config):
        resolved_base_url = _profile_base_url_for_model(config)
        if _is_openai_compatible_local_server_running(resolved_base_url):
            return False
        try:
            _start_mlx_lm_server(project_root, config=config)
        except Exception as exc:
            if raise_on_launch_error:
                raise
            logger.warning(
                "MLX LM server launch failed; continuing AoiTalk startup: %s",
                exc,
            )
            return False
        return True

    return False


def _start_luce_dflash(project_root: Path) -> None:
    script_path = project_root / "scripts" / "start_luce_dflash.ps1"
    if not script_path.exists():
        raise RuntimeError(f"Luce DFlash startup script not found: {script_path}")
    launch_args = _luce_dflash_launch_args(project_root)

    if _IS_WINDOWS:
        dflash_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *launch_args,
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _track_child_process(dflash_proc, openai_compatible_local=True)
    else:
        dflash_proc = subprocess.Popen(
            [
                "pwsh",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *launch_args,
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            start_new_session=True,
        )
        _track_child_process(dflash_proc, openai_compatible_local=True)

    print(f"Started Luce DFlash launcher (PID {dflash_proc.pid})")


def _start_qwopus_llama_server(
    project_root: Path,
    config: object | None = None,
) -> None:
    script_path = project_root / "scripts" / "start_qwopus_llama_server.ps1"
    if not script_path.exists():
        raise RuntimeError(f"Qwopus startup script not found: {script_path}")
    launch_args = _qwopus_launch_args(config)

    if _IS_WINDOWS:
        qwopus_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *launch_args,
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _track_child_process(qwopus_proc, openai_compatible_local=True)
    else:
        qwopus_proc = subprocess.Popen(
            [
                "pwsh",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *launch_args,
                "-Restart",
                "-Foreground",
            ],
            cwd=str(project_root),
            start_new_session=True,
        )
        _track_child_process(qwopus_proc, openai_compatible_local=True)

    print(f"Started Qwopus llama-server launcher (PID {qwopus_proc.pid})")


def _start_logged_openai_compatible_process(
    args: list[str],
    *,
    cwd: Path,
    log_path: Path,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
    }
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except Exception:
        log_file.close()
        raise
    log_file.close()
    _track_child_process(proc, openai_compatible_local=True)
    return proc


def _start_exo_server(
    project_root: Path,
    config: object | None = None,
) -> None:
    args, cwd = _exo_launch_plan(config, project_root)
    proc = _start_logged_openai_compatible_process(
        args,
        cwd=cwd,
        log_path=_service_log_dir(project_root) / "exo.log",
    )
    print(f"Started exo OpenAI-compatible server (PID {proc.pid})")


def _start_mlx_lm_server(
    project_root: Path,
    config: object | None = None,
) -> None:
    args, cwd = _mlx_lm_launch_plan(config)
    proc = _start_logged_openai_compatible_process(
        args,
        cwd=cwd,
        log_path=_service_log_dir(project_root) / "mlx_lm.log",
    )
    print(f"Started MLX LM OpenAI-compatible server (PID {proc.pid})")


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


def _frontend_build_fingerprint_path(frontend_dir: Path) -> Path:
    return frontend_dir / _FRONTEND_BUILD_FINGERPRINT_REL_PATH


def _is_frontend_build_input(path: Path, frontend_dir: Path) -> bool:
    try:
        relative = path.relative_to(frontend_dir)
    except ValueError:
        return False

    if any(part in _FRONTEND_BUILD_EXCLUDED_DIR_NAMES for part in relative.parts):
        return False
    if path.name in _FRONTEND_BUILD_EXCLUDED_FILE_NAMES:
        return False
    if path.name.startswith(".env"):
        return False
    return path.suffix.lower() in _FRONTEND_BUILD_INPUT_SUFFIXES


def _iter_frontend_build_input_files(frontend_dir: Path) -> list[Path]:
    if not frontend_dir.is_dir():
        return []
    input_files: list[Path] = []
    for root, dir_names, file_names in os.walk(frontend_dir):
        dir_names[:] = [
            name for name in dir_names if name not in _FRONTEND_BUILD_EXCLUDED_DIR_NAMES
        ]
        root_path = Path(root)
        for file_name in file_names:
            path = root_path / file_name
            if _is_frontend_build_input(path, frontend_dir):
                input_files.append(path)
    return sorted(input_files)


def _frontend_build_fingerprint(frontend_dir: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    file_count = 0
    for path in _iter_frontend_build_input_files(frontend_dir):
        relative = path.relative_to(frontend_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
        file_count += 1

    return {
        "version": _FRONTEND_BUILD_FINGERPRINT_VERSION,
        "digest": digest.hexdigest(),
        "file_count": file_count,
    }


def _read_frontend_build_fingerprint(frontend_dir: Path) -> dict[str, object] | None:
    try:
        return json.loads(
            _frontend_build_fingerprint_path(frontend_dir).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


def _write_frontend_build_fingerprint(
    frontend_dir: Path,
    fingerprint: dict[str, object],
) -> None:
    path = _frontend_build_fingerprint_path(frontend_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _normalize_next_static_asset_ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    ref = value.strip().strip("\"'`")
    if ref.startswith("/_next/"):
        ref = ref[len("/_next/") :]
    elif ref.startswith("_next/"):
        ref = ref[len("_next/") :]
    elif ref.startswith("/static/"):
        ref = ref[1:]

    if not ref.startswith("static/"):
        return None

    ref = unquote(ref.split("?", 1)[0].split("#", 1)[0])
    ref = ref.rstrip(";,")
    parts = [part for part in ref.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    if "." not in parts[-1]:
        return None
    return "/".join(parts)


def _iter_static_refs_from_json(value: object) -> set[str]:
    refs: set[str] = set()
    normalized = _normalize_next_static_asset_ref(value)
    if normalized:
        refs.add(normalized)
        return refs

    if isinstance(value, dict):
        for item in value.values():
            refs.update(_iter_static_refs_from_json(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_iter_static_refs_from_json(item))
    return refs


def _iter_static_refs_from_text(text: str) -> set[str]:
    refs: set[str] = set()
    for candidate_text in {text, text.replace("\\/", "/")}:
        for match in _FRONTEND_STATIC_REF_PATTERN.finditer(candidate_text):
            normalized = _normalize_next_static_asset_ref(match.group(0))
            if normalized:
                refs.add(normalized)
    return refs


def _collect_next_static_asset_refs(next_dir: Path) -> set[str]:
    refs: set[str] = set()
    scan_roots = [
        path
        for path in (
            next_dir / "build-manifest.json",
            next_dir / "app-build-manifest.json",
            next_dir / "server" / "app-build-manifest.json",
        )
        if path.is_file()
    ]
    server_dir = next_dir / "server"
    if server_dir.is_dir():
        scan_roots.extend(
            path
            for path in server_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".html", ".js", ".json", ".rsc"}
        )

    for path in sorted(set(scan_roots)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if path.suffix.lower() == ".json":
            try:
                refs.update(_iter_static_refs_from_json(json.loads(text)))
                continue
            except json.JSONDecodeError:
                pass
        refs.update(_iter_static_refs_from_text(text))

    return refs


def _missing_next_static_assets(next_dir: Path) -> list[str]:
    missing: list[str] = []
    for ref in _collect_next_static_asset_refs(next_dir):
        target = next_dir.joinpath(*ref.split("/"))
        if not target.is_file():
            missing.append(ref)
    return sorted(missing)


def _frontend_static_build_invalid_reason(frontend_dir: Path) -> str | None:
    next_dir = frontend_dir / ".next"
    required_paths = [
        next_dir,
        next_dir / "BUILD_ID",
        next_dir / "server",
        next_dir / "static",
        next_dir / "static" / "chunks",
    ]
    for path in required_paths:
        if not path.exists():
            return f"Next.js build artifact is missing: {path.relative_to(frontend_dir)}"

    missing_assets = _missing_next_static_assets(next_dir)
    if missing_assets:
        shown = ", ".join(missing_assets[:5])
        suffix = "" if len(missing_assets) <= 5 else f" and {len(missing_assets) - 5} more"
        return f"Next.js build references missing static asset(s): {shown}{suffix}"
    return None


def _frontend_build_rebuild_reason(frontend_dir: Path) -> tuple[str | None, dict[str, object]]:
    fingerprint = _frontend_build_fingerprint(frontend_dir)
    invalid_reason = _frontend_static_build_invalid_reason(frontend_dir)
    if invalid_reason:
        return invalid_reason, fingerprint

    stored = _read_frontend_build_fingerprint(frontend_dir)
    if stored != fingerprint:
        return "Frontend source fingerprint changed since the last verified build", fingerprint

    return None, fingerprint


def _ensure_frontend_build(
    project_root: Path,
    log_path: Path,
    env: dict[str, str],
) -> None:
    """Ensure Next.js production artifacts match the current frontend tree."""
    frontend_dir = project_root / "frontend"
    reason, fingerprint = _frontend_build_rebuild_reason(frontend_dir)
    if not reason:
        return

    next_dir = frontend_dir / ".next"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"{reason}; running npm run build before startup.\n")
        log_file.flush()

        if next_dir.exists():
            try:
                shutil.rmtree(next_dir)
            except OSError as exc:
                raise RuntimeError(
                    "Failed to remove stale Next.js build artifacts before rebuild.\n"
                    f"frontend.log tail:\n{_read_log_tail(log_path)}"
                ) from exc

        result = subprocess.run(
            [_npm_command(), "run", "build"],
            cwd=str(frontend_dir),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Frontend build is stale or broken and npm run build failed.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    post_build_reason = _frontend_static_build_invalid_reason(frontend_dir)
    if post_build_reason:
        raise RuntimeError(
            f"Frontend build completed but remains invalid: {post_build_reason}.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    _write_frontend_build_fingerprint(frontend_dir, fingerprint)


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
    should_start_exo = _should_start_exo_server(config)
    should_start_mlx_lm = _should_start_mlx_lm_server(config)
    local_server_launch_started = ensure_openai_compatible_local_server(config)

    frontend_log_path = _service_log_dir(project_root) / "frontend.log"
    _ensure_frontend_dependencies(project_root, frontend_log_path)
    frontend_env = _build_frontend_env(project_root)
    _ensure_frontend_build(project_root, frontend_log_path, frontend_env)

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
        _track_child_process(frontend_proc)

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
        _track_child_process(caddy_proc)
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
        _track_child_process(frontend_proc)

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
        _track_child_process(caddy_proc)

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
    elif should_start_exo and local_server_launch_started:
        print(
            f"Started Frontend (PID {frontend_proc.pid}) / "
            f"Caddy (PID {caddy_proc.pid}) / exo server starting"
        )
    elif should_start_mlx_lm and local_server_launch_started:
        print(
            f"Started Frontend (PID {frontend_proc.pid}) / "
            f"Caddy (PID {caddy_proc.pid}) / MLX LM server starting"
        )
    else:
        print(f"Started Frontend (PID {frontend_proc.pid}) / Caddy (PID {caddy_proc.pid})")


def kill_services() -> None:
    """Stop child service processes started by start_services."""
    for proc in list(_child_processes):
        _terminate_process_tree(proc)
    _child_processes.clear()
    _openai_compatible_local_processes.clear()
