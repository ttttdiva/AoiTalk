"""AoiTalk サービス起動オーケストレーション（責務分割パッケージ）。

分割前は単一ファイル `src/service_manager.py`（約1500行）に、(a) プロセス追跡/kill、
(b) ローカル LLM サーバー起動、(c) フロントエンドビルド指紋管理、(d) 起動
オーケストレーション の 4 系統が同居していた。保守性のため以下へ分割した:

- `._process_utils`      : プロセス追跡・ポート管理の低レベルユーティリティ
- `._frontend_build`     : Next.js ビルド指紋・静的アセット検査
- `._local_llm_servers`  : 各ローカル LLM エンジンの起動プラン
- 本 `__init__`（ファサード）: オーケストレーション本体と、テストが
  `service_manager.<名前>` を monkeypatch した状態で本体が実行される「hot core」
  関数（ensure / validate / qwopus 起動 / プロセス木停止 / start・kill_services 等）

分割方式は「既存 import 利用のみ（スクリプトパス起動なし）」の調査結果に基づく
パッケージ化 + 全公開 API 再エクスポート。分割前の公開シンボルはすべて本
`__init__` から import 可能で、挙動は分割前と同一（機械的移設）。

注意（hot core をファサードに残す理由）: 既存テストは `service_manager._IS_WINDOWS`
や `service_manager._start_exo_server` などモジュール属性を rebind する monkeypatch
を多用し、それらを参照する関数が同一名前空間で解決されることに依存する。関数の
グローバル参照は「定義モジュール」の名前空間で解決されるため、patch 下で本体が
実行される関数は本 `__init__` に定義する必要がある。独立したヘルパー群のみ
サブモジュールへ移設し、ファサードへ import して再エクスポートしている。

リポジトリルート算出: 分割前は `src/service_manager.py` から
`Path(__file__).resolve().parent.parent` を用いた。本パッケージは 1 階層深い
`src/service_manager/__init__.py` にあるため、同じ結果となる
`Path(__file__).resolve().parents[2]` を用いる。
"""

import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
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

from ._process_utils import (
    _IS_WINDOWS,
    _child_processes,
    _extract_port_from_netstat_address,
    _is_port_open,
    _kill_existing_on_port,
    _listening_pid_for_netstat_line,
    _openai_compatible_local_processes,
    _read_log_tail,
    _remove_tracked_process,
    _service_log_dir,
    _track_child_process,
    _wait_for_port,
)
from ._frontend_build import (
    _FRONTEND_BUILD_EXCLUDED_DIR_NAMES,
    _FRONTEND_BUILD_EXCLUDED_FILE_NAMES,
    _FRONTEND_BUILD_FINGERPRINT_REL_PATH,
    _FRONTEND_BUILD_FINGERPRINT_VERSION,
    _FRONTEND_BUILD_INPUT_SUFFIXES,
    _FRONTEND_STATIC_ASSET_SUFFIXES,
    _FRONTEND_STATIC_REF_PATTERN,
    _collect_next_static_asset_refs,
    _ensure_frontend_build,
    _ensure_frontend_dependencies,
    _frontend_build_fingerprint,
    _frontend_build_fingerprint_path,
    _frontend_build_rebuild_reason,
    _frontend_static_build_invalid_reason,
    _is_frontend_build_input,
    _iter_frontend_build_input_files,
    _iter_static_refs_from_json,
    _iter_static_refs_from_text,
    _missing_next_static_assets,
    _next_bin_path,
    _normalize_next_static_asset_ref,
    _npm_command,
    _read_frontend_build_fingerprint,
    _write_frontend_build_fingerprint,
)
from ._local_llm_servers import (
    _DEFAULT_AI_ROOT,
    _DEFAULT_DEV_ROOT,
    _DEFAULT_HOT_LLM_ROOT,
    _DEFAULT_LUCE_DFLASH_DRAFT_MODEL,
    _DEFAULT_LUCE_DFLASH_ROOT,
    _DEFAULT_LUCE_DFLASH_TARGET_MODEL,
    _DEFAULT_QWOPUS_MODEL_PATH,
    _LUCE_DFLASH_MODEL_IDS,
    _QWOPUS_MODEL_IDS,
    _base_url_host_port,
    _config_bool,
    _config_get,
    _configured_command,
    _configured_path,
    _env_bool,
    _env_or_default,
    _exo_launch_plan,
    _is_openai_compatible_local_server_running,
    _is_selected_local_model_running,
    _local_openai_model_ids,
    _luce_dflash_launch_args,
    _mlx_lm_launch_plan,
    _openai_compatible_local_base_url,
    _openai_compatible_local_model,
    _openai_compatible_local_model_id,
    _parse_positive_int,
    _profile_base_url_for_model,
    _require_existing_file,
    _require_existing_path,
    _resolve_optional_executable,
    _selected_openai_compatible_profile,
    _should_start_exo_server,
    _should_start_luce_dflash,
    _should_start_mlx_lm_server,
    _should_start_qwopus_llama_server,
    _split_launch_command,
    _start_exo_server,
    _start_logged_openai_compatible_process,
    _start_luce_dflash,
    _start_mlx_lm_server,
)

logger = logging.getLogger(__name__)


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
    project_root = Path(__file__).resolve().parents[2]
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
    project_root = Path(__file__).resolve().parents[2]
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


def _should_skip_caddy_for_desktop() -> bool:
    return _env_bool("AOITALK_SKIP_CADDY") or _env_bool("AOITALK_DESKTOP")


def _service_port(
    config: object | None,
    *,
    env_names: tuple[str, ...],
    config_key: str,
    default: int,
) -> int:
    """Resolve one instance-scoped service port from env/config."""
    raw = next((os.getenv(name) for name in env_names if os.getenv(name)), None)
    if raw is None:
        raw = _config_get(config, config_key, default)
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        port = default
    return port if 1 <= port <= 65535 else default


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
    project_root = Path(__file__).resolve().parents[2]
    skip_caddy = _should_skip_caddy_for_desktop()
    fastapi_port = _service_port(
        config,
        env_names=("AOITALK_WEB_PORT", "AOITALK_FASTAPI_PORT"),
        config_key="web_interface.port",
        default=3000,
    )
    next_port = _service_port(
        config,
        env_names=("AOITALK_NEXT_PORT", "AOITALK_FRONTEND_PORT"),
        config_key="frontend.port",
        default=3002,
    )
    caddy_port = _service_port(
        config,
        env_names=("AOITALK_CADDY_PORT",),
        config_key="caddy.port",
        default=6002,
    )
    caddy_fastapi_port = _service_port(
        config,
        env_names=("AOITALK_CADDY_FASTAPI_PORT",),
        config_key="caddy.fastapi_port",
        default=fastapi_port,
    )
    caddy_next_port = _service_port(
        config,
        env_names=("AOITALK_CADDY_NEXT_PORT",),
        config_key="caddy.next_port",
        default=next_port,
    )

    _kill_existing_on_port(fastapi_port)
    if not skip_caddy:
        _kill_existing_on_port(caddy_port)
        _kill_existing_on_port(next_port)

    frontend_already_running = skip_caddy and _wait_for_port(
        "127.0.0.1", next_port, timeout_seconds=0.5
    )

    should_start_dflash = _should_start_luce_dflash(config)
    should_start_qwopus = _should_start_qwopus_llama_server(config)
    should_start_exo = _should_start_exo_server(config)
    should_start_mlx_lm = _should_start_mlx_lm_server(config)
    local_server_launch_started = ensure_openai_compatible_local_server(config)

    frontend_log_path = _service_log_dir(project_root) / "frontend.log"
    _ensure_frontend_dependencies(project_root, frontend_log_path)
    frontend_env = _build_frontend_env(project_root)
    _ensure_frontend_build(project_root, frontend_log_path, frontend_env)

    frontend_proc: subprocess.Popen | None = None
    caddy_proc: subprocess.Popen | None = None

    if frontend_already_running:
        pass
    elif _IS_WINDOWS:
        frontend_proc = subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-Command",
                (
                    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                    f"if (Test-Path '{frontend_log_path}') {{ Remove-Item '{frontend_log_path}' }}; "
                    f"npm run start -- -p {next_port} -H 0.0.0.0 2>&1 | "
                    f"ForEach-Object {{ Write-Host $_; Add-Content -Path '{frontend_log_path}' -Value $_ -Encoding UTF8 }}"
                ),
            ],
            cwd=str(project_root / "frontend"),
            env=frontend_env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _track_child_process(frontend_proc)

        if not _wait_for_port("127.0.0.1", next_port, timeout_seconds=45):
            raise RuntimeError(
                f"Next.js frontend did not start listening on 127.0.0.1:{next_port}.\n"
                f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
            )

        if not skip_caddy:
            caddy_proc = subprocess.Popen(
                [_resolve_caddy_binary(project_root), "run", "--config", "Caddyfile"],
                cwd=str(project_root / "caddy"),
                env={
                    **os.environ,
                    "AOITALK_CADDY_PORT": str(caddy_port),
                    "AOITALK_CADDY_FASTAPI_PORT": str(caddy_fastapi_port),
                    "AOITALK_CADDY_NEXT_PORT": str(caddy_next_port),
                },
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            _track_child_process(caddy_proc)
    else:
        frontend_cmd = (
            f"npm run start -- -p {next_port} -H 0.0.0.0 2>&1 | "
            f"tee {str(frontend_log_path)!r}"
        )
        frontend_proc = subprocess.Popen(
            ["bash", "-c", frontend_cmd],
            cwd=str(project_root / "frontend"),
            env=frontend_env,
            start_new_session=True,
        )
        _track_child_process(frontend_proc)

        if not _wait_for_port("127.0.0.1", next_port, timeout_seconds=45):
            raise RuntimeError(
                f"Next.js frontend did not start listening on 127.0.0.1:{next_port}.\n"
                f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
            )

        if not skip_caddy:
            caddy_proc = subprocess.Popen(
                [_resolve_caddy_binary(project_root), "run", "--config", "Caddyfile"],
                cwd=str(project_root / "caddy"),
                env={
                    **os.environ,
                    "AOITALK_CADDY_PORT": str(caddy_port),
                    "AOITALK_CADDY_FASTAPI_PORT": str(caddy_fastapi_port),
                    "AOITALK_CADDY_NEXT_PORT": str(caddy_next_port),
                },
                start_new_session=True,
            )
            _track_child_process(caddy_proc)

    caddy_status = (
        "Caddy skipped for desktop"
        if skip_caddy
        else f"Caddy (PID {caddy_proc.pid})"
    )
    frontend_status = (
        f"Frontend (existing on 127.0.0.1:{next_port})"
        if frontend_proc is None
        else f"Frontend (PID {frontend_proc.pid})"
    )

    if should_start_dflash and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / Luce DFlash starting"
        )
    elif should_start_qwopus and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / Qwopus llama-server starting"
        )
    elif should_start_exo and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / exo server starting"
        )
    elif should_start_mlx_lm and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / MLX LM server starting"
        )
    else:
        print(f"Started {frontend_status} / {caddy_status}")


def kill_services() -> None:
    """Stop child service processes started by start_services."""
    for proc in list(_child_processes):
        _terminate_process_tree(proc)
    _child_processes.clear()
    _openai_compatible_local_processes.clear()
