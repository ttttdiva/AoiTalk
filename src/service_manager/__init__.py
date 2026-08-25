"""AoiTalk サービス起動オーケストレーション（責務分割パッケージ）。

分割前は単一ファイル `src/service_manager.py`（約1500行）に、(a) プロセス追跡/kill、
(b) ローカル LLM サーバー起動、(c) フロントエンドビルド指紋管理、(d) 起動
オーケストレーション の 4 系統が同居していた。保守性のため以下へ分割した:

- `._process_utils`      : プロセス追跡・ポート管理の低レベルユーティリティ
- `._frontend_build`     : Next.js ビルド指紋・静的アセット検査
- `._local_llm_servers`  : 各ローカル LLM エンジンの起動プラン
- 本 `__init__`（ファサード）: オーケストレーション本体と、テストが
  `service_manager.<名前>` を monkeypatch した状態で本体が実行される「hot core」
  関数（ensure / validate / プロセス木停止 / start・kill_services 等）

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
import ipaddress
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
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
from src.features import Features
from src.utils.startup_timing import get_startup_timer


_startup_timer = get_startup_timer()

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
    _models_log_dir,
    _web_log_dir,
    _track_child_process,
    _wait_for_port,
    _wait_for_port_closed,
    _wait_for_process_port,
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
    _validate_frontend_startup_artifacts,
    _write_frontend_build_fingerprint,
)
from ._local_llm_servers import (
    _DEFAULT_AI_ROOT,
    _DEFAULT_DEV_ROOT,
    _DEFAULT_HOT_LLM_ROOT,
    _DEFAULT_MUSE_GLIMMER_MODEL_PATH,
    _base_url_host_port,
    _config_bool,
    _config_get,
    _configured_command,
    _configured_path,
    _env_bool,
    _env_or_default,
    _exo_launch_plan,
    _is_openai_compatible_local_server_running,
    _LLAMA_CPP_MIN_MUSE_BUILD,
    _llama_cpp_base_url,
    _llama_cpp_launch_args as _build_llama_cpp_launch_args,
    _llama_cpp_launch_plan as _build_llama_cpp_launch_plan,
    _llama_cpp_model_ids_exact,
    _llama_cpp_managed_extra_flag,
    _llama_cpp_is_managed_selection,
    _llama_cpp_is_profile_selection,
    _llama_cpp_runtime_applies_to_selection,
    llama_cpp_managed_launch_configured,
    llama_cpp_managed_launch_configuration_error,
    llama_cpp_manual_managed_runtime,
    resolve_llama_cpp_runtime,
    llama_cpp_runtime_requirement,
    build_llama_cpp_profile_runtime_patch,
    should_resolve_llama_cpp_runtime_for_engine_switch,
    _validate_llama_cpp_model_alias,
    _validate_llama_cpp_extra_args,
    _llama_cpp_settings,
    _llama_cpp_selected_model,
    _should_start_llama_cpp as _should_start_llama_cpp_runtime,
    _start_llama_cpp_server,
    _is_selected_local_model_running,
    _local_openai_model_ids,
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
    _should_start_mlx_lm_server,
    _split_launch_command,
    _start_exo_server,
    _start_logged_openai_compatible_process,
    _start_mlx_lm_server,
)

logger = logging.getLogger(__name__)

_ENTERPRISE_CADDYFILE_MAX_BYTES = 1024 * 1024
_LLAMA_CPP_ENSURE_LOCK = threading.RLock()
_LLAMA_CPP_LEASE_CONDITION = threading.Condition()
# endpoint -> {"alias": str, "count": int}
_LLAMA_CPP_GENERATION_LEASES: dict[str, dict[str, object]] = {}
_LLAMA_CPP_LEASE_TICKET_SEQ = itertools.count(1)
_LLAMA_CPP_GENERATION_LEASE_TICKETS: dict[int, "LlamaCppGenerationLeaseTicket"] = {}


class _LlamaCppGenerationLeaseBusy(Exception):
    """Internal: owned stop is blocked by an in-flight generation lease."""

    def __init__(self, endpoint: str, alias: str, count: int):
        super().__init__(endpoint)
        self.endpoint = endpoint
        self.alias = alias
        self.count = count


class _LlamaCppGenerationLeaseTimeout(RuntimeError):
    """Generation leases did not drain before a normal lifecycle transition."""


def _normalize_llama_cpp_lease_endpoint(base_url: str) -> str:
    return normalize_openai_compatible_base_url(str(base_url or "").strip())


def _llama_cpp_generation_lease_timeout(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
) -> float:
    settings = _llama_cpp_settings(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    try:
        timeout = float(settings["readiness_timeout"])
    except (KeyError, TypeError, ValueError):
        timeout = 180.0
    if timeout <= 0:
        return 180.0
    return timeout


def _llama_cpp_generation_lease_identity(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
) -> tuple[str, str] | None:
    """Return (endpoint, alias) when this selection is a managed auto-start target."""

    if not _should_start_llama_cpp(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    ):
        return None
    settings = _llama_cpp_settings(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    alias = str(settings.get("model_alias") or "").strip()
    if not alias:
        alias = str(model or _llama_cpp_selected_model(config) or "").strip()
    if not alias:
        return None
    endpoint = _normalize_llama_cpp_lease_endpoint(
        _llama_cpp_base_url(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
    )
    if not endpoint:
        return None
    return endpoint, alias


def _peek_llama_cpp_generation_lease(endpoint: str) -> tuple[str, int]:
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    with _LLAMA_CPP_LEASE_CONDITION:
        lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
        if not lease:
            return "", 0
        return str(lease.get("alias") or ""), int(lease.get("count") or 0)


def _iter_owned_llama_cpp_lease_endpoints() -> list[str]:
    endpoints: list[str] = []
    seen: set[str] = set()
    for proc in list(_openai_compatible_local_processes):
        recorded = getattr(proc, "_aoi_llama_cpp_base_url", None)
        if not recorded:
            continue
        endpoint = _normalize_llama_cpp_lease_endpoint(str(recorded))
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints


def _llama_cpp_endpoints_requiring_lease_drain(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    force_restart: bool = False,
) -> list[str]:
    """Endpoints whose owned process must not stop until generation leases are idle."""

    endpoints: list[str] = []
    seen: set[str] = set()

    def _add(endpoint: str) -> None:
        clean = _normalize_llama_cpp_lease_endpoint(endpoint)
        if not clean or clean in seen:
            return
        seen.add(clean)
        endpoints.append(clean)

    identity = _llama_cpp_generation_lease_identity(
        config,
        model=model,
        overrides=overrides,
    )
    owned = _iter_owned_llama_cpp_lease_endpoints()
    candidates: list[str] = []
    if identity is not None:
        endpoint, alias = identity
        current_alias, count = _peek_llama_cpp_generation_lease(endpoint)
        if force_restart:
            candidates.append(endpoint)
            candidates.extend(owned)
        elif count > 0 and current_alias != alias:
            candidates.append(endpoint)
    elif force_restart:
        candidates.extend(owned)
    for candidate in candidates:
        _count_alias, count = _peek_llama_cpp_generation_lease(candidate)
        if count > 0:
            _add(candidate)
    return endpoints


def _wait_for_llama_cpp_generation_lease_idle(
    endpoint: str,
    timeout_seconds: float,
) -> None:
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    with _LLAMA_CPP_LEASE_CONDITION:
        while True:
            lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
            count = int((lease or {}).get("count") or 0)
            if count <= 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current_alias = str((lease or {}).get("alias") or "")
                raise RuntimeError(
                    "llama.cppの生成lease待ちがtimeoutしました。"
                    f"endpoint={normalized!r}, 使用中alias={current_alias!r}, "
                    f"使用中count={count}。"
                    "実行中の生成は停止せず、切替を中止しました。"
                )
            _LLAMA_CPP_LEASE_CONDITION.wait(timeout=remaining)


class LlamaCppGenerationLeaseTicket:
    """Exactly-once generation lease ticket. ``release()`` is idempotent."""

    __slots__ = ("endpoint", "alias", "ticket_id", "_released", "_holder")

    def __init__(self, endpoint: str, alias: str, ticket_id: int) -> None:
        self.endpoint = endpoint
        self.alias = alias
        self.ticket_id = ticket_id
        self._released = False
        self._holder = None

    def release(self) -> None:
        _release_llama_cpp_generation_lease_ticket(self)


def _register_llama_cpp_generation_lease_ticket(
    holder: object,
    ticket: LlamaCppGenerationLeaseTicket,
) -> None:
    tickets = getattr(holder, "_llama_cpp_generation_lease_tickets", None)
    if not isinstance(tickets, set):
        tickets = set()
        setattr(holder, "_llama_cpp_generation_lease_tickets", tickets)
    tickets.add(ticket)
    ticket._holder = holder


def _unregister_llama_cpp_generation_lease_ticket(
    ticket: LlamaCppGenerationLeaseTicket,
) -> None:
    holder = ticket._holder
    ticket._holder = None
    if holder is None:
        return
    tickets = getattr(holder, "_llama_cpp_generation_lease_tickets", None)
    if isinstance(tickets, set):
        tickets.discard(ticket)


def _acquire_llama_cpp_generation_lease(
    endpoint: str,
    alias: str,
    holder: object | None = None,
) -> LlamaCppGenerationLeaseTicket:
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    target_alias = str(alias or "").strip()
    # The ensure lock is the lifecycle boundary.  A normal stop holds it from
    # the idle check through process termination, so a new generation cannot
    # acquire a ticket in the gap between those two operations.  Release only
    # takes the condition lock, preserving the ability of an in-flight
    # generation to drain while a lifecycle transition waits.
    with _LLAMA_CPP_ENSURE_LOCK:
        with _LLAMA_CPP_LEASE_CONDITION:
            lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
            count = int((lease or {}).get("count") or 0)
            current_alias = str((lease or {}).get("alias") or "")
            if count > 0 and current_alias != target_alias:
                raise RuntimeError(
                    "llama.cppの生成leaseを別aliasへ切り替えられません。"
                    f"endpoint={normalized!r}, 使用中alias={current_alias!r}, "
                    f"要求alias={target_alias!r}, 使用中count={count}。"
                )
            ticket_id = next(_LLAMA_CPP_LEASE_TICKET_SEQ)
            ticket = LlamaCppGenerationLeaseTicket(normalized, target_alias, ticket_id)
            _LLAMA_CPP_GENERATION_LEASES[normalized] = {
                "alias": target_alias,
                "count": count + 1,
            }
            _LLAMA_CPP_GENERATION_LEASE_TICKETS[ticket_id] = ticket
            if holder is not None:
                _register_llama_cpp_generation_lease_ticket(holder, ticket)
            _LLAMA_CPP_LEASE_CONDITION.notify_all()
            return ticket


def _release_llama_cpp_generation_lease(endpoint: str, alias: str) -> None:
    del alias
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    with _LLAMA_CPP_LEASE_CONDITION:
        lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
        if not lease:
            return
        count = int(lease.get("count") or 0) - 1
        if count <= 0:
            _LLAMA_CPP_GENERATION_LEASES.pop(normalized, None)
        else:
            lease["count"] = count
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


def _release_llama_cpp_generation_lease_ticket(
    ticket: LlamaCppGenerationLeaseTicket | None,
) -> None:
    if ticket is None:
        return
    with _LLAMA_CPP_LEASE_CONDITION:
        if ticket._released:
            return
        ticket._released = True
        _LLAMA_CPP_GENERATION_LEASE_TICKETS.pop(ticket.ticket_id, None)
        _unregister_llama_cpp_generation_lease_ticket(ticket)
        lease = _LLAMA_CPP_GENERATION_LEASES.get(ticket.endpoint)
        if not lease:
            _LLAMA_CPP_LEASE_CONDITION.notify_all()
            return
        count = int(lease.get("count") or 0) - 1
        if count <= 0:
            _LLAMA_CPP_GENERATION_LEASES.pop(ticket.endpoint, None)
        else:
            lease["count"] = count
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


def _reset_llama_cpp_generation_leases() -> None:
    with _LLAMA_CPP_LEASE_CONDITION:
        for ticket in list(_LLAMA_CPP_GENERATION_LEASE_TICKETS.values()):
            ticket._released = True
            ticket._holder = None
        _LLAMA_CPP_GENERATION_LEASE_TICKETS.clear()
        _LLAMA_CPP_GENERATION_LEASES.clear()
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


def _release_llama_cpp_generation_lease_for_holder(
    holder: object | None,
    *,
    all: bool = False,
) -> None:
    if holder is None:
        return
    with _LLAMA_CPP_LEASE_CONDITION:
        tickets = getattr(holder, "_llama_cpp_generation_lease_tickets", None)
        if not isinstance(tickets, set) or not tickets:
            return
        pending = list(tickets)
    if not all:
        pending = pending[:1]
    for ticket in pending:
        ticket.release()


def _stop_owned_openai_compatible_local_servers_if_unleased(endpoint: str) -> None:
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    with _LLAMA_CPP_ENSURE_LOCK:
        with _LLAMA_CPP_LEASE_CONDITION:
            lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
            count = int((lease or {}).get("count") or 0)
            if count > 0:
                raise _LlamaCppGenerationLeaseBusy(
                    normalized,
                    str((lease or {}).get("alias") or ""),
                    count,
                )
        stop_openai_compatible_local_servers()


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
    """Stop local OpenAI-compatible server processes started by AoiTalk.

    Forced terminate.  Shutdown / ``kill_services`` use this directly.
    Usual operations that stop an AoiTalk-owned llama.cpp process must go
    through ``stop_owned_openai_compatible_local_servers_respecting_generation_leases``.
    """
    processes = list(_openai_compatible_local_processes)
    for proc in processes:
        _terminate_process_tree(proc)
        _remove_tracked_process(proc)
    return len(processes)


def _busy_llama_cpp_generation_leases() -> list[tuple[str, str, int]]:
    endpoints = set(_iter_owned_llama_cpp_lease_endpoints())
    endpoints.update(_LLAMA_CPP_GENERATION_LEASES.keys())
    busy: list[tuple[str, str, int]] = []
    for endpoint in endpoints:
        lease = _LLAMA_CPP_GENERATION_LEASES.get(endpoint)
        count = int((lease or {}).get("count") or 0)
        if count > 0:
            busy.append(
                (endpoint, str((lease or {}).get("alias") or ""), count)
            )
    return busy


def stop_owned_openai_compatible_local_servers_respecting_generation_leases(
    *,
    timeout_seconds: float | None = None,
) -> int:
    """Stop owned llama.cpp servers only after generation leases are idle.

    Usual operations (global engine switch / compensation) must use this
    helper.  Timeout leaves processes running and raises RuntimeError.
    """
    timeout = (
        _llama_cpp_generation_lease_timeout(None)
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    deadline = time.monotonic() + max(0.0, timeout)
    # Hold the same lock used by the managed-server ensure path.  Waiting on
    # the lease condition while holding this lock is intentional: generation
    # release only needs the condition lock, whereas a new generation must
    # pass the ensure lock before it can obtain a ticket.  This makes the
    # idle-check -> process-stop interval one atomic lifecycle boundary.
    with _LLAMA_CPP_ENSURE_LOCK:
        with _LLAMA_CPP_LEASE_CONDITION:
            while True:
                busy = _busy_llama_cpp_generation_leases()
                if not busy:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    endpoint, alias, count = busy[0]
                    raise _LlamaCppGenerationLeaseTimeout(
                        "llama.cppの生成lease待ちがtimeoutしました。"
                        f"endpoint={endpoint!r}, 使用中alias={alias!r}, "
                        f"使用中count={count}。"
                        "実行中の生成は停止せず、切替を中止しました。"
                    )
                _LLAMA_CPP_LEASE_CONDITION.wait(timeout=remaining)
        return stop_openai_compatible_local_servers()


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


def _llama_cpp_launch_args(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> list[str]:
    """Facade wrapper that respects tests/desktop callers patching _IS_WINDOWS."""

    return _build_llama_cpp_launch_args(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS if is_windows is None else is_windows,
    )


def _llama_cpp_launch_plan(
    config: object | None,
    *,
    project_root: Path | None = None,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> tuple[list[str], Path]:
    return _build_llama_cpp_launch_plan(
        config,
        project_root=project_root,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS if is_windows is None else is_windows,
    )


def _should_start_llama_cpp(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    return _should_start_llama_cpp_runtime(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS if is_windows is None else is_windows,
    )


def _should_start_llama_cpp_server(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """Backward/diagnostic alias for callers naming the server explicitly."""

    return _should_start_llama_cpp(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )


_llama_cpp_launch_arguments = _llama_cpp_launch_args
_llama_cpp_runtime_settings = _llama_cpp_settings


def _llama_cpp_runtime_conflict(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    force_restart: bool = False,
) -> tuple[bool, str, set[str]]:
    """Inspect the target port without touching externally-owned processes."""

    settings = _llama_cpp_settings(config, model=model, overrides=overrides, is_windows=_IS_WINDOWS)
    base_url = _llama_cpp_base_url(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    expected_alias = str(settings["model_alias"] or "").strip()
    served_ids = _llama_cpp_model_ids_exact(base_url)
    if not served_ids:
        # Keep compatibility with existing tests/integrations that stub the
        # historical lowercase /models helper.
        served_ids = _local_openai_model_ids(base_url)
    host, port = _base_url_host_port(base_url)
    previous_model = _openai_compatible_local_model_id(config)
    normalized_target_url = normalize_openai_compatible_base_url(base_url)

    def _tracked_process_owned_target(proc: object) -> bool:
        """Check endpoint metadata recorded at the time the process started."""

        try:
            if proc.poll() is not None:
                return False
        except Exception:
            return False
        recorded_url = getattr(proc, "_aoi_llama_cpp_base_url", None)
        if not recorded_url:
            # Processes created before endpoint metadata was introduced are
            # never trusted for ownership after a config mutation: the
            # persisted config may already describe a different endpoint.
            return False
        return normalize_openai_compatible_base_url(str(recorded_url)) == normalized_target_url

    owned_at_endpoint = bool(
        _openai_compatible_local_processes
        and any(_tracked_process_owned_target(proc) for proc in _openai_compatible_local_processes)
    )
    owned_previous_process = bool(
        owned_at_endpoint
        and str(previous_model or "").strip().casefold()
        != str(model or _llama_cpp_selected_model(config)).strip().casefold()
    )
    # Ownership is the process at this endpoint plus the alias it currently
    # serves.  Session TargetConfig already overlays openai_compatible_local.model
    # to the target, so previous_model == target cannot detect an owned Muse
    # that should hot-switch to Qwen.
    owned_serving_other = bool(
        owned_at_endpoint
        and served_ids
        and (not expected_alias or expected_alias not in served_ids)
    )
    if expected_alias and expected_alias in served_ids:
        if not force_restart:
            return False, base_url, served_ids
        if owned_at_endpoint:
            return True, base_url, served_ids
        raise RuntimeError(
            "llama.cppの設定変更対象portでは外部プロセスが期待aliasを提供中です。"
            "外部プロセスは停止せず、port/model_aliasを確認してください。"
        )
    if served_ids:
        if (
            owned_serving_other
            or owned_previous_process
            or (force_restart and owned_at_endpoint)
        ):
            # Hot-switch validation runs before the route stops the previous
            # AoiTalk-owned process.  Permit that controlled transition while
            # retaining the external-process rejection below.
            return True, base_url, served_ids
        raise RuntimeError(
            "llama.cppの対象ポートは別モデルを提供中です。"
            f"期待alias={expected_alias!r}, 実際={sorted(served_ids)!r}; "
            "外部プロセスは停止せず、port/model_aliasを確認してください。"
        )
    if _is_port_open(host, port, timeout_seconds=0.5):
        if owned_previous_process or (force_restart and owned_at_endpoint):
            return True, base_url, served_ids
        raise RuntimeError(
            "llama.cppの対象ポートは外部プロセスが使用中です。"
            f"{host}:{port} を停止せず、openai_compatible_local.llama_cppの"
            "host/portを変更してください。"
        )
    return True, base_url, served_ids


def _wait_for_llama_cpp_readiness(
    proc: object,
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
) -> None:
    settings = _llama_cpp_settings(config, model=model, overrides=overrides, is_windows=_IS_WINDOWS)
    base_url = _llama_cpp_base_url(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    expected_alias = str(settings["model_alias"] or "").strip()
    timeout_seconds = float(settings["readiness_timeout"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if proc.poll() is not None:
                raise RuntimeError(
                    "llama-serverが起動完了前に終了しました。"
                    f"期待alias={expected_alias!r}; logs/models/llama_cpp.logを確認してください。"
                )
        except AttributeError:
            pass
        served_ids = _llama_cpp_model_ids_exact(base_url)
        if not served_ids:
            served_ids = _local_openai_model_ids(base_url)
        if expected_alias and expected_alias in served_ids:
            return
        if served_ids and expected_alias not in served_ids:
            raise RuntimeError(
                "llama-serverは起動しましたが、/v1/modelsのaliasが一致しません。"
                f"期待={expected_alias!r}, 実際={sorted(served_ids)!r}。"
            )
        time.sleep(0.25)
    raise RuntimeError(
        "llama-serverのreadiness timeoutです。"
        f"{timeout_seconds:g}秒以内に/v1/modelsでalias {expected_alias!r}を確認できませんでした。"
    )


def _validate_llama_cpp_manual_connection(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
) -> None:
    """Validate an operator-managed llama.cpp endpoint without starting it."""

    selected_model = _llama_cpp_selected_model(config, model)
    settings = _llama_cpp_settings(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    if not _llama_cpp_is_managed_selection(
        config,
        selected_model,
        settings,
        overrides=overrides,
    ):
        return
    base_url = _llama_cpp_base_url(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    expected_model = selected_model
    served_ids = _llama_cpp_model_ids_exact(base_url)
    if not served_ids:
        # Preserve compatibility with existing tests/integrations that replace
        # the historical lowercase probe helper.
        served_ids = _local_openai_model_ids(base_url)
    if expected_model in served_ids:
        return
    host, port = _base_url_host_port(base_url)
    if served_ids:
        raise RuntimeError(
            "llama.cppのmanual接続先は別モデルを提供中です。"
            f"期待={expected_model!r}, 実際={sorted(served_ids)!r}。"
        )
    if _is_port_open(host, port, timeout_seconds=0.5):
        raise RuntimeError(
            "llama.cppのmanual接続先は応答しましたが、/v1/modelsに"
            f"期待model {expected_model!r} がありません。"
        )
    raise RuntimeError(
        "llama.cppのmanual接続先が起動していません。"
        f"/v1/modelsでmodel {expected_model!r} を確認できませんでした: {base_url}"
    )


def _ensure_llama_cpp_server(
    config: object | None,
    *,
    raise_on_launch_error: bool = False,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    force_restart: bool = False,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
) -> bool:
    timeout_seconds = _llama_cpp_generation_lease_timeout(
        config,
        model=model,
        overrides=overrides,
    )
    deadline = time.monotonic() + timeout_seconds
    identity = _llama_cpp_generation_lease_identity(
        config,
        model=model,
        overrides=overrides,
    )
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        for endpoint in _llama_cpp_endpoints_requiring_lease_drain(
            config,
            model=model,
            overrides=overrides,
            force_restart=force_restart,
        ):
            _wait_for_llama_cpp_generation_lease_idle(endpoint, remaining)
            remaining = max(0.0, deadline - time.monotonic())
        with _LLAMA_CPP_ENSURE_LOCK:
            drain_endpoints = _llama_cpp_endpoints_requiring_lease_drain(
                config,
                model=model,
                overrides=overrides,
                force_restart=force_restart,
            )
            if drain_endpoints:
                # Raced: another generation acquired between drain and lock.
                if time.monotonic() >= deadline:
                    _wait_for_llama_cpp_generation_lease_idle(
                        drain_endpoints[0],
                        0.0,
                    )
                continue
            try:
                started = _ensure_llama_cpp_server_unlocked(
                    config,
                    raise_on_launch_error=raise_on_launch_error,
                    model=model,
                    overrides=overrides,
                    force_restart=force_restart,
                )
            except _LlamaCppGenerationLeaseBusy:
                if time.monotonic() >= deadline:
                    raise
                continue
            if acquire_generation_lease and identity is not None:
                endpoint, alias = identity
                served_ids: set[str] = set()
                if not started:
                    served_ids = set(_llama_cpp_model_ids_exact(endpoint) or set())
                    if not served_ids:
                        served_ids = set(_local_openai_model_ids(endpoint) or set())
                if started or alias in served_ids:
                    ticket = _acquire_llama_cpp_generation_lease(
                        endpoint,
                        alias,
                        holder=lease_holder,
                    )
                    if lease_tickets is not None:
                        lease_tickets.append(ticket)
            return started


def _ensure_llama_cpp_server_unlocked(
    config: object | None,
    *,
    raise_on_launch_error: bool = False,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    force_restart: bool = False,
) -> bool:
    project_root = Path(__file__).resolve().parents[2]
    proc = None
    try:
        _validate_llama_cpp_model_alias(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        should_start = True
        _base_url = _llama_cpp_base_url(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        if force_restart:
            runtime_settings = _llama_cpp_settings(
                config,
                model=model,
                overrides=overrides,
                is_windows=_IS_WINDOWS,
            )
            # Validate the target before stopping any tracked process.  This
            # rejects an external listener while still permitting an owned
            # old process, including a changed-port transition.
            should_start, _base_url, _served_ids = _llama_cpp_runtime_conflict(
                config,
                model=model,
                overrides=overrides,
                # Manual (auto_start=false) mode may intentionally reuse an
                # external process that serves the expected alias; only an
                # automatic relaunch treats that listener as a conflict.
                force_restart=bool(runtime_settings["auto_start"]),
            )
            if _openai_compatible_local_processes:
                _stop_owned_openai_compatible_local_servers_if_unleased(_base_url)
        if not _should_start_llama_cpp(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        ):
            return False
        if not force_restart:
            should_start, _base_url, _served_ids = _llama_cpp_runtime_conflict(
                config,
                model=model,
                overrides=overrides,
            )
        if not should_start:
            return False
        if _openai_compatible_local_processes and not force_restart:
            target_host, target_port = _base_url_host_port(_base_url)
            if _is_port_open(target_host, target_port, timeout_seconds=0.25):
                # The conflict was an AoiTalk-owned previous model on the
                # same endpoint (external listeners are rejected above).
                # ``force_restart`` also covers a changed port: the old
                # tracked process must not survive the settings switch.
                _stop_owned_openai_compatible_local_servers_if_unleased(_base_url)
        proc = _start_llama_cpp_server(
            project_root,
            config=config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        _wait_for_llama_cpp_readiness(
            proc,
            config,
            model=model,
            overrides=overrides,
        )
        logger.info("llama-serverの起動とalias確認が完了しました")
        return True
    except Exception as exc:
        if proc is not None:
            _terminate_process_tree(proc)
            _remove_tracked_process(proc)
        if raise_on_launch_error:
            raise
        logger.warning("llama-serverの起動を延期します: %s", exc)
        return False


def validate_openai_compatible_local_launch_selection(
    config: object | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    llama_cpp_settings: dict[str, object] | None = None,
    force_restart: bool = False,
) -> None:
    selected_provider = str(
        provider or _config_get(config, "llm_provider", "") or ""
    ).strip().lower()
    if selected_provider != "openai_compatible_local":
        return

    selected_model_id = str(model or _openai_compatible_local_model_id(config)).strip()
    project_root = Path(__file__).resolve().parents[2]
    profile = local_server_profile_for_model(selected_model_id)
    if profile:
        resolved_base_url = _profile_base_url_for_model(
            config,
            model=selected_model_id,
            base_url=base_url,
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
    elif _llama_cpp_runtime_applies_to_selection(
        config,
        selected_model_id,
        overrides=llama_cpp_settings,
        is_windows=_IS_WINDOWS,
    ):
        resolved_llama_runtime = resolve_llama_cpp_runtime(
            config,
            model=selected_model_id,
            overrides=llama_cpp_settings,
            is_windows=_IS_WINDOWS,
        )
        llama_managed = _validate_llama_cpp_model_alias(
            config,
            model=selected_model_id,
            overrides=llama_cpp_settings,
            is_windows=_IS_WINDOWS,
        )
        llama_runtime_settings = resolved_llama_runtime.get("settings")
        if not isinstance(llama_runtime_settings, dict):
            llama_runtime_settings = _llama_cpp_settings(
                config,
                model=selected_model_id,
                overrides=llama_cpp_settings,
                is_windows=_IS_WINDOWS,
            )
        if _should_start_llama_cpp(
            config,
            model=selected_model_id,
            overrides=llama_cpp_settings,
            is_windows=_IS_WINDOWS,
        ):
            # Do not treat any listener as our server.  A same-port external
            # process is reported as a conflict without being stopped; an
            # already served exact alias is safe to reuse.
            should_start, _runtime_url, _served_ids = _llama_cpp_runtime_conflict(
                config,
                model=selected_model_id,
                overrides=llama_cpp_settings,
                force_restart=force_restart,
            )
            if should_start:
                _llama_cpp_launch_plan(
                    config,
                    model=selected_model_id,
                    overrides=llama_cpp_settings,
                    is_windows=_IS_WINDOWS,
                )
        elif llama_managed and bool(llama_runtime_settings["auto_start"]):
            if _llama_cpp_is_profile_selection(
                selected_model_id,
                model_alias=str(llama_runtime_settings.get("model_alias") or ""),
                model_path=str(llama_runtime_settings.get("model_path") or ""),
            ):
                launch_error = llama_cpp_managed_launch_configuration_error(
                    config,
                    model=selected_model_id,
                    overrides=llama_cpp_settings,
                    is_windows=_IS_WINDOWS,
                )
                if launch_error:
                    raise RuntimeError(
                        "選択した llama.cpp モデルの runtime 設定が未構成です。"
                        f" {launch_error}"
                    )
        elif llama_managed and not bool(llama_runtime_settings["auto_start"]):
            _validate_llama_cpp_manual_connection(
                config,
                model=selected_model_id,
                overrides=llama_cpp_settings,
            )


def ensure_openai_compatible_local_server(
    config: object | None,
    *,
    raise_on_launch_error: bool = False,
    force_restart: bool = False,
    model: str | None = None,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
) -> bool:
    """Start the selected bundled local OpenAI-compatible server if needed.

    Local LLM startup is best-effort. AoiTalk itself should keep starting even
    when the selected model is still loading or the helper process fails to
    launch; generation-time health handling reports that state to the user.
    """
    project_root = Path(__file__).resolve().parents[2]
    base_url = _openai_compatible_local_base_url(config)

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

    if _should_start_llama_cpp(config, model=model, is_windows=_IS_WINDOWS):
        return _ensure_llama_cpp_server(
            config,
            model=model,
            raise_on_launch_error=raise_on_launch_error,
            force_restart=force_restart,
            acquire_generation_lease=acquire_generation_lease,
            lease_holder=lease_holder,
            lease_tickets=lease_tickets,
        )

    return False


def _should_skip_caddy_for_desktop() -> bool:
    return _env_bool("AOITALK_SKIP_CADDY") or _env_bool("AOITALK_DESKTOP")


def _frontend_bind_host() -> str:
    """Resolve the Next.js bind address independently from the proxy address.

    Windows personal installations historically expose port 3002 to the LAN,
    where it is used directly by other PCs. Enterprise and Unix-native
    installations keep the Next.js upstream on loopback so Caddy remains their
    only public boundary. Docker uses 0.0.0.0 because Caddy runs in a separate
    container on the same private network.
    """
    configured_host = os.getenv("AOITALK_NEXT_HOST") or os.getenv(
        "AOITALK_FRONTEND_HOST"
    )
    if configured_host:
        return configured_host
    if _env_bool("AOITALK_DOCKER"):
        return "0.0.0.0"
    if _IS_WINDOWS and Features.profile_name() == "personal":
        return "0.0.0.0"
    return "127.0.0.1"


def _should_trust_login_proxy(frontend_host: str, *, skip_caddy: bool) -> bool:
    """Trust proxy IP headers only for a launcher-enforced private boundary."""
    if _env_bool("AOITALK_DOCKER"):
        # Compose owns this boundary: the application ports are not published,
        # and its explicit setting is covered by the compose contract test.
        return skip_caddy and _env_bool("AOITALK_LOGIN_TRUST_PROXY")
    if skip_caddy:
        return False
    normalized_host = frontend_host.strip().lower().strip("[]")
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


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


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            "EnterpriseのCaddyfileパスのメタデータを確認できません: "
            f"{path}"
        ) from exc
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _assert_no_reparse_path(path: Path, project_root: Path) -> None:
    """Reject symlink/junction components in a security-sensitive path."""
    current = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(project_root))
    while True:
        if _is_reparse_point(current):
            raise RuntimeError(
                "EnterpriseのCaddyfileパスにsymlink/reparse pointは使用できません: "
                f"{current}"
            )
        if current == boundary or current.parent == current:
            return
        current = current.parent


def _resolve_caddyfile_path(project_root: Path) -> Path:
    configured_caddyfile = os.getenv("AOITALK_CADDYFILE_PATH")
    if configured_caddyfile:
        caddyfile_path = Path(configured_caddyfile)
    else:
        caddyfile_path = Path(
            "caddy/Caddyfile.enterprise"
            if Features.is_enterprise()
            else "caddy/Caddyfile"
        )

    if not caddyfile_path.is_absolute():
        caddyfile_path = project_root / caddyfile_path

    if Features.is_enterprise():
        caddyfile_path = Path(os.path.abspath(caddyfile_path))
        enterprise_caddyfile_path = Path(
            os.path.abspath(project_root / "caddy" / "Caddyfile.enterprise")
        )
        if os.path.normcase(str(caddyfile_path)) != os.path.normcase(
            str(enterprise_caddyfile_path)
        ):
            raise RuntimeError(
                "Enterpriseではcaddy/Caddyfile.enterprise以外を使用できません: "
                f"{caddyfile_path}"
            )
        _assert_no_reparse_path(caddyfile_path, project_root)
        try:
            caddyfile_stat = caddyfile_path.lstat()
        except OSError as exc:
            raise RuntimeError(
                "EnterpriseのCaddyfileメタデータを確認できません: "
                f"{caddyfile_path}"
            ) from exc
        if not stat.S_ISREG(caddyfile_stat.st_mode):
            raise RuntimeError(
                "EnterpriseのCaddyfileは正規ファイルである必要があります: "
                f"{caddyfile_path}"
            )
    elif not caddyfile_path.is_file():
        raise RuntimeError(f"Caddyfileが見つかりません: {caddyfile_path}")
    return caddyfile_path


def _caddyfile_identity(path_stat: os.stat_result) -> tuple[int, int]:
    return path_stat.st_dev, path_stat.st_ino


def _caddyfile_change_marker(path_stat: os.stat_result) -> tuple[int, int, int]:
    return path_stat.st_size, path_stat.st_mtime_ns, path_stat.st_ctime_ns


def _read_enterprise_caddyfile_snapshot(
    caddyfile_path: Path, project_root: Path
) -> bytes:
    """Read a bounded, stable Enterprise Caddyfile without reopening its path."""
    _assert_no_reparse_path(caddyfile_path, project_root)
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_CLOEXEC", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(caddyfile_path, open_flags)
    except OSError as exc:
        raise RuntimeError(
            "EnterpriseのCaddyfileを安全にopenできません: "
            f"{caddyfile_path}"
        ) from exc

    try:
        try:
            before_stat = os.fstat(file_descriptor)
        except OSError as exc:
            raise RuntimeError(
                "EnterpriseのCaddyfileのopen後メタデータを確認できません: "
                f"{caddyfile_path}"
            ) from exc
        if not stat.S_ISREG(before_stat.st_mode):
            raise RuntimeError(
                "EnterpriseのCaddyfileは正規ファイルである必要があります: "
                f"{caddyfile_path}"
            )
        if before_stat.st_size > _ENTERPRISE_CADDYFILE_MAX_BYTES:
            raise RuntimeError(
                "EnterpriseのCaddyfileがサイズ上限を超えています: "
                f"{caddyfile_path}"
            )

        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(
                file_descriptor,
                min(64 * 1024, _ENTERPRISE_CADDYFILE_MAX_BYTES + 1 - bytes_read),
            )
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > _ENTERPRISE_CADDYFILE_MAX_BYTES:
                raise RuntimeError(
                    "EnterpriseのCaddyfileがサイズ上限を超えています: "
                    f"{caddyfile_path}"
                )

        try:
            after_stat = os.fstat(file_descriptor)
        except OSError as exc:
            raise RuntimeError(
                "EnterpriseのCaddyfileのread後メタデータを確認できません: "
                f"{caddyfile_path}"
            ) from exc
        if not stat.S_ISREG(after_stat.st_mode):
            raise RuntimeError(
                "EnterpriseのCaddyfileは正規ファイルである必要があります: "
                f"{caddyfile_path}"
            )
        if (
            _caddyfile_identity(before_stat) != _caddyfile_identity(after_stat)
            or _caddyfile_change_marker(before_stat)
            != _caddyfile_change_marker(after_stat)
        ):
            raise RuntimeError(
                "EnterpriseのCaddyfileが読み取り中に変更されました: "
                f"{caddyfile_path}"
            )

        _assert_no_reparse_path(caddyfile_path, project_root)
        try:
            path_stat = caddyfile_path.lstat()
        except OSError as exc:
            raise RuntimeError(
                "EnterpriseのCaddyfileのread後パスを確認できません: "
                f"{caddyfile_path}"
            ) from exc
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or _caddyfile_identity(path_stat) != _caddyfile_identity(after_stat)
        ):
            raise RuntimeError(
                "EnterpriseのCaddyfileパスが読み取り中に置換されました: "
                f"{caddyfile_path}"
            )
        return b"".join(chunks)
    finally:
        os.close(file_descriptor)


def _write_caddyfile_snapshot(
    caddy_proc: subprocess.Popen, caddyfile_snapshot: bytes
) -> None:
    """Write a complete config snapshot and fail closed if Caddy rejects stdin."""
    stdin = caddy_proc.stdin
    try:
        if stdin is None:
            raise OSError("Caddy stdin pipe is unavailable")
        written = stdin.write(caddyfile_snapshot)
        if written != len(caddyfile_snapshot):
            raise OSError(
                f"Caddy stdin short write: {written}/{len(caddyfile_snapshot)}"
            )
        stdin.flush()
        stdin.close()
    except Exception as exc:
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
        _terminate_process_tree(caddy_proc)
        _remove_tracked_process(caddy_proc)
        raise RuntimeError(
            "EnterpriseのCaddyfile snapshotをCaddy stdinへ完全に送信できません"
        ) from exc


def start_caddy(
    config: object | None = None,
    *,
    ready_fastapi_port: int | None = None,
) -> subprocess.Popen | None:
    """Start Caddy only after the FastAPI upstream is ready."""
    if _should_skip_caddy_for_desktop():
        return None

    fastapi_port = _service_port(
        config,
        env_names=("AOITALK_WEB_PORT", "AOITALK_FASTAPI_PORT"),
        config_key="web_interface.port",
        default=3000,
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
    next_port = _service_port(
        config,
        env_names=("AOITALK_NEXT_PORT", "AOITALK_FRONTEND_PORT"),
        config_key="frontend.port",
        default=3002,
    )
    caddy_next_port = _service_port(
        config,
        env_names=("AOITALK_CADDY_NEXT_PORT",),
        config_key="caddy.next_port",
        default=next_port,
    )

    if (
        ready_fastapi_port is not None
        and ready_fastapi_port != caddy_fastapi_port
    ):
        raise RuntimeError(
            "FastAPI の実ポートと Caddy の転送先が一致しません: "
            f"ready={ready_fastapi_port}, caddy={caddy_fastapi_port}"
        )

    if not _is_port_open("127.0.0.1", caddy_fastapi_port):
        raise RuntimeError(
            "FastAPI の起動完了前に Caddy を開始しようとしました: "
            f"127.0.0.1:{caddy_fastapi_port}"
        )

    project_root = Path(__file__).resolve().parents[2]
    from src.utils.log_layout import get_log_layout

    layout = get_log_layout(project_root)
    layout.ensure_dirs()
    caddy_access_log = layout.caddy_access_log().resolve()
    caddy_runtime_log = layout.caddy_runtime_log().resolve()
    caddyfile_path = _resolve_caddyfile_path(project_root)
    is_enterprise = Features.is_enterprise()
    caddyfile_snapshot = (
        _read_enterprise_caddyfile_snapshot(caddyfile_path, project_root)
        if is_enterprise
        else None
    )
    caddy_env = {
        **os.environ,
        "AOITALK_CADDY_PORT": str(caddy_port),
        "AOITALK_CADDY_FASTAPI_PORT": str(caddy_fastapi_port),
        "AOITALK_CADDY_NEXT_PORT": str(caddy_next_port),
        "AOITALK_CADDY_ACCESS_LOG": str(caddy_access_log),
        "AOITALK_CADDY_RUNTIME_LOG": str(caddy_runtime_log),
        "AOITALK_CADDY_FASTAPI_UPSTREAM": os.getenv(
            "AOITALK_CADDY_FASTAPI_UPSTREAM",
            f"127.0.0.1:{caddy_fastapi_port}",
        ),
        "AOITALK_CADDY_NEXT_UPSTREAM": os.getenv(
            "AOITALK_CADDY_NEXT_UPSTREAM",
            f"127.0.0.1:{caddy_next_port}",
        ),
    }
    if _IS_WINDOWS:
        caddy_env["AOITALK_CADDY_LOG_ROLL_EXTRA"] = "roll_uncompressed"
    if is_enterprise:
        # Native Enterprise bootstrap is a local-only setup boundary. Do not
        # let a stale parent/.env value turn the localhost site into a wildcard
        # listener. Compose starts Caddy separately and sets its container-side
        # topology explicitly, so this does not affect Docker publication.
        caddy_env["AOITALK_BOOTSTRAP_BIND_ADDRESS"] = "127.0.0.1"
    if _IS_WINDOWS:
        # Windowsでは証明書ファイル名（legoのドメイン名）からサイト名を解決する。
        # 特定ホスト名を起動コードへ固定せず、Linux/Composeの既定も変更しない。
        cert_path = caddy_env.get("AOITALK_CERT_CRT")
        key_path = caddy_env.get("AOITALK_CERT_KEY")
        if not cert_path and not key_path:
            certificate_dir = project_root / "certs" / "certificates"
            certificate_pairs = [
                (certificate, certificate.with_suffix(".key"))
                for certificate in sorted(certificate_dir.glob("*.crt"))
                if certificate.with_suffix(".key").is_file()
            ]
            if len(certificate_pairs) != 1:
                raise RuntimeError(
                    "Windows HTTPS証明書を一意に特定できません。"
                    "AOITALK_CERT_CRTとAOITALK_CERT_KEYを指定してください。"
                )
            cert_path, key_path = map(str, certificate_pairs[0])
        elif not cert_path or not key_path:
            raise RuntimeError(
                "AOITALK_CERT_CRTとAOITALK_CERT_KEYは両方指定してください。"
            )
        caddy_env.setdefault("AOITALK_CADDY_SITE_ADDRESS", Path(cert_path).stem)
        caddy_env.setdefault(
            "AOITALK_CADDY_TLS_DIRECTIVE",
            f"tls {json.dumps(cert_path)} {json.dumps(key_path)}",
        )
        # Windows personalは証明書のSNIを受けつつ、site labelによるHost制約を
        # 公開入口へ持ち込まない。IPv4で全インターフェースを明示的に待ち受ける。
        caddy_env.setdefault(
            "AOITALK_CADDY_SITE_LABEL", f"https://:{caddy_port}"
        )
        caddy_env.setdefault("AOITALK_CADDY_BIND_DIRECTIVE", "bind 0.0.0.0")
    else:
        # Linux nativeは従来どおり名前付きsite labelで自動HTTPSを利用する。
        caddy_env.setdefault(
            "AOITALK_CADDY_SITE_LABEL",
            f"{caddy_env.get('AOITALK_CADDY_SITE_ADDRESS', 'localhost')}:{caddy_port}",
        )
    popen_kwargs: dict[str, object] = {
        "cwd": str(project_root),
        "env": caddy_env,
    }
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        popen_kwargs["start_new_session"] = True

    caddy_args = [_resolve_caddy_binary(project_root), "run", "--config"]
    if caddyfile_snapshot is None:
        caddy_args.append(str(caddyfile_path))
    else:
        caddy_args.extend(["-", "--adapter", "caddyfile"])
        popen_kwargs["stdin"] = subprocess.PIPE

    caddy_proc = subprocess.Popen(caddy_args, **popen_kwargs)
    _track_child_process(caddy_proc)
    if caddyfile_snapshot is not None:
        _write_caddyfile_snapshot(caddy_proc, caddyfile_snapshot)
    with _startup_timer.phase("startup.services.caddy.listener_ready"):
        if not _wait_for_process_port(
            caddy_proc,
            "127.0.0.1",
            caddy_port,
            timeout_seconds=10,
        ):
            _terminate_process_tree(caddy_proc)
            _remove_tracked_process(caddy_proc)
            raise RuntimeError(
                f"Caddy did not start listening on 127.0.0.1:{caddy_port}.\n"
                f"caddy-runtime.log tail:\n{_read_log_tail(caddy_runtime_log)}"
            )

    print(
        f"Started Caddy (PID {caddy_proc.pid}) after FastAPI became ready "
        f"(runtime log: {caddy_runtime_log})"
    )
    return caddy_proc


def _rollback_tracked_processes(start_index: int) -> None:
    """Stop child processes added during a failed startup attempt."""
    for proc in reversed(list(_child_processes[start_index:])):
        _terminate_process_tree(proc)
        _remove_tracked_process(proc)


def _start_services(config: object | None = None) -> None:
    """Start frontend and provider services; Caddy starts after FastAPI is ready."""
    project_root = Path(__file__).resolve().parents[2]
    from src.utils.log_layout import get_log_layout
    from src.utils.log_housekeeping import (
        rotate_frontend_log_if_exists,
        run_log_housekeeping,
    )

    layout = get_log_layout(project_root)
    layout.migrate_legacy_paths()
    run_log_housekeeping(layout)
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
    # 起動できない条件は、稼働中のサービスを止める前に確定させる。
    # 先に kill してから検証で落ちると、既に動いていたサーバーまで巻き添えで
    # 停止したまま復旧されず、Web/API が無人のまま残る。
    #
    # 本番成果物の自己修復・指紋検証もこの起動経路へ接続する。検証用
    # `npm run build` は `.next-verify` へ出力されるため、ここでは必ず
    # `build:production` を使って canonical な `.next` を生成する。
    # 依存修復は成果物を触らないので、既存サービスを止める前に実行する。
    frontend_log_path = layout.frontend_log_path()
    rotate_frontend_log_if_exists(frontend_log_path)
    with _startup_timer.phase("startup.services.frontend.env_build"):
        frontend_env = _build_frontend_env(project_root)
    with _startup_timer.phase("startup.services.frontend.dependencies"):
        _ensure_frontend_dependencies(project_root, frontend_log_path)

    # `_ensure_frontend_build` は stale な canonical `.next` を作り直す際に
    # 旧ディレクトリを削除する。稼働中の `next start` がそのディレクトリを
    # 参照している間に削除しないよう、stale/missing の場合だけ frontend
    # listener を先に停止し、その後 production build を行う。
    frontend_port_killed_for_build = False
    frontend_dir = project_root / "frontend"
    with _startup_timer.phase("startup.services.frontend.rebuild_check"):
        build_reason, _ = _frontend_build_rebuild_reason(frontend_dir)
    if build_reason:
        with _startup_timer.phase("startup.services.frontend.port_cleanup_for_build"):
            _kill_existing_on_port(next_port)
        frontend_port_killed_for_build = True
        with _startup_timer.phase("startup.services.frontend.build"):
            _ensure_frontend_build(project_root, frontend_log_path, frontend_env)
    with _startup_timer.phase("startup.services.frontend.artifact_validation"):
        _validate_frontend_startup_artifacts(project_root)

    with _startup_timer.phase("startup.services.port_cleanup"):
        _kill_existing_on_port(fastapi_port)
        if not skip_caddy:
            _kill_existing_on_port(caddy_port)
            if not frontend_port_killed_for_build:
                _kill_existing_on_port(next_port)

    with _startup_timer.phase("startup.services.next.reuse_probe"):
        frontend_already_running = skip_caddy and _wait_for_port(
            "127.0.0.1", next_port, timeout_seconds=0.5
        )

    with _startup_timer.phase("startup.services.topology.resolve"):
        should_start_exo = _should_start_exo_server(config)
        should_start_mlx_lm = _should_start_mlx_lm_server(config)
        should_start_llama_cpp = _should_start_llama_cpp(config, is_windows=_IS_WINDOWS)
    with _startup_timer.phase("startup.services.provider.ensure"):
        local_server_launch_started = ensure_openai_compatible_local_server(config)

    with _startup_timer.phase("startup.services.frontend.env_finalize"):
        frontend_host = _frontend_bind_host()
        frontend_env["AOITALK_LOGIN_TRUST_PROXY"] = (
            "true"
            if _should_trust_login_proxy(frontend_host, skip_caddy=skip_caddy)
            else "false"
        )

    frontend_proc: subprocess.Popen | None = None
    if frontend_already_running:
        _startup_timer.mark("startup.services.next.listener_ready")
    elif _IS_WINDOWS:
        with _startup_timer.phase("startup.services.next.spawn"):
            frontend_proc = subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                        f"npm run start -- -p {next_port} -H {frontend_host} 2>&1 | "
                        f"ForEach-Object {{ Write-Host $_; Add-Content -Path '{frontend_log_path}' -Value $_ -Encoding UTF8 }}"
                    ),
                ],
                cwd=str(project_root / "frontend"),
                env=frontend_env,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            _track_child_process(frontend_proc)

        with _startup_timer.phase("startup.services.next.socket_poll"):
            if not _wait_for_process_port(
                frontend_proc,
                "127.0.0.1",
                next_port,
                timeout_seconds=45,
            ):
                raise RuntimeError(
                    f"Next.js frontend did not start listening on 127.0.0.1:{next_port}.\n"
                    f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
                )
        _startup_timer.mark("startup.services.next.listener_ready")
    else:
        frontend_cmd = (
            f"set -o pipefail; npm run start -- -p {next_port} -H {frontend_host} 2>&1 | "
            f"tee {str(frontend_log_path)!r}"
        )
        with _startup_timer.phase("startup.services.next.spawn"):
            frontend_proc = subprocess.Popen(
                ["bash", "-c", frontend_cmd],
                cwd=str(project_root / "frontend"),
                env=frontend_env,
                start_new_session=True,
            )
            _track_child_process(frontend_proc)

        with _startup_timer.phase("startup.services.next.socket_poll"):
            if not _wait_for_process_port(
                frontend_proc,
                "127.0.0.1",
                next_port,
                timeout_seconds=45,
            ):
                raise RuntimeError(
                    f"Next.js frontend did not start listening on 127.0.0.1:{next_port}.\n"
                    f"frontend.log tail:\n{_read_log_tail(frontend_log_path)}"
                )
        _startup_timer.mark("startup.services.next.listener_ready")

    caddy_status = (
        "Caddy skipped for desktop"
        if skip_caddy
        else "Caddy waiting for FastAPI readiness"
    )
    frontend_status = (
        f"Frontend (existing on 127.0.0.1:{next_port})"
        if frontend_proc is None
        else f"Frontend (PID {frontend_proc.pid})"
    )

    if should_start_exo and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / exo server starting"
        )
    elif should_start_mlx_lm and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / MLX LM server starting"
        )
    elif should_start_llama_cpp and local_server_launch_started:
        print(
            f"Started {frontend_status} / "
            f"{caddy_status} / llama-server起動中"
        )
    else:
        print(f"Started {frontend_status} / {caddy_status}")


def start_services(config: object | None = None) -> None:
    """Start managed services and roll back every child added on failure."""
    tracked_process_start = len(_child_processes)
    try:
        _start_services(config)
    except Exception:
        _rollback_tracked_processes(tracked_process_start)
        raise


def kill_services() -> None:
    """Stop child service processes started by start_services."""
    for proc in list(_child_processes):
        _terminate_process_tree(proc)
    _child_processes.clear()
    _openai_compatible_local_processes.clear()
