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
import math
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
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import psutil

from ._gateway_activation import restore_activation_secret as _restore_gateway_activation_secret
from ._qdrant_server import (
    managed_qdrant_status,
    start_managed_qdrant,
    stop_managed_qdrant,
)

from src.llm.openai_compatible_local_profiles import (
    EXO_BASE_URL,
    MLX_LM_BASE_URL,
    is_macos,
    local_server_profile_for_model,
    managed_local_runtime_for_model,
    normalize_openai_compatible_base_url,
    openai_compatible_local_base_url,
)
from src.services.strata_runtime import (
    strata_api_key,
    strata_settings,
    strata_base_url,
    strata_launch_plan,
    strata_readiness,
    strata_vision_enabled,
)
from src.features import Features
from src.api.public_base_path import normalize_public_base_path
from src.utils.startup_timing import get_startup_timer
from src.utils.startup_console import startup_console
from src.utils.logging_config import FILE_ONLY_LOG_EXTRA


_startup_timer = get_startup_timer()

from ._process_utils import (
    _IS_WINDOWS,
    _OwnedProcessIdentity,
    _capture_owned_process_tree,
    _canonical_process_path,
    _child_processes,
    _extract_port_from_netstat_address,
    _is_port_open,
    _kill_existing_on_port,
    _listening_pid_for_netstat_line,
    _owned_process_identity_matches,
    _openai_compatible_local_processes,
    _read_owned_process_identity,
    _read_log_tail,
    _remove_tracked_process,
    _service_log_dir,
    _models_log_dir,
    _web_log_dir,
    _supervised_startup_enabled,
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
    _normalize_frontend_tsconfig_for_fingerprint,
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
    _freetoken_base_url,
    _freetoken_launch_plan,
    _freetoken_readiness_ready,
    _freetoken_settings,
    _should_start_freetoken,
    _start_freetoken_server,
    _wait_for_freetoken_readiness,
    freetoken_managed_launch_configured,
    freetoken_managed_launch_configuration_error,
    resolve_freetoken_runtime,
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
    _set_llama_cpp_startup_state,
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
# A managed llama.cpp launch is allowed to take a long time while the model is
# loaded.  The startup path only needs a listener before it can continue; the
# request/generation path performs the stricter /v1/models alias check.
_LLAMA_CPP_STARTUP_LISTENER_TIMEOUT_SECONDS = 10.0
_LOCAL_PROVIDER_STARTUP_LISTENER_TIMEOUT_SECONDS = 10.0
_LLAMA_CPP_LEASE_CONDITION = threading.Condition()
_STRATA_LOAD_LOCK = threading.Lock()
# endpoint -> {"alias": str, "count": int}
_LLAMA_CPP_GENERATION_LEASES: dict[str, dict[str, object]] = {}
_LLAMA_CPP_LEASE_TICKET_SEQ = itertools.count(1)
_LLAMA_CPP_GENERATION_LEASE_TICKETS: dict[int, "LlamaCppGenerationLeaseTicket"] = {}
# A client/session can outlive one request.  Keep a separate consumer lease so
# an engine switch cannot tear down a managed local runtime that another
# session, group, meeting, or agent still owns.  Generation leases remain
# request-scoped and are deliberately kept in the legacy map above for
# compatibility with the existing request lifecycle code/tests.
_LLAMA_CPP_RUNTIME_CONSUMER_LEASES: dict[str, dict[str, object]] = {}
_LLAMA_CPP_RUNTIME_CONSUMER_LEASE_TICKETS: dict[
    int, "ManagedLocalRuntimeConsumerLeaseTicket"
] = {}
_LLAMA_CPP_RUNTIME_CONSUMER_LEASE_SEQ = itertools.count(1)
# endpoint/alias/PID keyed deferred stops created when a committed route
# switch timed out waiting for active consumers.  Records retain the exact
# tracked process object so a replacement PID/process can never be killed.
_LLAMA_CPP_DEFERRED_STOPS: dict[int, dict[str, object]] = {}
_LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE = False
_LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN_SEQ = itertools.count(1)
_LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN: int | None = None


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


def _runtime_consumer_lease_conflict(
    endpoint: str,
    alias: str,
    *,
    consumer: str = "consumer",
) -> RuntimeError:
    current_generation_alias, generation_count = _peek_llama_cpp_generation_lease(
        endpoint
    )
    current_consumer_alias, consumer_count = _peek_llama_cpp_runtime_consumer_lease(
        endpoint
    )
    active_alias = current_generation_alias or current_consumer_alias
    active_count = generation_count + consumer_count
    return RuntimeError(
        "managed local runtime is in use by another consumer; "
        f"endpoint={endpoint!r}, active_alias={active_alias!r}, "
        f"requested_alias={alias!r}, active_count={active_count}, "
        f"consumer={consumer!r}"
    )


def _llama_cpp_generation_lease_timeout(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
) -> float:
    selected_model = str(
        model or _openai_compatible_local_model_id(config) or ""
    ).strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model,
    )
    if managed_runtime == "strata":
        return float(strata_settings(config, model=selected_model)["readiness_timeout"])
    if managed_runtime == "freetoken":
        settings = _freetoken_settings(
            config,
            model=selected_model,
            is_windows=_IS_WINDOWS,
        )
    else:
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

    selected_model = str(
        model or _openai_compatible_local_model_id(config) or ""
    ).strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model,
    )
    if managed_runtime == "strata":
        settings = strata_settings(config, model=selected_model)
        if not settings["auto_start"]:
            return None
        return strata_base_url(config, model=selected_model), selected_model

    if managed_runtime == "freetoken":
        if not _should_start_freetoken(
            config,
            model=selected_model,
            is_windows=_IS_WINDOWS,
        ):
            return None
        settings = _freetoken_settings(
            config,
            model=selected_model,
            is_windows=_IS_WINDOWS,
        )
        alias = str(settings.get("model_alias") or "").strip()
        endpoint = _normalize_llama_cpp_lease_endpoint(
            _freetoken_base_url(
                config,
                model=selected_model,
                is_windows=_IS_WINDOWS,
            )
        )
        return (endpoint, alias) if endpoint and alias else None

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


def _peek_llama_cpp_runtime_consumer_lease(endpoint: str) -> tuple[str, int]:
    """Return the alias/count held by non-request local runtime consumers."""

    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    with _LLAMA_CPP_LEASE_CONDITION:
        lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(normalized)
        if not lease:
            return "", 0
        return str(lease.get("alias") or ""), int(lease.get("count") or 0)


def _peek_llama_cpp_lifecycle_lease(endpoint: str) -> tuple[str, int]:
    """Return all request and consumer leases protecting one endpoint."""

    generation_alias, generation_count = _peek_llama_cpp_generation_lease(endpoint)
    consumer_alias, consumer_count = _peek_llama_cpp_runtime_consumer_lease(endpoint)
    return (
        generation_alias or consumer_alias,
        generation_count + consumer_count,
    )


def _iter_owned_llama_cpp_lease_endpoints() -> list[str]:
    endpoints: list[str] = []
    seen: set[str] = set()
    for proc in list(_openai_compatible_local_processes):
        recorded = (
            getattr(proc, "_aoi_local_base_url", None)
            or getattr(proc, "_aoi_llama_cpp_base_url", None)
        )
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
        consumer_alias, consumer_count = _peek_llama_cpp_runtime_consumer_lease(
            endpoint
        )
        active_alias = current_alias or consumer_alias
        if force_restart:
            candidates.append(endpoint)
            candidates.extend(owned)
        elif (count > 0 or consumer_count > 0) and active_alias != alias:
            candidates.append(endpoint)
    elif force_restart:
        candidates.extend(owned)
    for candidate in candidates:
        _count_alias, count = _peek_llama_cpp_generation_lease(candidate)
        _consumer_alias, consumer_count = _peek_llama_cpp_runtime_consumer_lease(
            candidate
        )
        if count > 0 or consumer_count > 0:
            _add(candidate)
    return endpoints


def _check_local_startup_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Managed local model startup was cancelled")


@contextmanager
def _llama_cpp_startup_lock(cancel_event):
    if cancel_event is None:
        with _LLAMA_CPP_ENSURE_LOCK:
            yield
        return
    while True:
        _check_local_startup_cancelled(cancel_event)
        if _LLAMA_CPP_ENSURE_LOCK.acquire(timeout=0.1):
            break
    try:
        _check_local_startup_cancelled(cancel_event)
        yield
    finally:
        _LLAMA_CPP_ENSURE_LOCK.release()


def _wait_for_llama_cpp_generation_lease_idle(
    endpoint: str,
    timeout_seconds: float,
    *,
    cancel_event=None,
) -> None:
    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    with _LLAMA_CPP_LEASE_CONDITION:
        while True:
            _check_local_startup_cancelled(cancel_event)
            generation_lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
            consumer_lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(normalized)
            generation_count = int((generation_lease or {}).get("count") or 0)
            consumer_count = int((consumer_lease or {}).get("count") or 0)
            count = generation_count + consumer_count
            if count <= 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current_alias = str(
                    (generation_lease or {}).get("alias")
                    or (consumer_lease or {}).get("alias")
                    or ""
                )
                raise RuntimeError(
                    "llama.cppの生成lease待ちがtimeoutしました。"
                    f"endpoint={normalized!r}, 使用中alias={current_alias!r}, "
                    f"使用中count={count}。"
                    "実行中の生成は停止せず、切替を中止しました。"
                )
            _LLAMA_CPP_LEASE_CONDITION.wait(timeout=min(remaining, 0.1) if cancel_event is not None else remaining)


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


class ManagedLocalRuntimeConsumerLeaseTicket:
    """Idempotent lease for a client/session that may outlive one request.

    The ticket is intentionally independent from the request generation lease:
    a session can retain a target client while idle, and a switch must keep
    that target's managed process alive until the owner explicitly cleans it
    up.  ``consumer`` is diagnostic metadata only; the endpoint/alias fence is
    the lifecycle identity.
    """

    __slots__ = (
        "endpoint",
        "alias",
        "consumer",
        "ticket_id",
        "_released",
        "_holder",
    )

    def __init__(
        self,
        endpoint: str,
        alias: str,
        consumer: str,
        ticket_id: int,
    ) -> None:
        self.endpoint = endpoint
        self.alias = alias
        self.consumer = consumer
        self.ticket_id = ticket_id
        self._released = False
        self._holder = None

    def release(self) -> None:
        _release_llama_cpp_runtime_consumer_lease(self)


def _register_llama_cpp_runtime_consumer_ticket(
    holder: object,
    ticket: ManagedLocalRuntimeConsumerLeaseTicket,
) -> None:
    tickets = getattr(holder, "_llama_cpp_runtime_consumer_lease_tickets", None)
    if not isinstance(tickets, set):
        tickets = set()
        setattr(holder, "_llama_cpp_runtime_consumer_lease_tickets", tickets)
    tickets.add(ticket)
    ticket._holder = holder


def _unregister_llama_cpp_runtime_consumer_ticket(
    ticket: ManagedLocalRuntimeConsumerLeaseTicket,
) -> None:
    holder = ticket._holder
    ticket._holder = None
    if holder is None:
        return
    tickets = getattr(holder, "_llama_cpp_runtime_consumer_lease_tickets", None)
    if isinstance(tickets, set):
        tickets.discard(ticket)


def _acquire_llama_cpp_runtime_consumer_lease(
    endpoint: str,
    alias: str,
    *,
    consumer: str = "consumer",
    holder: object | None = None,
) -> ManagedLocalRuntimeConsumerLeaseTicket:
    """Protect a managed local endpoint for one long-lived consumer.

    The same lifecycle lock used by ensure/stop closes the check-to-acquire
    race.  A different alias cannot share one endpoint while either a request
    or another consumer still owns it.
    """

    normalized = _normalize_llama_cpp_lease_endpoint(endpoint)
    target_alias = str(alias or "").strip()
    target_consumer = str(consumer or "consumer").strip() or "consumer"
    if not normalized or not target_alias:
        raise ValueError("managed local runtime lease requires endpoint and alias")
    with _LLAMA_CPP_ENSURE_LOCK:
        with _LLAMA_CPP_LEASE_CONDITION:
            generation_alias, generation_count = _peek_llama_cpp_generation_lease(
                normalized
            )
            consumer_alias, consumer_count = _peek_llama_cpp_runtime_consumer_lease(
                normalized
            )
            active_alias = generation_alias or consumer_alias
            active_count = generation_count + consumer_count
            if active_count > 0 and active_alias != target_alias:
                raise _runtime_consumer_lease_conflict(
                    normalized,
                    target_alias,
                    consumer=target_consumer,
                )
            ticket_id = next(_LLAMA_CPP_RUNTIME_CONSUMER_LEASE_SEQ)
            ticket = ManagedLocalRuntimeConsumerLeaseTicket(
                normalized,
                target_alias,
                target_consumer,
                ticket_id,
            )
            lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.setdefault(
                normalized,
                {"alias": target_alias, "count": 0, "consumers": {}},
            )
            lease["alias"] = target_alias
            lease["count"] = int(lease.get("count") or 0) + 1
            consumers = lease.setdefault("consumers", {})
            if isinstance(consumers, dict):
                consumers[target_consumer] = int(consumers.get(target_consumer) or 0) + 1
            _LLAMA_CPP_RUNTIME_CONSUMER_LEASE_TICKETS[ticket_id] = ticket
            if holder is not None:
                _register_llama_cpp_runtime_consumer_ticket(holder, ticket)
            _LLAMA_CPP_LEASE_CONDITION.notify_all()
            return ticket


def _acquire_managed_local_runtime_consumer_for_route(
    config: object | None,
    *,
    provider: str,
    model: str,
    consumer: str,
    holder: object | None = None,
) -> ManagedLocalRuntimeConsumerLeaseTicket | None:
    """Acquire a consumer lease from a provider/model route overlay.

    Raw group/meeting/specialist transports often carry a route separate from
    the persisted Main provider.  Overlay the route before resolving the
    managed identity so a global cloud setting cannot accidentally make a
    local meeting target look unowned.
    """

    if str(provider or "").strip().lower() != "openai_compatible_local":
        return None
    model_id = str(model or "").strip()
    if not model_id:
        return None
    candidate = config
    try:
        from src.llm.manager import TargetConfig

        candidate = TargetConfig(
            config,
            {
                "llm_provider": "openai_compatible_local",
                "llm_model": model_id,
                "openai_compatible_local.model": model_id,
            },
        )
    except Exception:
        pass
    identity = _llama_cpp_generation_lease_identity(candidate, model=model_id)
    if identity is None:
        return None
    return _acquire_llama_cpp_runtime_consumer_lease(
        identity[0],
        identity[1],
        consumer=consumer,
        holder=holder,
    )


def _release_llama_cpp_runtime_consumer_lease(
    ticket: ManagedLocalRuntimeConsumerLeaseTicket | None,
) -> None:
    if ticket is None:
        return
    released = False
    with _LLAMA_CPP_LEASE_CONDITION:
        if ticket._released:
            return
        ticket._released = True
        _LLAMA_CPP_RUNTIME_CONSUMER_LEASE_TICKETS.pop(ticket.ticket_id, None)
        _unregister_llama_cpp_runtime_consumer_ticket(ticket)
        lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(ticket.endpoint)
        if not lease:
            _LLAMA_CPP_LEASE_CONDITION.notify_all()
        else:
            count = int(lease.get("count") or 0) - 1
            consumers = lease.get("consumers")
            if isinstance(consumers, dict):
                consumer_count = int(consumers.get(ticket.consumer) or 0) - 1
                if consumer_count <= 0:
                    consumers.pop(ticket.consumer, None)
                else:
                    consumers[ticket.consumer] = consumer_count
            if count <= 0:
                _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.pop(ticket.endpoint, None)
            else:
                lease["count"] = count
        _LLAMA_CPP_LEASE_CONDITION.notify_all()
        released = True
    if released:
        _drain_deferred_llama_cpp_stops(non_blocking=True)


def _release_llama_cpp_runtime_consumer_leases_for_holder(
    holder: object | None,
    *,
    all: bool = False,
) -> None:
    if holder is None:
        return
    with _LLAMA_CPP_LEASE_CONDITION:
        tickets = getattr(holder, "_llama_cpp_runtime_consumer_lease_tickets", None)
        if not isinstance(tickets, set) or not tickets:
            return
        pending = list(tickets)
    if not all:
        pending = pending[:1]
    for ticket in pending:
        ticket.release()


def _reset_llama_cpp_runtime_consumer_leases() -> None:
    with _LLAMA_CPP_LEASE_CONDITION:
        for ticket in list(_LLAMA_CPP_RUNTIME_CONSUMER_LEASE_TICKETS.values()):
            ticket._released = True
            ticket._holder = None
        _LLAMA_CPP_RUNTIME_CONSUMER_LEASE_TICKETS.clear()
        _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.clear()
        _LLAMA_CPP_DEFERRED_STOPS.clear()
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


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
    released = False
    with _LLAMA_CPP_LEASE_CONDITION:
        if ticket._released:
            return
        ticket._released = True
        _LLAMA_CPP_GENERATION_LEASE_TICKETS.pop(ticket.ticket_id, None)
        _unregister_llama_cpp_generation_lease_ticket(ticket)
        lease = _LLAMA_CPP_GENERATION_LEASES.get(ticket.endpoint)
        if not lease:
            _LLAMA_CPP_LEASE_CONDITION.notify_all()
        else:
            count = int(lease.get("count") or 0) - 1
            if count <= 0:
                _LLAMA_CPP_GENERATION_LEASES.pop(ticket.endpoint, None)
            else:
                lease["count"] = count
        _LLAMA_CPP_LEASE_CONDITION.notify_all()
        released = True
    if released:
        _drain_deferred_llama_cpp_stops(non_blocking=True)


def _reset_llama_cpp_generation_leases() -> None:
    with _LLAMA_CPP_LEASE_CONDITION:
        for ticket in list(_LLAMA_CPP_GENERATION_LEASE_TICKETS.values()):
            ticket._released = True
            ticket._holder = None
        _LLAMA_CPP_GENERATION_LEASE_TICKETS.clear()
        _LLAMA_CPP_GENERATION_LEASES.clear()
        _LLAMA_CPP_DEFERRED_STOPS.clear()
        _LLAMA_CPP_LEASE_CONDITION.notify_all()
    # Test/setup reset historically covered the whole process-local lifecycle
    # ledger.  Keep that invariant after adding long-lived consumer leases.
    _reset_llama_cpp_runtime_consumer_leases()


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
            generation_lease = _LLAMA_CPP_GENERATION_LEASES.get(normalized)
            consumer_lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(normalized)
            count = int((generation_lease or {}).get("count") or 0) + int(
                (consumer_lease or {}).get("count") or 0
            )
            if count > 0:
                raise _LlamaCppGenerationLeaseBusy(
                    normalized,
                    str(
                        (generation_lease or {}).get("alias")
                        or (consumer_lease or {}).get("alias")
                        or ""
                    ),
                    count,
                )
        # This helper is used while replacing one endpoint. Keep the stop
        # scoped to that endpoint so a different local runtime/consumer cannot
        # be killed as a side effect of an alias change.
        processes = _owned_processes_for_scope(endpoint=normalized)
        if not processes:
            # Legacy tracked doubles/processes may lack endpoint metadata. A
            # single such process is still an unambiguous compatibility
            # target; multiple unannotated processes are left running rather
            # than guessed across endpoints.
            unannotated = [
                process
                for process in list(_openai_compatible_local_processes)
                if not str(
                    getattr(process, "_aoi_local_base_url", None)
                    or getattr(process, "_aoi_llama_cpp_base_url", "")
                    or ""
                ).strip()
            ]
            if len(unannotated) == 1:
                # An unknown worker cannot be mapped to the requested
                # endpoint. Never use the broad legacy stop while any other
                # endpoint has an active request/consumer lease.
                has_other_active_lease = bool(_busy_llama_cpp_generation_leases())
                if (
                    len(_openai_compatible_local_processes) == 1
                    and not has_other_active_lease
                ):
                    # Preserve the legacy low-level hook for one unannotated
                    # tracked worker (the only process that can be identified
                    # safely in this compatibility shape).
                    stop_openai_compatible_local_servers()
                    return
        for process in processes:
            _terminate_process_tree(process)
            _remove_tracked_process(process)


_NEXT_PROCESS_TREE_ATTR = "_aoi_next_process_tree"
_NEXT_CAPTURE_PENDING = object()
_NEXT_CAPTURE_FAILED = object()


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    next_tree = getattr(proc, "_aoi_next_process_tree", None)
    if _IS_WINDOWS and next_tree is not None:
        if next_tree is _NEXT_CAPTURE_PENDING or next_tree is _NEXT_CAPTURE_FAILED:
            logger.warning(
                "Next.js process-tree identity capture is incomplete; cleanup refused"
            )
            return
        if not isinstance(next_tree, tuple) or not _terminate_validated_next_tree(next_tree):
            logger.warning(
                "Next.js process-tree identity changed; cleanup refused"
            )
        return

    # A Windows llama.cpp launch keeps stdout file-only for CUDA stability and
    # may have a separate visible PowerShell tail console.  Stop that console
    # with its worker so an engine switch never leaves an orphan window.
    companion = getattr(proc, "_aoi_llama_cpp_console_process", None)
    if companion is not None and companion is not proc:
        try:
            setattr(proc, "_aoi_llama_cpp_console_process", None)
        except Exception:
            pass
        try:
            _terminate_process_tree(companion)
        finally:
            _remove_tracked_process(companion)
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
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def _record_next_process_tree(
    proc: subprocess.Popen,
    *,
    frontend_dir: Path,
    next_port: int,
    frontend_host: str,
    require_descendants: bool = True,
) -> None:
    """Record exact Windows Next identities before and after readiness."""
    # Unit tests use light-weight Popen doubles.  A real Windows Popen has a
    # native handle; production capture is mandatory, while doubles keep the
    # existing startup tests independent of the host process table.
    if not _IS_WINDOWS or not hasattr(proc, "_handle"):
        return
    previous = getattr(proc, _NEXT_PROCESS_TREE_ATTR, None)
    setattr(proc, _NEXT_PROCESS_TREE_ATTR, _NEXT_CAPTURE_PENDING)
    try:
        records = _capture_owned_process_tree(proc)
        if not records:
            raise RuntimeError("Next.js frontend process identityを取得できません")

        expected_cwd = _canonical_process_path(frontend_dir)
        if records[0].cwd != expected_cwd:
            raise RuntimeError("Next.js frontend launcherのcwdを検証できません")

        if isinstance(previous, tuple) and not _owned_process_identity_matches(
            previous[0], records[0]
        ):
            raise RuntimeError("Next.js frontend launcherのidentityが起動後に変化しました")

        host_marker = str(frontend_host).casefold()
        port_marker = str(int(next_port))
        descendants = records[1:]
        if not descendants:
            if require_descendants:
                raise RuntimeError("Next.js frontend descendantを取得できません")
            setattr(proc, _NEXT_PROCESS_TREE_ATTR, records)
            return
        for identity in descendants:
            if Path(identity.executable).name.casefold() == "conhost.exe":
                # CREATE_NEW_CONSOLE may add a Windows console host whose cwd
                # and command line are unrelated to Next.  It is still
                # recorded and revalidated by exact identity.
                continue
            command = " ".join(identity.command_line).casefold()
            if (
                identity.cwd != expected_cwd
                or port_marker not in command
                or host_marker not in command
                or not any(token in command for token in ("next", "npm"))
            ):
                raise RuntimeError(
                    "Next.js frontend descendantの実行identityを検証できません"
                )
        setattr(proc, _NEXT_PROCESS_TREE_ATTR, records)
    except BaseException:
        setattr(proc, _NEXT_PROCESS_TREE_ATTR, _NEXT_CAPTURE_FAILED)
        raise


def _validated_next_tree(
    records: tuple[_OwnedProcessIdentity, ...],
) -> dict[int, _OwnedProcessIdentity] | None:
    """Return live members only when all recorded and current descendants agree."""
    known_pids = {identity.pid for identity in records}
    current: dict[int, _OwnedProcessIdentity] = {}
    for expected in records:
        observed = _read_owned_process_identity(expected.pid)
        if observed is None:
            try:
                if psutil.pid_exists(expected.pid):
                    return None
            except (psutil.Error, OSError):
                return None
            continue
        if not _owned_process_identity_matches(expected, observed):
            return None
        if observed.parent_pid != expected.parent_pid:
            return None
        current[expected.pid] = observed

    # A tracked process may have spawned a child after the readiness snapshot.
    # Refuse the whole operation instead of allowing an exact parent kill to
    # take an unrecorded process with it.
    for identity in current.values():
        try:
            descendants = psutil.Process(identity.pid).children(recursive=True)
        except (psutil.Error, OSError):
            return None
        if any(int(child.pid) not in known_pids for child in descendants):
            return None
    return current


def _next_identity_depth(
    identity: _OwnedProcessIdentity,
    by_pid: dict[int, _OwnedProcessIdentity],
) -> int:
    depth = 0
    parent = identity.parent_pid
    seen: set[int] = set()
    while parent in by_pid and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = by_pid[parent].parent_pid
    return depth


def _terminate_exact_next_identity(identity: _OwnedProcessIdentity) -> bool:
    """Terminate one already-validated PID without tree/name/port selection."""
    observed = _read_owned_process_identity(identity.pid)
    if observed is None:
        try:
            return not psutil.pid_exists(identity.pid)
        except (psutil.Error, OSError):
            return False
    if not _owned_process_identity_matches(identity, observed):
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(identity.pid), "/F"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observed = _read_owned_process_identity(identity.pid)
        if observed is None:
            try:
                return not psutil.pid_exists(identity.pid)
            except (psutil.Error, OSError):
                return False
        if not _owned_process_identity_matches(identity, observed):
            return False
        time.sleep(0.05)
    return False


def _terminate_validated_next_tree(
    records: tuple[_OwnedProcessIdentity, ...],
) -> bool:
    """Clean a validated Next tree using exact leaf-to-root actions."""
    current = _validated_next_tree(records)
    if current is None:
        return False
    by_pid = {identity.pid: identity for identity in records}
    live_members = list(current.values())
    live_members.sort(
        key=lambda identity: _next_identity_depth(identity, by_pid),
        reverse=True,
    )
    for identity in live_members:
        if not _terminate_exact_next_identity(identity):
            return False
    return _validated_next_tree(records) == {}


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
    endpoints.update(_LLAMA_CPP_RUNTIME_CONSUMER_LEASES.keys())
    busy: list[tuple[str, str, int]] = []
    for endpoint in endpoints:
        generation_lease = _LLAMA_CPP_GENERATION_LEASES.get(endpoint)
        consumer_lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(endpoint)
        count = int((generation_lease or {}).get("count") or 0) + int(
            (consumer_lease or {}).get("count") or 0
        )
        if count > 0:
            busy.append(
                (
                    endpoint,
                    str(
                        (generation_lease or {}).get("alias")
                        or (consumer_lease or {}).get("alias")
                        or ""
                    ),
                    count,
                )
            )
    return busy


def _register_deferred_llama_cpp_stops(processes: list[object]) -> None:
    """Remember exact owned workers for a committed switch timeout."""

    with _LLAMA_CPP_LEASE_CONDITION:
        for process in processes:
            endpoint = _normalize_llama_cpp_lease_endpoint(
                str(
                    getattr(process, "_aoi_local_base_url", None)
                    or getattr(process, "_aoi_llama_cpp_base_url", "")
                    or ""
                )
            )
            alias = str(
                getattr(process, "_aoi_local_model_alias", "") or ""
            ).strip()
            runtime = str(getattr(process, "_aoi_local_runtime", "") or "").strip()
            try:
                pid = int(getattr(process, "pid"))
            except (TypeError, ValueError):
                pid = 0
            if not endpoint or not alias or pid <= 0:
                continue
            _LLAMA_CPP_DEFERRED_STOPS[id(process)] = {
                "process": process,
                "endpoint": endpoint,
                "alias": alias,
                "runtime": runtime,
                "pid": pid,
            }
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


def _cancel_deferred_llama_cpp_stops(
    *,
    endpoint: str | None = None,
    alias: str | None = None,
) -> int:
    normalized = (
        _normalize_llama_cpp_lease_endpoint(endpoint)
        if endpoint
        else ""
    )
    selected_alias = str(alias or "").strip()
    with _LLAMA_CPP_LEASE_CONDITION:
        removed = 0
        for key, record in list(_LLAMA_CPP_DEFERRED_STOPS.items()):
            if normalized and record.get("endpoint") != normalized:
                continue
            if selected_alias and record.get("alias") != selected_alias:
                continue
            _LLAMA_CPP_DEFERRED_STOPS.pop(key, None)
            removed += 1
        _LLAMA_CPP_LEASE_CONDITION.notify_all()
        return removed


def _deferred_llama_cpp_stop_ready_locked() -> bool:
    return any(
        int(
            (
                _LLAMA_CPP_GENERATION_LEASES.get(
                    str(record.get("endpoint") or "")
                )
                or {}
            ).get("count")
            or 0
        )
        + int(
            (
                _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(
                    str(record.get("endpoint") or "")
                )
                or {}
            ).get("count")
            or 0
        )
        <= 0
        for record in _LLAMA_CPP_DEFERRED_STOPS.values()
    )


def _finish_deferred_llama_cpp_drain_worker(token: int) -> None:
    """Clear the scheduler state only for the worker that owns ``token``."""

    global _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE
    global _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN
    with _LLAMA_CPP_LEASE_CONDITION:
        if _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN != token:
            return
        _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE = False
        _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN = None
        _LLAMA_CPP_LEASE_CONDITION.notify_all()


def _schedule_deferred_llama_cpp_drain() -> None:
    """Run one deferred cleanup after a busy lifecycle lock is released."""

    global _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE
    global _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN
    with _LLAMA_CPP_LEASE_CONDITION:
        if _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE:
            return
        if not _LLAMA_CPP_DEFERRED_STOPS:
            return
        _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE = True
        worker_token = next(_LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN_SEQ)
        _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN = worker_token

    def _run() -> None:
        try:
            while True:
                _drain_deferred_llama_cpp_stops()
                with _LLAMA_CPP_LEASE_CONDITION:
                    should_finish = (
                        not _LLAMA_CPP_DEFERRED_STOPS
                        or not _deferred_llama_cpp_stop_ready_locked()
                    )
                    if should_finish:
                        # Keep the decision and state transition in one
                        # condition critical section. Otherwise a final
                        # lease release can observe ACTIVE=True in the gap,
                        # decline to schedule a successor, and then have
                        # this worker clear ACTIVE with pending work left.
                        _finish_deferred_llama_cpp_drain_worker(worker_token)
                        return
        finally:
            _finish_deferred_llama_cpp_drain_worker(worker_token)

    try:
        threading.Thread(
            target=_run,
            daemon=True,
            name="llama-cpp-deferred-stop",
        ).start()
    except Exception:
        with _LLAMA_CPP_LEASE_CONDITION:
            if _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN == worker_token:
                _LLAMA_CPP_DEFERRED_DRAIN_WORKER_ACTIVE = False
                _LLAMA_CPP_DEFERRED_DRAIN_WORKER_TOKEN = None
        logger.debug("deferred llama.cpp stop scheduling failed", exc_info=True)


def _drain_deferred_llama_cpp_stops(*, non_blocking: bool = False) -> int:
    """Stop pending exact workers after their final lease releases.

    A lease release updates its ledger before entering this function.  The
    fast path therefore avoids the lifecycle lock when no deferred worker is
    ready or another lease still protects every pending endpoint.  If the
    final release races an ensure that is loading another model, release
    remains non-blocking and a single one-shot cleanup worker drains later.
    """

    with _LLAMA_CPP_LEASE_CONDITION:
        if not _LLAMA_CPP_DEFERRED_STOPS:
            return 0
        if not _deferred_llama_cpp_stop_ready_locked():
            return 0

    if non_blocking:
        acquired = _LLAMA_CPP_ENSURE_LOCK.acquire(blocking=False)
        if not acquired:
            _schedule_deferred_llama_cpp_drain()
            return 0
    else:
        _LLAMA_CPP_ENSURE_LOCK.acquire()

    pending: list[object] = []
    try:
        with _LLAMA_CPP_LEASE_CONDITION:
            for key, record in list(_LLAMA_CPP_DEFERRED_STOPS.items()):
                endpoint = str(record.get("endpoint") or "")
                generation = _LLAMA_CPP_GENERATION_LEASES.get(endpoint)
                consumer = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(endpoint)
                if int((generation or {}).get("count") or 0) + int(
                    (consumer or {}).get("count") or 0
                ) > 0:
                    continue
                process = record.get("process")
                if process not in _openai_compatible_local_processes:
                    _LLAMA_CPP_DEFERRED_STOPS.pop(key, None)
                    continue
                try:
                    pid = int(getattr(process, "pid"))
                    alive = process.poll() is None
                except Exception:
                    pid = 0
                    alive = False
                metadata_endpoint = _normalize_llama_cpp_lease_endpoint(
                    str(
                        getattr(process, "_aoi_local_base_url", None)
                        or getattr(process, "_aoi_llama_cpp_base_url", "")
                        or ""
                    )
                )
                metadata_alias = str(
                    getattr(process, "_aoi_local_model_alias", "") or ""
                ).strip()
                if (
                    not alive
                    or pid != int(record.get("pid") or 0)
                    or metadata_endpoint != endpoint
                    or metadata_alias != str(record.get("alias") or "")
                ):
                    _LLAMA_CPP_DEFERRED_STOPS.pop(key, None)
                    continue
                _LLAMA_CPP_DEFERRED_STOPS.pop(key, None)
                pending.append(process)
        for process in pending:
            _terminate_process_tree(process)
            _remove_tracked_process(process)
        return len(pending)
    finally:
        _LLAMA_CPP_ENSURE_LOCK.release()


def _owned_processes_for_scope(
    *,
    runtime: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> list:
    normalized_endpoint = (
        _normalize_llama_cpp_lease_endpoint(endpoint)
        if endpoint
        else ""
    )
    selected_runtime = str(runtime or "").strip()
    selected_model = str(model or "").strip()
    result = []
    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            continue
        if selected_runtime and str(getattr(proc, "_aoi_local_runtime", "") or "") != selected_runtime:
            continue
        if selected_model and str(getattr(proc, "_aoi_local_model_alias", "") or "") != selected_model:
            continue
        recorded = str(
            getattr(proc, "_aoi_local_base_url", None)
            or getattr(proc, "_aoi_llama_cpp_base_url", "")
            or ""
        )
        if normalized_endpoint and _normalize_llama_cpp_lease_endpoint(recorded) != normalized_endpoint:
            continue
        result.append(proc)
    return result


def stop_owned_openai_compatible_local_servers_respecting_generation_leases(
    *,
    timeout_seconds: float | None = None,
    runtime: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    defer_on_timeout: bool = False,
) -> int:
    """Stop owned llama.cpp servers only after generation leases are idle.

    Usual operations (global engine switch / compensation) must use this
    helper.  Timeout leaves processes running and raises RuntimeError.
    """
    scoped = bool(runtime or model or endpoint)
    timeout = (
        _llama_cpp_generation_lease_timeout(None)
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    deadline = time.monotonic() + max(0.0, timeout)

    def _lease_snapshot() -> tuple[list[tuple[str, str, int]], list]:
        """Read the busy ledger and scoped process set under the condition lock."""

        if not scoped:
            return _busy_llama_cpp_generation_leases(), []
        processes = _owned_processes_for_scope(
            runtime=runtime,
            model=model,
            endpoint=endpoint,
        )
        endpoints = {
            _normalize_llama_cpp_lease_endpoint(
                str(
                    getattr(proc, "_aoi_local_base_url", None)
                    or getattr(proc, "_aoi_llama_cpp_base_url", "")
                    or ""
                )
            )
            for proc in processes
        }
        if endpoint:
            endpoints.add(_normalize_llama_cpp_lease_endpoint(endpoint))
        # A request lease can outlive process metadata (for example while a
        # raw transport is racing a restart). Include lease-ledger endpoints
        # for the requested alias without broadening to other local models.
        if model:
            selected_alias = str(model).strip()
            for candidate, lease in {
                **_LLAMA_CPP_GENERATION_LEASES,
                **_LLAMA_CPP_RUNTIME_CONSUMER_LEASES,
            }.items():
                if str((lease or {}).get("alias") or "").strip() == selected_alias:
                    endpoints.add(candidate)
        busy = []
        for candidate in endpoints:
            generation_lease = _LLAMA_CPP_GENERATION_LEASES.get(candidate)
            consumer_lease = _LLAMA_CPP_RUNTIME_CONSUMER_LEASES.get(candidate)
            count = int((generation_lease or {}).get("count") or 0) + int(
                (consumer_lease or {}).get("count") or 0
            )
            if count > 0:
                busy.append(
                    (
                        candidate,
                        str(
                            (generation_lease or {}).get("alias")
                            or (consumer_lease or {}).get("alias")
                            or ""
                        ),
                        count,
                    )
                )
        return busy, processes

    # Waiting must release the lifecycle lock.  A lease ticket releases the
    # condition first and then drains deferred workers under this same lock;
    # holding it while waiting would deadlock generation->consumer teardown.
    while True:
        with _LLAMA_CPP_LEASE_CONDITION:
            busy, processes = _lease_snapshot()
            if busy:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    _LLAMA_CPP_LEASE_CONDITION.wait(timeout=remaining)
                    continue

        # Re-check after acquiring the lifecycle lock.  This preserves the
        # idle-check -> process-stop boundary while allowing lease release and
        # deferred cleanup to make progress during the wait above.
        with _LLAMA_CPP_ENSURE_LOCK:
            with _LLAMA_CPP_LEASE_CONDITION:
                busy, processes = _lease_snapshot()
                if busy:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        continue
                    if defer_on_timeout:
                        if scoped:
                            deferred_processes = list(processes)
                        else:
                            busy_endpoints = {item[0] for item in busy}
                            deferred_processes = [
                                process
                                for process in list(_openai_compatible_local_processes)
                                if _normalize_llama_cpp_lease_endpoint(
                                    str(
                                        getattr(process, "_aoi_local_base_url", None)
                                        or getattr(process, "_aoi_llama_cpp_base_url", "")
                                        or ""
                                    )
                                ) in busy_endpoints
                            ]
                        _register_deferred_llama_cpp_stops(deferred_processes)
                    endpoint, alias, count = busy[0]
                    raise _LlamaCppGenerationLeaseTimeout(
                        "llama.cppの生成lease待ちがtimeoutしました。"
                        f"endpoint={endpoint!r}, 使用中alias={alias!r}, "
                        f"使用中count={count}。"
                        "実行中の生成は停止せず、切替を中止しました。"
                    )

            if not scoped:
                return stop_openai_compatible_local_servers()
            # The scoped process snapshot was taken while holding the
            # lifecycle lock, so replacement cannot invalidate it before stop.
            if not processes:
                # A scoped stop with no tracked workers is a harmless no-op in
                # production. Preserve the low-level seam for legacy/test
                # callers while never broadening a real multi-process stop.
                if not _openai_compatible_local_processes:
                    return stop_openai_compatible_local_servers()
                unannotated = [
                    process
                    for process in list(_openai_compatible_local_processes)
                    if not str(
                        getattr(process, "_aoi_local_base_url", None)
                        or getattr(process, "_aoi_llama_cpp_base_url", "")
                        or ""
                    ).strip()
                ]
                if len(unannotated) == 1 and len(_openai_compatible_local_processes) == 1:
                    return stop_openai_compatible_local_servers()
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


def _llama_cpp_launch_args(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> list[str]:
    """Facade wrapper that lets tests patch _IS_WINDOWS."""

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


def _owned_llama_cpp_process_at(
    base_url: str,
    expected_alias: str,
) -> object | None:
    """Return a live AoiTalk-owned llama.cpp process for an exact target.

    The persisted runtime settings are mutable (for example during a model
    hot-switch), so ownership must be established from metadata captured on
    the tracked process itself.  Endpoint comparison uses the same canonical
    normalizer as the rest of the local OpenAI-compatible runtime; aliases are
    intentionally compared exactly because ``/v1/models`` alias matching is
    case-sensitive.
    """

    normalized_endpoint = normalize_openai_compatible_base_url(
        str(base_url or "").strip()
    )
    target_alias = str(expected_alias or "").strip()
    if not normalized_endpoint or not target_alias:
        return None

    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            # A process whose liveness cannot be established is not trusted
            # for ownership decisions.
            continue
        if str(getattr(proc, "_aoi_local_runtime", "") or "") != "llama_cpp":
            continue
        recorded_endpoint = str(
            getattr(proc, "_aoi_local_base_url", None)
            or getattr(proc, "_aoi_llama_cpp_base_url", "")
            or ""
        ).strip()
        if not recorded_endpoint:
            continue
        if (
            normalize_openai_compatible_base_url(recorded_endpoint)
            != normalized_endpoint
        ):
            continue
        recorded_alias = str(
            getattr(proc, "_aoi_local_model_alias", "") or ""
        ).strip()
        if recorded_alias == target_alias:
            return proc
    return None


def _wait_for_llama_cpp_startup_listener(
    proc: object,
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    cancel_event=None,
) -> None:
    """Wait only for a newly launched llama-server listener.

    Loading a large GGUF can take substantially longer than the service
    startup critical path.  This gate deliberately checks the launcher and
    TCP listener only; strict ``/v1/models`` alias readiness remains the
    responsibility of request-time recovery (or an explicitly strict launch).
    """

    _check_local_startup_cancelled(cancel_event)
    settings = _llama_cpp_settings(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    base_url = _llama_cpp_base_url(
        config,
        model=model,
        overrides=overrides,
        is_windows=_IS_WINDOWS,
    )
    expected_alias = str(settings.get("model_alias") or "").strip()
    host, port = _base_url_host_port(base_url)
    log_path = Path(__file__).resolve().parents[2] / "logs" / "models" / "llama_cpp.log"

    try:
        if proc.poll() is not None:
            raise RuntimeError(
                "llama-serverがlistener起動前に終了しました。"
                f" endpoint={base_url!r}, expected_alias={expected_alias!r};"
                f" logs/models/llama_cpp.logを確認してください。"
            )
    except AttributeError:
        # Lightweight process doubles used by integrations may not expose
        # ``poll``.  _wait_for_process_port still performs the normal probe.
        pass

    if _wait_for_process_port(
        proc,
        host,
        port,
        timeout_seconds=_LLAMA_CPP_STARTUP_LISTENER_TIMEOUT_SECONDS,
        **({"cancel_event": cancel_event} if cancel_event is not None else {}),
    ):
        _check_local_startup_cancelled(cancel_event)
        return

    try:
        exited = proc.poll() is not None
    except AttributeError:
        exited = False
    if exited:
        raise RuntimeError(
            "llama-serverがlistener起動前に終了しました。"
            f" endpoint={base_url!r}, expected_alias={expected_alias!r};"
            f" logs/models/llama_cpp.logを確認してください。"
        )
    raise RuntimeError(
        "llama-serverのlistener起動がtimeoutしました。"
        f" {_LLAMA_CPP_STARTUP_LISTENER_TIMEOUT_SECONDS:g}秒以内に"
        f"{host}:{port}で待ち受けを確認できませんでした。"
        f" expected_alias={expected_alias!r};"
        f" logs/models/llama_cpp.log tail:\n{_read_log_tail(log_path)}"
    )


def _wait_for_llama_cpp_readiness(
    proc: object,
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    cancel_event=None,
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
        _check_local_startup_cancelled(cancel_event)
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
        _check_local_startup_cancelled(cancel_event)
        if expected_alias and expected_alias in served_ids:
            _set_llama_cpp_startup_state(proc, "ready")
            return
        if served_ids and expected_alias not in served_ids:
            raise RuntimeError(
                "llama-serverは起動しましたが、/v1/modelsのaliasが一致しません。"
                f"期待={expected_alias!r}, 実際={sorted(served_ids)!r}。"
            )
        if cancel_event is not None:
            cancel_event.wait(0.25)
        else:
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
    wait_for_model_ready: bool = True,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
    cancel_event=None,
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
        _check_local_startup_cancelled(cancel_event)
        remaining = max(0.0, deadline - time.monotonic())
        for endpoint in _llama_cpp_endpoints_requiring_lease_drain(
            config,
            model=model,
            overrides=overrides,
            force_restart=force_restart,
        ):
            _wait_for_llama_cpp_generation_lease_idle(
                endpoint, remaining,
                **({"cancel_event": cancel_event} if cancel_event is not None else {}),
            )
            remaining = max(0.0, deadline - time.monotonic())
        with _llama_cpp_startup_lock(cancel_event):
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
                    wait_for_model_ready=wait_for_model_ready or acquire_generation_lease,
                    **({"cancel_event": cancel_event} if cancel_event is not None else {}),
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
    wait_for_model_ready: bool = True,
    cancel_event=None,
) -> bool:
    project_root = Path(__file__).resolve().parents[2]
    proc = None
    started_here = False
    try:
        _check_local_startup_cancelled(cancel_event)
        _validate_llama_cpp_model_alias(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        # Resolve the target runtime once for this lifecycle transition.  In
        # particular, the alias is captured before probing /v1/models so a
        # process that is still loading can be identified from its own launch
        # metadata without consulting mutable persisted settings again.
        runtime_settings = _llama_cpp_settings(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        expected_alias = str(runtime_settings.get("model_alias") or "").strip()
        should_start = True
        _base_url = _llama_cpp_base_url(
            config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
        )
        if force_restart:
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
            # A best-effort startup may have already handed control back while
            # llama-server is loading its model.  Reuse that exact owned
            # process rather than treating its empty /v1/models response as a
            # conflict and launching a second server.  Strict callers wait for
            # the alias; startup callers intentionally do not.
            proc = _owned_llama_cpp_process_at(_base_url, expected_alias)
            if proc is not None:
                if raise_on_launch_error and wait_for_model_ready:
                    _wait_for_llama_cpp_readiness(
                        proc,
                        config,
                        model=model,
                        overrides=overrides,
                        **({"cancel_event": cancel_event} if cancel_event is not None else {}),
                    )
                    _set_llama_cpp_startup_state(proc, "ready")
                    logger.info("llama-serverの起動とalias確認が完了しました")
                else:
                    logger.warning(
                        "llama-serverはlistener起動済みですがmodelをloading中です。"
                        " AoiTalkはdegraded状態で起動を継続します: alias=%r, endpoint=%s",
                        expected_alias,
                        _base_url,
                    )
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
        _check_local_startup_cancelled(cancel_event)
        proc = _start_llama_cpp_server(
            project_root,
            config=config,
            model=model,
            overrides=overrides,
            is_windows=_IS_WINDOWS,
            **({"watch_readiness": True} if not wait_for_model_ready else {}),
        )
        started_here = True
        if raise_on_launch_error and wait_for_model_ready:
            _wait_for_llama_cpp_readiness(
                proc,
                config,
                model=model,
                overrides=overrides,
                **({"cancel_event": cancel_event} if cancel_event is not None else {}),
            )
            _set_llama_cpp_startup_state(proc, "ready")
            logger.info("llama-serverの起動とalias確認が完了しました")
        else:
            _wait_for_llama_cpp_startup_listener(
                proc,
                config,
                model=model,
                overrides=overrides,
                **({"cancel_event": cancel_event} if cancel_event is not None else {}),
            )
            # This is deliberately a single non-blocking observation.  It can
            # improve diagnostics when a very fast load has already exposed
            # the alias, but it must never turn best-effort startup back into
            # the strict readiness loop.
            try:
                observed_ids = _llama_cpp_model_ids_exact(_base_url)
            except Exception:
                observed_ids = set()
            _set_llama_cpp_startup_state(proc, "loading")
            logger.warning(
                "llama-server listener起動済み。modelをloading中のため"
                " AoiTalkはdegraded状態で起動を継続します: alias=%r, endpoint=%s,"
                " observed_ids=%r",
                expected_alias,
                _base_url,
                sorted(observed_ids),
            )
        return True
    except Exception as exc:
        if started_here and proc is not None:
            _terminate_process_tree(proc)
            _remove_tracked_process(proc)
        if raise_on_launch_error:
            raise
        logger.warning("llama-serverの起動を延期します: %s", exc)
        return False


def _owned_local_process_at(
    base_url: str,
    *,
    runtime: str,
) -> bool:
    normalized = normalize_openai_compatible_base_url(base_url)
    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            continue
        if str(getattr(proc, "_aoi_local_runtime", "") or "") != runtime:
            continue
        recorded = str(
            getattr(proc, "_aoi_local_base_url", "") or ""
        ).strip()
        if recorded and normalize_openai_compatible_base_url(recorded) == normalized:
            return True
    return False


def _validate_freetoken_manual_connection(
    config: object | None,
    *,
    model: str,
    base_url: str | None = None,
) -> None:
    resolved_base_url = normalize_openai_compatible_base_url(
        base_url
        or openai_compatible_local_base_url(
            config,
            model=model,
        )
    )
    settings = _freetoken_settings(
        config,
        model=model,
        is_windows=_IS_WINDOWS,
    )
    expected_alias = str(settings.get("model_alias") or model).strip()
    served_ids = _llama_cpp_model_ids_exact(resolved_base_url)
    if expected_alias in served_ids:
        return
    host, port = _base_url_host_port(resolved_base_url)
    if served_ids:
        raise RuntimeError(
            "FreeToken manual接続先は別モデルを提供中です。"
            f"期待={expected_alias!r}, 実際={sorted(served_ids)!r}。"
        )
    if _is_port_open(host, port, timeout_seconds=0.5):
        raise RuntimeError(
            "FreeToken manual接続先は応答しましたが、"
            f"/v1/modelsに期待model {expected_alias!r} がありません。"
        )
    raise RuntimeError(
        "FreeToken manual接続先が起動していません。"
        f" endpoint={resolved_base_url}"
    )


def _owned_loading_local_process_at(
    base_url: str, *, runtime: str, model_alias: str,
) -> object | None:
    """Reuse only a live, exact-target loader started by this process."""
    endpoint = normalize_openai_compatible_base_url(base_url)
    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            continue
        if (
            getattr(proc, "_aoi_local_runtime", "") == runtime
            and getattr(proc, "_aoi_local_model_alias", "") == model_alias
            and getattr(proc, "_aoi_local_readiness_state", "") == "loading"
            and normalize_openai_compatible_base_url(
                str(getattr(proc, "_aoi_local_base_url", "") or "")
            ) == endpoint
        ):
            return proc
    return None


def _observe_local_provider_startup(
    proc: object, *, runtime: str, base_url: str,
) -> None:
    """Bound startup observation; a live pre-listener loader remains owned.

    Strata constructs its engine before binding HTTP. A listener timeout alone
    therefore cannot distinguish a failed launch from ordinary model loading.
    Strict request-time readiness still verifies health and the exact model.
    """
    host, port = _base_url_host_port(base_url)
    listener_ready = _wait_for_process_port(
        proc, host, port,
        timeout_seconds=_LOCAL_PROVIDER_STARTUP_LISTENER_TIMEOUT_SECONDS,
    )
    if proc.poll() is not None:
        raise RuntimeError(
            f"{runtime}が起動中に終了しました。"
            f"{host}:{port}; logs/models/{runtime}.logを確認してください。"
        )
    setattr(proc, "_aoi_local_readiness_state", "loading")
    logger.warning(
        "%s modelをloading中のためAoiTalkはdegraded状態で起動を継続します:"
        " endpoint=%s, listener_ready=%s",
        runtime, base_url, listener_ready,
    )


def _wait_for_strata_model_readiness(
    proc: object,
    config: object | None,
    *,
    model: str,
    deadline: float,
    budget,
    require_loaded: bool = False,
    force_restart: bool = False,
) -> None:
    while True:
        readiness = strata_readiness(
            config,
            model=model,
            owned_process=proc,
        )
        strict_loaded = bool(
            require_loaded
            or (force_restart and readiness.get("process_ready") is True)
        )
        if readiness["ready"] or (
            not strict_loaded and readiness.get("accepting_requests") is True
        ) or (
            not strict_loaded and readiness.get("process_ready") is True
        ):
            # Lazy wrappers load weights on the first inference. Accept their
            # verified health/model contract without claiming weights are ready.
            state = "ready" if readiness["ready"] else "loading"
            setattr(proc, "_aoi_local_readiness_state", state)
            return
        if budget is not None:
            budget.checkpoint()
        if proc.poll() is not None:
            raise RuntimeError("Strataが起動中に終了しました。logs/models/strata.logを確認してください")
        if time.monotonic() >= deadline:
            raise RuntimeError("Strataのhealth/model readinessがtimeoutしました")
        time.sleep(0.5)


def _strata_load_boundary(proc: object) -> None:
    path_value = getattr(proc, "_aoi_local_startup_log_path", None)
    try:
        offset = Path(str(path_value)).stat().st_size if path_value else 0
    except (OSError, TypeError, ValueError):
        offset = 0
    proc._aoi_local_startup_log_offset = offset
    proc._aoi_local_startup_started_at = time.monotonic()
    proc._aoi_local_readiness_state = "loading"


def _strata_post_load(
    config: object | None,
    endpoint: str,
    *,
    deadline: float,
) -> None:
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    key = strata_api_key(config)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(
        f"{base}/load",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise TimeoutError("Strata model load deadline expired before POST /load")
    with urlopen(request, timeout=timeout) as response:
        response.read(64 * 1024)


def _strata_start_or_join_loader(
    proc: object,
    config: object | None,
    endpoint: str,
    model: str,
    *,
    deadline: float,
) -> tuple[threading.Event, dict[str, BaseException]]:
    with _STRATA_LOAD_LOCK:
        event = getattr(proc, "_aoi_strata_load_event", None)
        result = getattr(proc, "_aoi_strata_load_result", None)
        if isinstance(event, threading.Event) and isinstance(result, dict):
            return event, result
        _strata_load_boundary(proc)
        event = threading.Event()
        result = {}
        proc._aoi_strata_load_event = event
        proc._aoi_strata_load_result = result

        try:
            load_ticket = _acquire_llama_cpp_generation_lease(
                endpoint,
                model,
                holder=proc,
            )
        except BaseException as exc:
            # Keep the failed loader event attached to this process. A later
            # generation joins the same failure until an explicit restart or
            # reload boundary clears it; it must not create a second POST.
            result["error"] = exc
            event.set()
            return event, result
        proc._aoi_strata_load_lease_ticket = load_ticket

        def run() -> None:
            try:
                _strata_post_load(config, endpoint, deadline=deadline)
            except BaseException as exc:  # preserve loader failure for caller
                result["error"] = exc
            finally:
                load_ticket.release()
                proc._aoi_strata_load_lease_ticket = None
                event.set()

        thread = threading.Thread(
            target=run,
            daemon=True,
            name="aoi-strata-load",
        )
        proc._aoi_strata_load_thread = thread
        try:
            thread.start()
        except BaseException as exc:
            result["error"] = exc
            load_ticket.release()
            proc._aoi_strata_load_lease_ticket = None
            proc._aoi_strata_load_thread = None
            event.set()
        return event, result


def _wait_for_strata_loader(
    event: threading.Event,
    result: dict[str, BaseException],
    *,
    deadline: float,
    budget,
) -> None:
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Strata model load timed out")
        event.wait(min(0.5, remaining))
        if event.is_set():
            break
        if budget is not None:
            budget.checkpoint()
    error = result.get("error")
    if error is not None:
        raise error


def _ensure_strata_model_ready_for_generation(
    config: object | None,
    *,
    model: str,
    deadline: float,
    budget,
) -> None:
    settings = strata_settings(config, model=model)
    endpoint = strata_base_url(config, model=model)
    readiness = strata_readiness(config, model=model)
    if readiness.get("ready") is True:
        return
    proc = _owned_strata_process_at(endpoint)
    if proc is None:
        raise RuntimeError("Strataのowned loading processが見つかりません")
    prepared_config: dict[str, object] = {}
    try:
        prepared_value = json.loads(
            Path(str(settings["config_path"])).read_text(encoding="utf-8-sig")
        )
        if isinstance(prepared_value, dict):
            prepared_config = prepared_value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if (
        not strata_vision_enabled(settings, prepared_config)
        and getattr(proc, "_aoi_local_startup_log_path", None)
    ):
        if getattr(proc, "_aoi_local_readiness_state", "") == "ready":
            with _STRATA_LOAD_LOCK:
                proc._aoi_strata_load_event = None
                proc._aoi_strata_load_result = None
        event, result = _strata_start_or_join_loader(
            proc,
            config,
            endpoint,
            model,
            deadline=deadline,
        )
        _wait_for_strata_loader(event, result, deadline=deadline, budget=budget)
    _wait_for_strata_model_readiness(
        proc,
        config,
        model=model,
        deadline=deadline,
        budget=budget,
        require_loaded=True,
    )
    proc._aoi_local_readiness_state = "ready"
    proc._aoi_strata_load_completed = True


def _ensure_freetoken_server(
    config: object | None,
    *,
    raise_on_launch_error: bool = False,
    force_restart: bool = False,
    model: str | None = None,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
) -> bool:
    selected_model = str(
        model or _openai_compatible_local_model_id(config) or ""
    ).strip()
    timeout_seconds = _llama_cpp_generation_lease_timeout(
        config,
        model=selected_model,
    )
    deadline = time.monotonic() + timeout_seconds
    identity = _llama_cpp_generation_lease_identity(
        config,
        model=selected_model,
    )

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        for endpoint in _llama_cpp_endpoints_requiring_lease_drain(
            config,
            model=selected_model,
            force_restart=force_restart,
        ):
            _wait_for_llama_cpp_generation_lease_idle(endpoint, remaining)
            remaining = max(0.0, deadline - time.monotonic())

        with _LLAMA_CPP_ENSURE_LOCK:
            drain_endpoints = _llama_cpp_endpoints_requiring_lease_drain(
                config,
                model=selected_model,
                force_restart=force_restart,
            )
            if drain_endpoints:
                if time.monotonic() >= deadline:
                    _wait_for_llama_cpp_generation_lease_idle(
                        drain_endpoints[0],
                        0.0,
                    )
                continue

            proc = None
            try:
                settings = _freetoken_settings(
                    config,
                    model=selected_model,
                    is_windows=_IS_WINDOWS,
                )
                if not bool(settings.get("auto_start")):
                    _validate_freetoken_manual_connection(
                        config,
                        model=selected_model,
                    )
                    return False

                if not _should_start_freetoken(
                    config,
                    model=selected_model,
                    is_windows=_IS_WINDOWS,
                ):
                    error = freetoken_managed_launch_configuration_error(
                        config,
                        model=selected_model,
                        is_windows=_IS_WINDOWS,
                    )
                    if error:
                        raise RuntimeError(error)
                    return False

                base_url = _freetoken_base_url(
                    config,
                    model=selected_model,
                    is_windows=_IS_WINDOWS,
                )
                alias = str(settings.get("model_alias") or "").strip()
                wait_for_model = (
                    raise_on_launch_error or force_restart or acquire_generation_lease
                )
                loading_proc = None if force_restart else _owned_loading_local_process_at(
                    base_url, runtime="freetoken", model_alias=alias,
                )

                if not force_restart and _freetoken_readiness_ready(
                    base_url,
                    alias,
                ):
                    if loading_proc is not None:
                        setattr(loading_proc, "_aoi_local_readiness_state", "ready")
                    started = False
                elif loading_proc is not None:
                    if wait_for_model:
                        _wait_for_freetoken_readiness(
                            loading_proc, config, model=selected_model,
                            is_windows=_IS_WINDOWS,
                        )
                        setattr(loading_proc, "_aoi_local_readiness_state", "ready")
                    # A second caller must not terminate a healthy owned loader
                    # just because its model is not ready yet. Failure of this
                    # caller's strict wait likewise leaves that loader owned.
                    started = False
                else:
                    host, port = _base_url_host_port(base_url)
                    port_open = _is_port_open(host, port, timeout_seconds=0.5)
                    owned_target = _owned_local_process_at(
                        base_url,
                        runtime="freetoken",
                    )

                    if force_restart and _openai_compatible_local_processes:
                        # Restart only the owned FreeToken target.  A global
                        # stop here could kill another local runtime (or a
                        # session/agent consumer) on a different endpoint.
                        stop_owned_openai_compatible_local_servers_respecting_generation_leases(
                            runtime="freetoken",
                            model=alias,
                            endpoint=base_url,
                            timeout_seconds=_llama_cpp_generation_lease_timeout(
                                config,
                                model=selected_model,
                            ),
                        )
                    elif port_open and not owned_target:
                        raise RuntimeError(
                            "FreeTokenの対象portは外部プロセスが使用中です。"
                            f"{host}:{port} は停止しません。"
                        )
                    elif not port_open and any(
                        getattr(candidate, "_aoi_local_readiness_state", "") == "loading"
                        for candidate in _owned_processes_for_scope(
                            endpoint=base_url,
                        )
                    ):
                        # A different alias can be loading before it binds.
                        # Do not spawn a second server onto its future port or
                        # stop that owned loader without an explicit restart.
                        raise RuntimeError(
                            "FreeTokenの対象endpointでは別モデルが起動中です。"
                            "起動完了を待つか、明示的に再起動してください。"
                        )
                    elif port_open and owned_target:
                        _stop_owned_openai_compatible_local_servers_if_unleased(
                            base_url
                        )

                    project_root = Path(__file__).resolve().parents[2]
                    proc = _start_freetoken_server(
                        project_root,
                        config=config,
                        model=selected_model,
                        is_windows=_IS_WINDOWS,
                    )
                    if wait_for_model:
                        _wait_for_freetoken_readiness(
                            proc,
                            config,
                            model=selected_model,
                            is_windows=_IS_WINDOWS,
                        )
                        setattr(proc, "_aoi_local_readiness_state", "ready")
                        logger.info("FreeTokenの起動とhealth/model確認が完了しました")
                    else:
                        _observe_local_provider_startup(
                            proc, runtime="freetoken", base_url=base_url,
                        )
                    started = True

                if acquire_generation_lease and identity is not None:
                    endpoint, alias = identity
                    if started or _freetoken_readiness_ready(endpoint, alias):
                        ticket = _acquire_llama_cpp_generation_lease(
                            endpoint,
                            alias,
                            holder=lease_holder,
                        )
                        if lease_tickets is not None:
                            lease_tickets.append(ticket)
                return started
            except Exception as exc:
                if proc is not None:
                    _terminate_process_tree(proc)
                    _remove_tracked_process(proc)
                if raise_on_launch_error:
                    raise
                logger.warning("FreeTokenの起動を延期します: %s", exc)
                return False


def _owned_strata_process_at(base_url: str):
    """Return the live tracked Strata process, not the legacy boolean probe."""
    normalized = normalize_openai_compatible_base_url(base_url)
    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            continue
        if getattr(proc, "_aoi_local_runtime", "") != "strata":
            continue
        recorded = str(getattr(proc, "_aoi_local_base_url", "") or "")
        if recorded and normalize_openai_compatible_base_url(recorded) == normalized:
            return proc
    return None


def _owned_strata_processes_for_model(model: str) -> list:
    """Return live AoiTalk-owned Strata processes for one model.

    A model may briefly have an old port and a replacement port during a
    configuration refresh.  Filter by the runtime marker and served model so
    another managed runtime on a different endpoint is never stopped.
    """

    selected = str(model or "").strip()
    processes = []
    for proc in list(_openai_compatible_local_processes):
        try:
            if proc.poll() is not None:
                continue
        except Exception:
            continue
        if getattr(proc, "_aoi_local_runtime", "") != "strata":
            continue
        if str(getattr(proc, "_aoi_local_model_alias", "") or "") != selected:
            continue
        processes.append(proc)
    return processes


def stop_strata_server(config: object | None, *, model: str) -> bool:
    settings = strata_settings(config, model=model)
    if not settings["auto_start"]:
        return False
    endpoint = strata_base_url(config, model=model)
    with _LLAMA_CPP_ENSURE_LOCK:
        if _peek_llama_cpp_lifecycle_lease(endpoint)[1]:
            raise RuntimeError("Strataが応答生成中です。生成終了後に停止してください")
        proc = _owned_strata_process_at(endpoint)
        if proc is None or getattr(proc, "_aoi_local_model_alias", "") != model:
            return False
        _terminate_process_tree(proc)
        _remove_tracked_process(proc)
        return True


def validate_strata_setup_safety(config: object | None, *, model: str) -> None:
    """Reject setup while the same-model Strata runtime is serving or leased."""

    endpoints = {strata_base_url(config, model=model)}
    with _LLAMA_CPP_ENSURE_LOCK:
        for proc in _owned_strata_processes_for_model(model):
            endpoint = str(getattr(proc, "_aoi_local_base_url", "") or "")
            if endpoint:
                endpoints.add(endpoint)
        for endpoint in endpoints:
            alias, count = _peek_llama_cpp_lifecycle_lease(endpoint)
            if count > 0:
                raise RuntimeError(
                    "Strata setupは応答生成中には実行できません。"
                    f"使用中alias={alias!r}, count={count}"
                )
        owned = _owned_strata_processes_for_model(model)
        if owned:
            raise RuntimeError(
                "Strata runtimeが稼働中です。停止してからsetupを再実行してください"
            )
        host, port = _base_url_host_port(strata_base_url(config, model=model))
        if _is_port_open(host, port, timeout_seconds=0.2):
            raise RuntimeError(
                "Strataの対象portを外部プロセスが使用中です。"
                "稼働中のendpointを停止せずsetupを中止しました"
            )


def _strata_endpoints_requiring_lease_drain(
    config: object | None,
    *,
    model: str,
    force_restart: bool,
) -> list[str]:
    """Return only same-model Strata endpoints whose leases must drain."""

    candidates = {strata_base_url(config, model=model)}
    if force_restart:
        candidates.update(
            str(getattr(proc, "_aoi_local_base_url", "") or "")
            for proc in _owned_strata_processes_for_model(model)
        )
    return [
        endpoint
        for endpoint in candidates
        if endpoint and _peek_llama_cpp_lifecycle_lease(endpoint)[1] > 0
    ]


def _ensure_strata_server(
    config: object | None,
    *,
    model: str,
    raise_on_launch_error: bool = False,
    force_restart: bool = False,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
    wait_for_model_ready: bool = True,
    deadline: float | None = None,
) -> bool:
    from src.llm.generation_budget import current_generation_budget

    budget = current_generation_budget.get()
    settings = strata_settings(config, model=model)
    if not settings["auto_start"]:
        return False
    prepared_config: dict[str, object] = {}
    try:
        prepared_value = json.loads(
            Path(str(settings["config_path"])).read_text(encoding="utf-8-sig")
        )
        if isinstance(prepared_value, dict):
            prepared_config = prepared_value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    vision_enabled = strata_vision_enabled(settings, prepared_config)
    endpoint = strata_base_url(config, model=model)
    if deadline is None:
        timeout = float(settings["readiness_timeout"])
        if budget is not None:
            remaining = budget.checkpoint()
            if remaining is not None:
                timeout = min(timeout, remaining)
        deadline = time.monotonic() + timeout
    while True:
        if budget is not None:
            budget.checkpoint()
        # A generation lease protects a process from replacement; it is not
        # an exclusive inference lock. Reusing the same healthy wrapper must
        # not wait for an earlier request (including lazy first inference).
        # Keep probe + ticket acquisition under the lifecycle lock, so a
        # concurrent stop/restart cannot slip between these operations.
        if not force_restart:
            with _LLAMA_CPP_ENSURE_LOCK:
                active_alias, active_count = _peek_llama_cpp_lifecycle_lease(endpoint)
                if active_count > 0 and active_alias == model:
                    loading = _owned_loading_local_process_at(
                        endpoint, runtime="strata", model_alias=model,
                    )
                    readiness = strata_readiness(
                        config,
                        model=model,
                        owned_process=loading,
                    )
                    if (
                        readiness["ready"]
                        or readiness.get("accepting_requests") is True
                        or (
                            not wait_for_model_ready
                            and readiness.get("process_ready") is True
                        )
                    ):
                        if readiness["ready"] and loading is not None:
                            setattr(loading, "_aoi_local_readiness_state", "ready")
                        if budget is not None:
                            budget.checkpoint()
                        if acquire_generation_lease:
                            ticket = _acquire_llama_cpp_generation_lease(
                                endpoint, model, holder=lease_holder,
                            )
                            if lease_tickets is not None:
                                lease_tickets.append(ticket)
                        return False
        remaining = max(0.0, deadline - time.monotonic())
        for busy in _strata_endpoints_requiring_lease_drain(config, model=model, force_restart=force_restart):
            _wait_for_llama_cpp_generation_lease_idle(busy, remaining)
        with _LLAMA_CPP_ENSURE_LOCK:
            if _strata_endpoints_requiring_lease_drain(config, model=model, force_restart=force_restart):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Strataの起動待ち中にgeneration leaseが解放されませんでした")
                continue
            proc = None
            try:
                loading_proc = (
                    _owned_strata_process_at(endpoint)
                    if force_restart
                    else _owned_loading_local_process_at(
                        endpoint, runtime="strata", model_alias=model,
                    )
                )
                readiness = strata_readiness(
                    config,
                    model=model,
                    owned_process=loading_proc,
                )
                available = (
                    readiness["ready"]
                    or readiness.get("accepting_requests") is True
                    or (
                        not wait_for_model_ready
                        and readiness.get("process_ready") is True
                    )
                )
                started = False
                wait_for_model = (
                    wait_for_model_ready
                    and (raise_on_launch_error or force_restart or acquire_generation_lease)
                )
                if (
                    force_restart
                    and loading_proc is not None
                    and readiness.get("process_ready") is True
                    and readiness["ready"] is False
                ):
                    # A fresh lazy process is only process-ready: do not stop
                    # it for a force restart until its strict loaded-model
                    # readiness is proven. A timeout leaves this old owner
                    # untouched and rolls back only a process started here.
                    _wait_for_strata_model_readiness(
                        loading_proc,
                        config,
                        model=model,
                        deadline=deadline,
                        budget=budget,
                        require_loaded=True,
                    )
                    readiness = strata_readiness(
                        config,
                        model=model,
                        owned_process=loading_proc,
                    )
                    available = readiness["ready"] or readiness.get(
                        "accepting_requests"
                    ) is True
                if readiness["ready"] and loading_proc is not None:
                    setattr(loading_proc, "_aoi_local_readiness_state", "ready")
                if not available and loading_proc is not None:
                    _wait_for_strata_model_readiness(
                        loading_proc,
                        config,
                        model=model,
                        deadline=deadline,
                        budget=budget,
                        require_loaded=vision_enabled and wait_for_model,
                        force_restart=force_restart,
                    )
                    # Reuse the exact process while weights load. Do not assign
                    # it to proc: rollback belongs only to this call's launch.
                    available = True
                if force_restart or not available:
                    host, port = _base_url_host_port(endpoint)
                    owned = _owned_strata_process_at(endpoint)
                    stale_owned = (
                        [proc for proc in _owned_strata_processes_for_model(model) if proc is not owned]
                        if force_restart
                        else []
                    )
                    if _is_port_open(host, port, timeout_seconds=0.5) and not owned:
                        raise RuntimeError("Strataのportを外部プロセスが使用中です。停止しません")
                    if owned is None and any(
                        getattr(candidate, "_aoi_local_readiness_state", "") == "loading"
                        for candidate in _owned_processes_for_scope(endpoint=endpoint)
                    ):
                        raise RuntimeError(
                            "Strataの対象endpointでは別runtimeが起動中です。停止しません"
                        )
                    if budget is not None:
                        budget.checkpoint()
                    args, cwd = strata_launch_plan(config, model=model)
                    if owned is not None:
                        # Only this endpoint was drained. Never stop an owned
                        # llama.cpp/FreeToken process on a different port.
                        if _peek_llama_cpp_lifecycle_lease(endpoint)[1]:
                            raise RuntimeError("Strataの対象endpointは応答生成中です")
                        _terminate_process_tree(owned)
                        _remove_tracked_process(owned)
                    startup_log_path = Path(__file__).resolve().parents[2] / "logs/models/strata.log"
                    try:
                        startup_log_offset = startup_log_path.stat().st_size
                    except OSError:
                        startup_log_offset = 0
                    from src.utils.process_identity import strata_title_template

                    proc = _start_logged_openai_compatible_process(
                        args, cwd=cwd,
                        log_path=startup_log_path,
                        # Strata is a long-lived lazy runtime: it can outlive
                        # this backend across a restart.  A parent-owned
                        # stdout PIPE would then close under the wrapper while
                        # its first lazy-load progress line is printed, which
                        # disconnects the generation request before native
                        # startup.  Keep its output on the stable log file;
                        # other runtimes retain their existing console policy.
                        mirror_to_parent_console=False,
                        create_new_console_on_windows=False,
                        console_title=strata_title_template(model),
                    )
                    try:
                        startup_log_offset = min(
                            startup_log_offset,
                            startup_log_path.stat().st_size,
                        )
                    except OSError:
                        pass
                    proc._aoi_local_runtime = "strata"
                    proc._aoi_local_base_url = endpoint
                    proc._aoi_local_model_alias = model
                    proc._aoi_local_startup_log_path = str(startup_log_path)
                    proc._aoi_local_startup_log_offset = startup_log_offset
                    proc._aoi_local_startup_started_at = time.monotonic()
                    proc._aoi_local_readiness_state = "loading"
                    if wait_for_model or raise_on_launch_error or force_restart or acquire_generation_lease:
                        _wait_for_strata_model_readiness(
                            proc,
                            config,
                            model=model,
                            deadline=deadline,
                            budget=budget,
                            require_loaded=vision_enabled and wait_for_model,
                            force_restart=force_restart,
                        )
                    else:
                        _observe_local_provider_startup(
                            proc, runtime="strata", base_url=endpoint,
                        )
                    # A port change can leave the previous AoiTalk-owned Strata
                    # listener alive.  Stop only same-model Strata processes,
                    # after the replacement is ready; unrelated llama.cpp or
                    # FreeToken processes on other ports remain untouched.
                    for stale in stale_owned:
                        if _peek_llama_cpp_lifecycle_lease(
                            str(getattr(stale, "_aoi_local_base_url", ""))
                        )[1]:
                            raise RuntimeError("旧Strata endpointは応答生成中です")
                        _terminate_process_tree(stale)
                        _remove_tracked_process(stale)
                    started = True
                if budget is not None:
                    budget.checkpoint()
                if acquire_generation_lease:
                    ticket = _acquire_llama_cpp_generation_lease(endpoint, model, holder=lease_holder)
                    if lease_tickets is not None:
                        lease_tickets.append(ticket)
                return started
            except Exception as exc:
                if proc is not None:
                    _terminate_process_tree(proc)
                    _remove_tracked_process(proc)
                if raise_on_launch_error:
                    raise
                logger.warning("Strataの起動を延期します: %s", exc)
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
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model_id,
    )

    if managed_runtime == "strata":
        settings = strata_settings(config, model=selected_model_id)
        if settings["auto_start"]:
            strata_launch_plan(config, model=selected_model_id)
        return

    if managed_runtime == "freetoken":
        resolved = resolve_freetoken_runtime(
            config,
            model=selected_model_id,
            is_windows=_IS_WINDOWS,
        )
        settings = resolved.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        if not bool(settings.get("auto_start", resolved.get("auto_start"))):
            _validate_freetoken_manual_connection(
                config,
                model=selected_model_id,
                base_url=base_url,
            )
            return
        if resolved.get("state") != "ready":
            error = freetoken_managed_launch_configuration_error(
                config,
                model=selected_model_id,
                is_windows=_IS_WINDOWS,
            )
            raise RuntimeError(
                error
                or "選択したFreeToken managed runtimeが未構成です。"
            )
        _freetoken_launch_plan(
            config,
            model=selected_model_id,
            is_windows=_IS_WINDOWS,
        )
        return

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
    wait_for_model_ready: bool = True,
    model: str | None = None,
    acquire_generation_lease: bool = False,
    lease_holder: object | None = None,
    lease_tickets: list | None = None,
    cancel_event=None,
) -> bool:
    """Start the selected bundled local OpenAI-compatible server if needed.

    Local LLM startup is best-effort. AoiTalk itself should keep starting even
    when the selected model is still loading or the helper process fails to
    launch; generation-time health handling reports that state to the user.
    """
    # Enterprise external deployments are owned by the operator.  This guard
    # is intentionally before runtime/profile resolution so stale managed
    # settings cannot trigger a local llama.cpp/FreeToken launch.
    try:
        from src.llm.deployment_resolver import resolve_llm_deployment

        deployment = resolve_llm_deployment(config)
        if (
            deployment is not None
            and deployment.backend == "external"
            and deployment.effective_provider == "openai_compatible_local"
        ):
            return False
    except Exception:
        # Lightweight personal/test configs may not define a deployment; keep
        # the existing local-runtime path in that case.
        pass

    project_root = Path(__file__).resolve().parents[2]
    base_url = _openai_compatible_local_base_url(config)
    selected_model = str(
        model or _openai_compatible_local_model_id(config) or ""
    ).strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model,
    )
    if managed_runtime == "strata":
        from src.llm.generation_budget import current_generation_budget

        settings = strata_settings(config, model=selected_model)
        budget = current_generation_budget.get()
        timeout = float(settings["readiness_timeout"])
        if budget is not None:
            remaining = budget.checkpoint()
            if remaining is not None:
                timeout = min(timeout, remaining)
        deadline = time.monotonic() + timeout
        tracked_tickets = lease_tickets if lease_tickets is not None else []
        ticket_start = len(tracked_tickets)
        try:
            started = _ensure_strata_server(
                config,
                model=selected_model,
                raise_on_launch_error=raise_on_launch_error,
                force_restart=force_restart,
                acquire_generation_lease=acquire_generation_lease,
                lease_holder=lease_holder,
                lease_tickets=tracked_tickets,
                wait_for_model_ready=wait_for_model_ready,
                deadline=deadline,
            )
            if acquire_generation_lease:
                _ensure_strata_model_ready_for_generation(
                    config,
                    model=selected_model,
                    deadline=deadline,
                    budget=budget,
                )
        except Exception:
            while len(tracked_tickets) > ticket_start:
                tracked_tickets.pop().release()
            if raise_on_launch_error or force_restart:
                raise
            if acquire_generation_lease:
                logger.warning("Strata model load did not reach readiness", exc_info=True)
                return False
            raise
        if lease_tickets is None:
            while len(tracked_tickets) > ticket_start:
                tracked_tickets.pop().release()
        return started

    if managed_runtime == "freetoken":
        return _ensure_freetoken_server(
            config,
            model=selected_model,
            raise_on_launch_error=raise_on_launch_error,
            force_restart=force_restart,
            acquire_generation_lease=acquire_generation_lease,
            lease_holder=lease_holder,
            lease_tickets=lease_tickets,
        )

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

    if (
        managed_runtime == "llama_cpp"
        or _should_start_llama_cpp(config, model=model, is_windows=_IS_WINDOWS)
    ):
        return _ensure_llama_cpp_server(
            config,
            model=model,
            raise_on_launch_error=raise_on_launch_error,
            force_restart=force_restart,
            wait_for_model_ready=wait_for_model_ready,
            acquire_generation_lease=acquire_generation_lease,
            lease_holder=lease_holder,
            lease_tickets=lease_tickets,
            **({"cancel_event": cancel_event} if cancel_event is not None else {}),
        )

    return False


def _should_skip_caddy() -> bool:
    return _env_bool("AOITALK_SKIP_CADDY")


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


def _listening_tcp_ports_by_pid() -> dict[int, set[int]]:
    """Return a best-effort snapshot of TCP listeners grouped by process ID."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return {}

    listeners: dict[int, set[int]] = {}
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or connection.pid is None:
            continue
        local_address = connection.laddr
        port = getattr(local_address, "port", None)
        if port is None:
            try:
                port = local_address[1]
            except (IndexError, TypeError):
                continue
        try:
            normalized_pid = int(connection.pid)
            normalized_port = int(port)
        except (TypeError, ValueError):
            continue
        if normalized_pid <= 0 or not 1 <= normalized_port <= 65535:
            continue
        listeners.setdefault(normalized_pid, set()).add(normalized_port)
    return listeners


def _looks_like_aoitalk_main_process(process: psutil.Process) -> bool:
    """Return whether a process is an AoiTalk ``main.py`` runtime owner."""
    try:
        command_line = process.cmdline()
        working_directory = Path(process.cwd())
    except (psutil.Error, OSError):
        return False

    has_main_entrypoint = any(
        Path(str(argument).strip('"')).name.casefold() == "main.py"
        for argument in command_line
    )
    if not has_main_entrypoint:
        return False
    return (
        working_directory / "src" / "service_manager" / "__init__.py"
    ).is_file()


def _aoitalk_main_owner_pid(listener_pid: int) -> int | None:
    """Find the nearest AoiTalk runtime ancestor that owns a listener."""
    try:
        listener = psutil.Process(listener_pid)
        candidates = [listener, *listener.parents()]
    except psutil.Error:
        return None

    for candidate in candidates:
        if _looks_like_aoitalk_main_process(candidate):
            return candidate.pid
    return None


class AoiTalkAlreadyRunning(RuntimeError):
    """Signal that a healthy same-repository AoiTalk instance already owns startup."""

    def __init__(self, operator_url: str) -> None:
        self.operator_url = str(operator_url)
        super().__init__(f"AoiTalk はすでに起動しています: {self.operator_url}")


def _registered_aoi_talk_deployment_root(project_root: Path) -> Path | None:
    """Read only the two public path bindings for this repo's Portal release."""

    root = project_root.resolve()
    registration = root.parent / "85_CaddyGateway" / "config" / "private" / "runtime.env"
    allowed_keys = {"GATEWAY_AOITALK_ROOT", "GATEWAY_AOITALK_WORKING_DIRECTORY"}
    bindings: dict[str, str] = {}
    try:
        with registration.open(encoding="utf-8-sig") as stream:
            for line in stream:
                key, separator, value = line.strip().partition("=")
                key = key.strip().upper()
                if not separator or key not in allowed_keys:
                    continue
                if key in bindings:
                    return None
                value = value.strip()
                if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                    value = value[1:-1]
                bindings[key] = value
        if set(bindings) != allowed_keys:
            return None
        configured_root = Path(bindings["GATEWAY_AOITALK_ROOT"])
        configured_cwd = Path(bindings["GATEWAY_AOITALK_WORKING_DIRECTORY"])
        if not configured_root.is_absolute() or not configured_cwd.is_absolute():
            return None
        if Path(os.path.abspath(configured_cwd)) != root:
            return None
        deployment = Path(os.path.abspath(configured_root))
        release_base = root / "artifacts" / "releases"
        relative = deployment.relative_to(release_base)
        if not relative.parts:
            return None
        # Never turn an arbitrary checkout, symlink, or junction into this
        # repository merely because it exposes a health endpoint.
        current = root
        for component in (deployment / "main.py").relative_to(root).parts:
            current /= component
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None
            if current != deployment / "main.py" and not stat.S_ISDIR(info.st_mode):
                return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return deployment
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None


def _strict_aoitalk_main_identity(
    listener_pid: int,
    project_root: Path,
) -> tuple[int, float] | None:
    """Return a stable identity for this repo or its registered Portal release.

    Port ownership is not enough to adopt a process: another checkout, an
    unrelated service, or a recycled PID must remain protected. Require the
    listener's AoiTalk ancestor to run this checkout's main script, or the
    exact registered Portal deployment script from its registered root.
    """

    owner_pid = _aoitalk_main_owner_pid(listener_pid)
    if owner_pid is None:
        return None
    root = project_root.resolve()
    expected_script_working_directories = {
        (root / "main.py").resolve(): {root},
    }
    deployment_root = _registered_aoi_talk_deployment_root(root)
    if deployment_root is not None:
        deployment_root = deployment_root.resolve()
        expected_script_working_directories[deployment_root / "main.py"] = {
            root,
            deployment_root,
        }
    try:
        process = psutil.Process(owner_pid)
        if int(process.pid) != int(owner_pid) or not process.is_running():
            return None
        if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return None
        created_at = float(process.create_time())
        if not math.isfinite(created_at) or created_at <= 0:
            return None
        working_directory = Path(process.cwd()).resolve()
        if not any(
            working_directory in allowed_directories
            for allowed_directories in expected_script_working_directories.values()
        ):
            return None
        command_line = process.cmdline()
    except (psutil.Error, OSError, TypeError, ValueError):
        return None

    for raw_argument in command_line:
        argument = str(raw_argument).strip().strip('"')
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = working_directory / candidate
        try:
            expected_working_directories = expected_script_working_directories.get(
                candidate.resolve(),
            )
            if expected_working_directories and working_directory in expected_working_directories:
                return int(owner_pid), created_at
        except (OSError, RuntimeError):
            continue
    return None


def _probe_loopback_http_status(port: int, path: str) -> bool:
    """Probe one loopback endpoint without reading or logging its response body."""
    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection("127.0.0.1", int(port), timeout=0.75)
        connection.request("GET", path, headers={"Connection": "close"})
        return int(connection.getresponse().status) == 200
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _frontend_readiness_path() -> str:
    """Return the canonical unauthenticated Next route for this base path."""

    configured = os.getenv("AOITALK_PUBLIC_BASE_PATH", "").strip()
    if not configured:
        configured = os.getenv("NEXT_PUBLIC_BASE_PATH", "").strip()
    base_path = normalize_public_base_path(configured)
    return f"{base_path}/login" if base_path else "/login"


def _startup_listener_pids(
    listeners_by_pid: dict[int, set[int]],
    port: int,
) -> list[int]:
    return sorted(pid for pid, ports in listeners_by_pid.items() if port in ports)


def _reject_existing_startup_listener(reason: str) -> None:
    raise RuntimeError(
        "既存のFastAPI/Next.js listenerを安全にAoiTalkとして識別できないため、"
        f"起動を中止しました（{reason}）。既存サービスは停止していません。"
        "既存プロセスを確認するか、別ポートを指定してください。"
    )


def _check_existing_aoi_talk_instance(
    config: object | None,
    *,
    fastapi_port: int,
    next_port: int,
) -> str | None:
    """Guard startup before Qdrant/build/port cleanup touches any service.

    A healthy pair of loopback listeners owned by this exact checkout is an
    idempotent manual launch.  Every partial, unknown, unrelated, or unhealthy
    occupancy fails closed so startup cannot kill a service it did not prove.
    """

    listeners_by_pid = _listening_tcp_ports_by_pid()
    ports = (int(fastapi_port), int(next_port))
    listener_pids = {
        port: _startup_listener_pids(listeners_by_pid, port) for port in ports
    }
    port_open: dict[int, bool] = {}
    for port in ports:
        try:
            port_open[port] = _is_port_open(
                "127.0.0.1", port, timeout_seconds=0.2
            )
        except Exception:
            # A platform probe failure is an unknown occupied state.  Keep
            # startup fail-closed when listener evidence exists, while still
            # allowing the normal free-port path to proceed.
            port_open[port] = False
    occupied = {
        port
        for port in ports
        if listener_pids[port] or port_open[port]
    }
    if not occupied:
        return None
    if occupied != set(ports):
        _reject_existing_startup_listener("部分起動またはlistener競合")
    if any(len(listener_pids[port]) != 1 for port in ports):
        _reject_existing_startup_listener("listener所有者が一意ではありません")

    project_root = Path(__file__).resolve().parents[2]
    identities = {
        port: _strict_aoitalk_main_identity(listener_pids[port][0], project_root)
        for port in ports
    }
    if any(identity is None for identity in identities.values()):
        _reject_existing_startup_listener("所有者がこのrepositoryのmain.pyではありません")
    identity_values = set(identities.values())
    if len(identity_values) != 1:
        _reject_existing_startup_listener("FastAPIとNext.jsのmain.py所有者が一致しません")

    if not _probe_loopback_http_status(fastapi_port, "/health"):
        _reject_existing_startup_listener("FastAPI healthがHTTP 200ではありません")
    if not _probe_loopback_http_status(next_port, _frontend_readiness_path()):
        _reject_existing_startup_listener("Next.jsがHTTP 200ではありません")

    if _supervised_startup_enabled():
        raise RuntimeError(
            "supervised startupでは稼働中のAoiTalkを引き継げません。"
            "既存サービスを停止せず、別ポートまたは既存プロセスの終了後に再試行してください。"
        )
    return get_operator_web_url(config)


def _assert_caddy_takeover_safe(*, caddy_port: int, fastapi_port: int) -> None:
    """Refuse to replace another live AoiTalk instance's public Caddy entry.

    QA/development instances commonly select alternate FastAPI and Next.js
    ports.  Reusing the default Caddy port in that topology previously killed
    the production Caddy process first, then removed the replacement on exit,
    leaving the mobile application with no reachable API endpoint.  A normal
    same-port restart remains allowed because it intentionally replaces the
    existing runtime as one topology.
    """
    listeners_by_pid = _listening_tcp_ports_by_pid()
    caddy_listener_pids = sorted(
        pid for pid, ports in listeners_by_pid.items() if caddy_port in ports
    )
    for listener_pid in caddy_listener_pids:
        owner_pid = _aoitalk_main_owner_pid(listener_pid)
        if owner_pid is None:
            # Preserve the historical cleanup behavior for an unrelated or
            # orphaned listener that cannot be identified as a live AoiTalk.
            continue
        owner_ports = listeners_by_pid.get(owner_pid, set())
        if not owner_ports or fastapi_port in owner_ports:
            # No live backend was found (stale runtime), or this is the normal
            # same-topology restart path.
            continue

        existing_ports = ", ".join(str(port) for port in sorted(owner_ports))
        logger.warning(
            "Refusing Caddy takeover on port %s: AoiTalk owner PID %s listens "
            "on backend port(s) %s, requested backend port is %s",
            caddy_port,
            owner_pid,
            existing_ports,
            fastapi_port,
            extra=FILE_ONLY_LOG_EXTRA,
        )
        raise RuntimeError(
            f"Caddyポート {caddy_port} は別のAoiTalk実行中インスタンスが使用しています"
            f"（既存API: {existing_ports}、今回のAPI: {fastapi_port}）。"
            "稼働中のモバイル接続を停止しないため起動を中止しました。"
            "検証用インスタンスでは AOITALK_SKIP_CADDY=true を指定するか、"
            "AOITALK_CADDY_PORT に別ポートを指定してください。"
        )


def _site_identity(value: object | None) -> str | None:
    """Return a hostname/IP identity from a configured Caddy endpoint."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if host in {"", "0.0.0.0", "::", "*", "localhost"}:
        return None
    return host


def _windows_certificate_identity(config: object | None = None) -> str | None:
    """Resolve the explicitly configured public identity used by setup_https."""
    for value in (
        os.getenv("AOITALK_CADDY_PUBLIC_URL"),
        os.getenv("AOITALK_CADDY_SITE_ADDRESS"),
        os.getenv("AOITALK_CADDY_SITE_LABEL"),
        _config_get(config, "caddy.public_url", None),
        _config_get(config, "caddy.site_address", None),
        _config_get(config, "web_interface.public_host", None),
    ):
        identity = _site_identity(value)
        if identity:
            return identity
    domain = os.getenv("DDNS_NOW_DOMAIN", "").strip().lower().rstrip(".")
    if domain:
        return domain if domain.endswith(".f5.si") else f"{domain}.f5.si"
    return None


def _discover_windows_certificate_pair(
    config: object | None = None,
    *,
    project_root: Path | None = None,
) -> tuple[str, str] | None:
    """Find the valid canonical lego pair for the configured site identity.

    Only the runtime-owned canonical ``certs/certificates`` directory is
    inspected.  Certificate metadata is public; private-key bytes are never
    read.  An unconfigured identity or missing pair leaves ownership with
    Caddy-native HTTPS, while equally ranked matches fail closed.
    """
    if not _IS_WINDOWS:
        return None
    identity = _windows_certificate_identity(config)
    root = project_root or Path(__file__).resolve().parents[2]
    certificate_dir = root / "certs" / "certificates"
    now = datetime.now(timezone.utc)
    matches: list[tuple[Path, Path]] = []
    try:
        from cryptography import x509
    except Exception:
        return None
    for certificate in sorted(certificate_dir.glob("*.crt")):
        key = certificate.with_suffix(".key")
        if not key.is_file():
            continue
        try:
            cert = x509.load_pem_x509_certificate(certificate.read_bytes())
            not_before = cert.not_valid_before_utc
            not_after = cert.not_valid_after_utc
            names = set()
            try:
                san = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
                names.update(name.lower().rstrip(".") for name in san.get_values_for_type(x509.DNSName))
                names.update(str(name).lower() for name in san.get_values_for_type(x509.IPAddress))
            except x509.ExtensionNotFound:
                pass
            cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            names.update(attr.value.lower().rstrip(".") for attr in cn)
            if not_before <= now <= not_after and (not identity or identity in names):
                matches.append((certificate, key))
        except Exception:
            continue
    if len(matches) == 1:
        return tuple(map(str, matches[0]))
    if len(matches) > 1:
        labels = ", ".join(path.name for path, _ in matches)
        raise RuntimeError(
            "Windows HTTPS証明書候補が同じsite identityに複数あります: "
            f"{labels}。AOITALK_CERT_CRTとAOITALK_CERT_KEYを指定してください。"
        )
    return None


def get_caddy_public_url(config: object | None = None) -> str | None:
    """Return the operator-facing URL exposed by the managed Caddy process.

    FastAPI reports its loopback listener URL, but native startup normally
    publishes the Web UI through Caddy on a different port/hostname.  Keep
    URL resolution next to the Caddy topology so the readiness callback can
    return the same effective endpoint that was configured for the process.
    Explicit public URL settings take precedence; otherwise derive the URL
    from Caddy's site address and port without exposing credentials or paths.
    """

    if _should_skip_caddy():
        return None

    def _configured(*env_names: str, config_keys: tuple[str, ...] = ()) -> str | None:
        for name in env_names:
            value = os.getenv(name)
            if value and str(value).strip():
                return str(value).strip()
        for key in config_keys:
            value = _config_get(config, key, None)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    explicit = _configured(
        "AOITALK_CADDY_PUBLIC_URL",
        "AOITALK_PUBLIC_URL",
        config_keys=("caddy.public_url", "web_interface.public_url"),
    )
    site_label = _configured("AOITALK_CADDY_SITE_LABEL")
    address = _configured(
        "AOITALK_CADDY_SITE_ADDRESS",
        config_keys=("caddy.site_address", "web_interface.public_host"),
    )
    port = _service_port(
        config,
        env_names=("AOITALK_CADDY_PORT",),
        config_key="caddy.port",
        default=6002,
    )

    # Explicit certificate paths remain a supported way to name the endpoint.
    if not address and _IS_WINDOWS:
        configured_certificate = os.getenv("AOITALK_CERT_CRT")
        if configured_certificate and str(configured_certificate).strip():
            address = Path(configured_certificate).stem
        else:
            address = _windows_certificate_identity(config)
            if not address:
                discovered = _discover_windows_certificate_pair(config)
                if discovered:
                    address = Path(discovered[0]).stem

    # Explicit public URLs are trusted configuration, but strip trailing
    # slashes so browser/open-url and API consumers agree on one identity.
    candidate = explicit or site_label or address or "localhost"
    candidate = str(candidate).strip().rstrip("/")
    if not candidate:
        candidate = "localhost"

    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    scheme = (parsed.scheme or "https").lower()
    host = parsed.hostname or "localhost"
    if parsed.username or parsed.password:
        # Never propagate credentials into an operator-facing URL.
        host = "localhost"
    host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host

    # A site label such as ``https://:6002`` deliberately binds all hosts and
    # is not a usable browser destination.  Wildcard bind addresses are also
    # not valid destinations for a browser, so fall back to the configured
    # site address/certificate name (or loopback) in those cases.
    wildcard_hosts = {"", "0.0.0.0", "::", "[::]", "*"}
    if (not parsed.hostname or not host_text.strip("[]")) or host.lower() in wildcard_hosts:
        fallback = address if address and address.strip().lower() not in wildcard_hosts else "localhost"
        fallback_parsed = urlparse(
            fallback if "://" in fallback else f"{scheme}://{fallback}"
        )
        host = fallback_parsed.hostname or "localhost"
        host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if fallback_parsed.scheme:
            scheme = fallback_parsed.scheme.lower()

    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None

    # An explicit public URL owns its own default port (for example
    # ``https://chat.example``); do not append the internal Caddy port to it.
    if explicit and parsed_port is None:
        return f"{scheme}://{host_text}"
    effective_port = parsed_port or port
    return f"{scheme}://{host_text}:{effective_port}"


# Keep a descriptive alias for integrations that call the resolver directly.
resolve_caddy_public_url = get_caddy_public_url


def get_operator_web_url(config: object | None = None) -> str:
    """Return the URL an operator can use for the currently managed topology.

    Caddy is the normal public boundary.  When it is explicitly skipped, the
    Next.js listener is the browser-facing service and must be reported instead
    of falling back to the FastAPI API port.
    """

    if not _should_skip_caddy():
        return get_caddy_public_url(config) or "http://127.0.0.1:3002"

    next_port = _service_port(
        config,
        env_names=("AOITALK_NEXT_PORT", "AOITALK_FRONTEND_PORT"),
        config_key="frontend.port",
        default=3002,
    )
    host = _frontend_bind_host().strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{next_port}"


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
    # Startup always builds and serves the canonical production artifact.  A
    # stale NEXT_DIST_DIR inherited from a verification/QA process would make
    # Next write generated types into a custom `.next-*` directory instead.
    env["NEXT_DIST_DIR"] = ".next"
    # This process always launches the production Next server. Do not let a
    # QA/dev shell's NODE_ENV select development behavior for the canonical
    # artifact or make `next start` run in development mode.
    env["NODE_ENV"] = "production"
    # run.bat などGatewayを経由しない起動でも、公開経路のproof検証に使う
    # activation secretをNextにだけ引き継ぐ。
    logger.info(
        "Gateway activation secret: %s",
        _restore_gateway_activation_secret(project_root, env),
    )
    if not env.get("INTERNAL_API_KEY"):
        value = _read_env_value_from_dotenv(project_root, "INTERNAL_API_KEY")
        if value:
            env["INTERNAL_API_KEY"] = value
        else:
            logger.warning(
                "INTERNAL_API_KEY が環境変数にも .env にも見つかりません。"
                "Next.js から Python API への内部委譲が失敗します。"
            )
    if not env.get("PYTHON_API_URL"):
        fastapi_port = _service_port(
            None,
            env_names=("AOITALK_WEB_PORT", "AOITALK_FASTAPI_PORT"),
            config_key="web_interface.port",
            default=3000,
        )
        env["PYTHON_API_URL"] = f"http://127.0.0.1:{fastapi_port}"
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
    if _should_skip_caddy():
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
        # WindowsのTLS ownershipは3状態に分ける。
        #
        # * 完全な AOITALK_CERT_CRT/KEY は、既存の明示的な証明書
        #   override としてCaddyへ渡す。
        # * 片方だけは曖昧な設定なのでfail-closedにする。
        # * どちらも無い場合は、canonical lego stateがsite identityと
        #   metadata上で一致するときだけCaddyへ渡す。
        #
        # certs/ はruntime-owned/ignored dataであり、globの個数を起動条件
        # にしてはいけない。明示されたTLS directiveだけは既存のCaddy
        # 設定として尊重し、その場合のみexplicit-cert用のcatch-all/bind
        # defaultsを適用する。
        cert_path = caddy_env.get("AOITALK_CERT_CRT")
        key_path = caddy_env.get("AOITALK_CERT_KEY")
        if bool(cert_path) != bool(key_path):
            raise RuntimeError(
                "AOITALK_CERT_CRTとAOITALK_CERT_KEYは両方指定してください。"
            )
        if not cert_path and not key_path:
            discovered = _discover_windows_certificate_pair(
                config, project_root=project_root
            )
            if discovered:
                cert_path, key_path = discovered
                caddy_env["AOITALK_CERT_CRT"] = cert_path
                caddy_env["AOITALK_CERT_KEY"] = key_path
        if cert_path and key_path:
            caddy_env.setdefault("AOITALK_CADDY_SITE_ADDRESS", Path(cert_path).stem)
            caddy_env.setdefault(
                "AOITALK_CADDY_TLS_DIRECTIVE",
                f"tls {json.dumps(cert_path)} {json.dumps(key_path)}",
            )
            # Windows personalは明示証明書のSNIを受けつつ、site labelによる
            # Host制約を公開入口へ持ち込まない。IPv4で全インターフェースを
            # 明示的に待ち受ける。
            caddy_env.setdefault(
                "AOITALK_CADDY_SITE_LABEL", f"https://:{caddy_port}"
            )
            caddy_env.setdefault("AOITALK_CADDY_BIND_DIRECTIVE", "bind 0.0.0.0")
        elif caddy_env.get("AOITALK_CADDY_TLS_DIRECTIVE", "").strip():
            # A deployment-specific Caddyfile directive is an explicit TLS
            # override even when the pair is not represented by the legacy
            # AOITALK_CERT_* variables.  Keep it verbatim and preserve the
            # established Windows catch-all/bind defaults.
            caddy_env.setdefault(
                "AOITALK_CADDY_SITE_LABEL", f"https://:{caddy_port}"
            )
            caddy_env.setdefault("AOITALK_CADDY_BIND_DIRECTIVE", "bind 0.0.0.0")
        else:
            # No matching canonical pair: retain Caddy-native HTTPS.  When a
            # configured DDNS/site identity exists, keep that identity rather
            # than silently changing the public URL to localhost.
            native_address = _windows_certificate_identity(config)
            caddy_env.setdefault(
                "AOITALK_CADDY_SITE_LABEL",
                f"{native_address or caddy_env.get('AOITALK_CADDY_SITE_ADDRESS', 'localhost')}:{caddy_port}",
            )
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

    # Keep the operator console concise; the runtime-log path and process
    # details remain available in the application log for diagnostics.
    startup_console.stage("[OK] Caddy")
    logger.info(
        "Started Caddy (PID %s) after FastAPI became ready (runtime log: %s)",
        caddy_proc.pid,
        caddy_runtime_log,
        extra=FILE_ONLY_LOG_EXTRA,
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
    active_log_path = os.getenv("AOITALK_APP_LOG_PATH", "").strip()
    try:
        active_paths = {Path(active_log_path).resolve()} if active_log_path else None
    except Exception:
        active_paths = None
    run_log_housekeeping(layout, active_paths=active_paths)
    skip_caddy = _should_skip_caddy()
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
    if not skip_caddy:
        with _startup_timer.phase("startup.services.caddy.takeover_guard"):
            _assert_caddy_takeover_safe(
                caddy_port=caddy_port,
                fastapi_port=fastapi_port,
            )
    # 起動できない条件は、稼働中のサービスを止める前に確定させる。
    # 先に kill してから検証で落ちると、既に動いていたサーバーまで巻き添えで
    # 停止したまま復旧されず、Web/API が無人のまま残る。
    #
    frontend_dir = project_root / "frontend"
    with _startup_timer.phase("startup.services.frontend.rebuild_check"):
        build_reason, _ = _frontend_build_rebuild_reason(frontend_dir)
    if build_reason:
        raise RuntimeError(
            "通常起動を中止しました。既存のFrontend production buildが現在のsourceと一致しません。"
            "通常起動ではnpm ci/buildを実行しません。"
            "frontendディレクトリで、次のコマンドを別々に実行してください。\n"
            "  cd frontend\n"
            "  npm run build:production"
            f" 詳細: {build_reason}"
        )

    frontend_log_path = layout.frontend_log_path()
    rotate_frontend_log_if_exists(frontend_log_path)
    with _startup_timer.phase("startup.services.frontend.dependencies"):
        if not _next_bin_path(frontend_dir).exists():
            raise RuntimeError(
                "通常起動を中止しました。Frontend依存関係がありません。"
                "通常起動ではnpm ciを実行しません。"
                "frontendディレクトリで、次のコマンドを実行してください。\n"
                "  cd frontend\n"
                "  npm ci"
            )
    with _startup_timer.phase("startup.services.frontend.env_build"):
        frontend_env = _build_frontend_env(project_root)
    with _startup_timer.phase("startup.services.frontend.artifact_validation"):
        try:
            _validate_frontend_startup_artifacts(project_root)
        except RuntimeError as exc:
            raise RuntimeError(
                "通常起動を中止しました。Frontend production artifactがありません。"
                "通常起動ではbuildを実行しません。"
                "frontendディレクトリで、次のコマンドを実行してください。\n"
                "  cd frontend\n"
                "  npm run build:production"
                f" 詳細: {exc}"
            ) from exc
    frontend_ready_message = "[OK] Web UI (既存production build)"

    with _startup_timer.phase("startup.services.port_cleanup"):
        _kill_existing_on_port(fastapi_port)
        if not skip_caddy:
            _kill_existing_on_port(caddy_port)
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
        startup_local_model = _openai_compatible_local_model_id(config)
        startup_managed_runtime = managed_local_runtime_for_model(
            config, startup_local_model
        )
        if startup_managed_runtime in {"strata", "llama_cpp", "freetoken"}:
            # Managed model acquisition can wait on model loading or a shared
            # runtime lease. Defer it until an explicit inference request so
            # unavailable GPU capacity never blocks the Web/API startup.
            local_server_launch_started = False
        else:
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
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if str(frontend_env.get("AOITALK_HEADLESS", "")).strip().lower()
                    in {"1", "true", "yes", "on"}
                    else subprocess.CREATE_NEW_CONSOLE
                ),
            )
            _track_child_process(frontend_proc)
            _record_next_process_tree(
                frontend_proc,
                frontend_dir=project_root / "frontend",
                next_port=next_port,
                frontend_host=frontend_host,
                require_descendants=False,
            )

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
        _record_next_process_tree(
            frontend_proc,
            frontend_dir=project_root / "frontend",
            next_port=next_port,
            frontend_host=frontend_host,
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

    # Only report the Web UI as ready after the Next.js listener has actually
    # accepted connections. A successful build/artifact check alone is not a
    # usable frontend and must not produce a premature [OK] line.
    startup_console.stage(frontend_ready_message)

    caddy_status = (
        "Caddy skipped (AOITALK_SKIP_CADDY)"
        if skip_caddy
        else "Caddy waiting for FastAPI readiness"
    )
    frontend_status = (
        f"Frontend (existing on 127.0.0.1:{next_port})"
        if frontend_proc is None
        else f"Frontend (PID {frontend_proc.pid})"
    )

    if should_start_exo and local_server_launch_started:
        startup_console.stage(
            f"Started {frontend_status} / "
            f"{caddy_status} / exo server starting"
        )
    elif should_start_mlx_lm and local_server_launch_started:
        startup_console.stage(
            f"Started {frontend_status} / "
            f"{caddy_status} / MLX LM server starting"
        )
    elif should_start_llama_cpp and local_server_launch_started:
        startup_console.stage(
            f"Started {frontend_status} / "
            f"{caddy_status} / llama-server起動中"
        )
    else:
        startup_console.stage(f"Started {frontend_status} / {caddy_status}")


def start_services(config: object | None = None) -> None:
    """Start managed services and roll back every child added on failure."""
    tracked_process_start = len(_child_processes)
    qdrant_started = False
    try:
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
        existing_url = _check_existing_aoi_talk_instance(
            config,
            fastapi_port=fastapi_port,
            next_port=next_port,
        )
        if existing_url is not None:
            raise AoiTalkAlreadyRunning(existing_url)
        # main.py has already initialized DB/config; index clients start later.
        # Existing startup may clean these ports, so reject overlap before
        # creating a Qdrant container or touching any currently running service.
        reserved_ports = {
            _service_port(config, env_names=env_names, config_key=key, default=default)
            for env_names, key, default in (
                (("AOITALK_WEB_PORT", "AOITALK_FASTAPI_PORT"), "web_interface.port", 3000),
                (("AOITALK_NEXT_PORT", "AOITALK_FRONTEND_PORT"), "frontend.port", 3002),
                (("AOITALK_CADDY_PORT",), "caddy.port", 6002),
            )
        }
        qdrant_started = start_managed_qdrant(
            config, is_windows=_IS_WINDOWS, reserved_ports=reserved_ports,
        )
        _start_services(config)
    except BaseException:
        try:
            _rollback_tracked_processes(tracked_process_start)
        finally:
            if qdrant_started:
                stop_managed_qdrant()
        raise


def kill_services() -> None:
    """Stop child service processes started by start_services."""
    try:
        for proc in list(_child_processes):
            _terminate_process_tree(proc)
        _child_processes.clear()
        _openai_compatible_local_processes.clear()
    finally:
        stop_managed_qdrant()


# Install the default cross-process lifecycle boundary after defining the direct
# backend. The coordinator and focused backend tests explicitly select direct mode.
from ._shared_local_runtime import install as _install_shared_local_runtime
_install_shared_local_runtime(sys.modules[__name__])
