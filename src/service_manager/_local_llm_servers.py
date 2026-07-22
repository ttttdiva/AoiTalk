"""ローカル OpenAI 互換 LLM サーバーの起動プラン群。

Luce DFlash / exo / MLX LM 各エンジンの「起動すべきか」の判定、起動引数・起動
コマンドの構築、ローカルサーバーの稼働確認、そして exo / MLX LM / Luce DFlash の
起動処理を提供する。挙動は分割前の `service_manager.py` と同一（機械的移設）。

補足: Qwopus 用の `_qwopus_launch_args` / `_start_qwopus_llama_server`、および
オーケストレーションを担う `ensure_openai_compatible_local_server` /
`validate_openai_compatible_local_launch_selection` は、テストが
`service_manager.<名前>` を monkeypatch した状態でそれらの本体が実行される都合上、
パッケージ `__init__.py`（ファサード）側に定義している。

リポジトリルートの算出について: 分割前は `src/service_manager.py` から
`Path(__file__).resolve().parent.parent` でルートを求めていた。本モジュールは
1 階層深い `src/service_manager/` 配下にあるため、同じ結果を得るよう
`Path(__file__).resolve().parents[2]` を用いる。
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from src.llm.openai_compatible_local_profiles import (
    EXO_BASE_URL,
    MLX_LM_BASE_URL,
    is_macos,
    local_server_profile_for_model,
    normalize_openai_compatible_base_url,
    openai_compatible_local_base_url,
)

from ._process_utils import (
    _IS_WINDOWS,
    _is_port_open,
    _service_log_dir,
    _track_child_process,
)

_LUCE_DFLASH_MODEL_IDS = {
    "luce-dflash",  # Lucebox server alias exposed by /v1/models.
    "qwen3.6-27b",
    "qwen3.6-27b-dflash",
}
_QWOPUS_MODEL_IDS = {
    "qwopus3.6-35b-a3b",
}
_DEFAULT_AI_ROOT = Path(Path(__file__).resolve().anchor or ".") / "AI"
_DEFAULT_DEV_ROOT = Path(Path(__file__).resolve().anchor or ".") / "Dev"
_DEFAULT_HOT_LLM_ROOT = _DEFAULT_AI_ROOT / "models" / "Hot" / "llm"
_DEFAULT_QWOPUS_MODEL_PATH = (
    _DEFAULT_HOT_LLM_ROOT
    / "qwopus"
    / "models"
    / "Qwopus3.6-35B-A3B-v1-Q4_K_M.gguf"
)
_DEFAULT_LUCE_DFLASH_ROOT = _DEFAULT_DEV_ROOT / "67_lucebox-hub" / "dflash"
_DEFAULT_LUCE_DFLASH_TARGET_MODEL = (
    _DEFAULT_HOT_LLM_ROOT / "luce-dflash" / "models" / "Qwen3.6-27B-Q4_K_M.gguf"
)
_DEFAULT_LUCE_DFLASH_DRAFT_MODEL = (
    _DEFAULT_HOT_LLM_ROOT
    / "luce-dflash"
    / "models"
    / "draft"
    / "dflash-draft-3.6-q8_0.gguf"
)


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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    project_root = Path(__file__).resolve().parents[2]
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
