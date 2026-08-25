"""ローカル OpenAI 互換 LLM サーバーの起動プラン群。

exo / MLX LM 各エンジンの「起動すべきか」の判定、起動引数・起動コマンドの構築、
ローカルサーバーの稼働確認、および exo / MLX LM / 汎用 llama.cpp の起動処理を
提供する。挙動は分割前の `service_manager.py` と同一（機械的移設）。

補足: オーケストレーションを担う `ensure_openai_compatible_local_server` /
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
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from src.llm.openai_compatible_local_profiles import (
    EXO_BASE_URL,
    LLAMA_CPP_DEFAULT_CONTEXT_SIZE,
    LLAMA_CPP_DEFAULT_GPU_LAYERS,
    LLAMA_CPP_DEFAULT_HOST,
    LLAMA_CPP_DEFAULT_PORT,
    LLAMA_CPP_DEFAULT_READINESS_TIMEOUT,
    llama_cpp_model_profile,
    llama_cpp_mtp_metadata,
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_profile_legacy_kind,
    MLX_LM_BASE_URL,
    is_macos,
    local_server_profile_for_model,
    normalize_openai_compatible_base_url,
    openai_compatible_local_base_url,
)

from ._process_utils import (
    _IS_WINDOWS,
    _is_port_open,
    _models_log_dir,
    _track_child_process,
)

logger = logging.getLogger(__name__)

_DEFAULT_AI_ROOT = Path(Path(__file__).resolve().anchor or ".") / "AI"
_DEFAULT_DEV_ROOT = Path(Path(__file__).resolve().anchor or ".") / "Dev"
_DEFAULT_HOT_LLM_ROOT = _DEFAULT_AI_ROOT / "models" / "Hot" / "llm"
_DEFAULT_MUSE_GLIMMER_MODEL_PATH = ""
_LLAMA_CPP_MIN_MUSE_BUILD = int(
    (llama_cpp_model_profile("muse-glimmer-30b") or {}).get(
        "minimum_llama_cpp_build"
    )
    or 0
)
# These options are assembled from the nested runtime settings and must not be
# overridden by ``extra_args``.  Include the common short aliases accepted by
# llama.cpp as well as ``--name=value`` spellings.
_LLAMA_CPP_MANAGED_EXTRA_FLAGS = frozenset(
    {
        "--model",
        "-m",
        "--alias",
        "-a",
        "--host",
        "--port",
        "-p",
        "--ctx-size",
        "--context-size",
        "--n-ctx",
        "-c",
        "--n-gpu-layers",
        "--gpu-layers",
        "-ngl",
        "-n-gpu-layers",
        # MTP/speculative decoding is runtime-owned.  Users configure the
        # formal profile/runtime metadata instead of injecting
        # these flags through extra_args.
        "--spec-type",
        "--spec-draft-model",
        "--model-draft",
        "-md",
        "--spec-draft-hf",
        "--spec-draft-n-max",
    }
)

# ``resolve_llama_cpp_runtime`` is used from the session/generation hot path.
# Keep the optional MTP CLI probe out of that path and reuse the result for the
# lifetime of this process.  The executable identity includes both its
# resolved path and enough stat information to invalidate a cached result when
# a binary is replaced in place.
_LLAMA_CPP_MTP_CAPABILITY_CACHE: dict[tuple[object, ...], bool | None] = {}
_LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK = threading.Lock()
# A probe may take up to five seconds.  Keep serialization scoped to one
# executable identity so an unrelated executable can be probed concurrently.
_LLAMA_CPP_MTP_CAPABILITY_PROBE_LOCKS: dict[tuple[object, ...], threading.Lock] = {}
_LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING = object()


def _llama_cpp_executable_identity(
    executable: str,
) -> tuple[object, ...] | None:
    """Return a cache key for one executable path and its current file identity.

    A missing/unstatable path has no reliable identity.  Returning ``None``
    keeps an inconclusive probe from being reused for a later binary that is
    installed at the same path.
    """

    value = str(executable or "").strip()
    candidate = Path(value).expanduser()
    # ``_resolve_llama_cpp_executable`` normally passes an absolute file path,
    # but resolving a command name here keeps direct capability queries safe as
    # well (and avoids conflating two PATH entries with the same basename).
    if not candidate.is_file():
        located = shutil.which(value)
        if located:
            candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = candidate.absolute()
    try:
        stat = resolved.stat()
    except OSError:
        return None
    # ``normcase`` is a no-op on POSIX and folds drive/path case on Windows,
    # preventing equivalent spellings of one executable from probing twice.
    normalized_path = os.path.normcase(os.path.normpath(str(resolved)))
    return (
        normalized_path,
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(getattr(stat, "st_size", 0)),
        int(getattr(stat, "st_mtime_ns", 0)),
        int(getattr(stat, "st_ctime_ns", 0)),
        int(getattr(stat, "st_mode", 0)),
    )


def _llama_cpp_mtp_cli_cached(executable: str) -> bool | None:
    """Return a previously probed capability without spawning a process."""

    identity = _llama_cpp_executable_identity(executable)
    if identity is None:
        return None
    with _LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK:
        cached = _LLAMA_CPP_MTP_CAPABILITY_CACHE.get(
            identity,
            _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING,
        )
    return None if cached is _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING else cached


def _llama_cpp_mtp_cli_cache_result(
    executable: str,
    capability: bool | None,
    *,
    identity: tuple[object, ...] | None,
) -> None:
    """Store a probe result, including ``None`` (an inconclusive probe).

    Callers that ran a process should pass the identity captured before the
    probe.  Recomputing it after a probe could associate an old result with a
    binary that was replaced in place while the process was running.
    """

    if identity is None:
        return
    with _LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK:
        _LLAMA_CPP_MTP_CAPABILITY_CACHE[identity] = capability


def _config_get(config: object | None, key: str, default: object = None) -> object:
    if config is None:
        return default

    getter = getattr(config, "get", None)
    if callable(getter):
        missing = object()
        try:
            direct = getter(key, missing)
            # A direct dotted key wins only when it really exists.  Plain
            # nested dictionaries return the sentinel for a missing dotted
            # key, after which traversal below resolves ``a.b`` correctly.
            if direct is not missing:
                return direct
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


def _llama_cpp_raw_settings(config: object | None) -> dict[str, object]:
    """Return the nested llama.cpp settings without mutating the config."""

    raw = _config_get(config, "openai_compatible_local.llama_cpp", {})
    return dict(raw) if isinstance(raw, dict) else {}


_PROFILE_RUNTIME_SETTING_KEYS = (
    "model_path",
    "model_alias",
    "context_size",
    "extra_args",
    "gpu_layers",
    "reasoning_effort",
    "mtp_enabled",
    "auto_start",
)


def _llama_cpp_profile_runtime_model_paths(raw: dict[str, object]) -> list[str]:
    """Return saved profile GGUF paths for discovery-root inference.

    ``profile_runtime`` has both a literal-safe encoded shape and a legacy
    dotted/nested shape.  Walking the mapping rather than assuming one key
    format keeps discovery compatible with both.  Only parent directories are
    used as roots; the saved path itself is never reused for another profile.
    """

    roots: list[str] = []
    seen: set[str] = set()

    def _walk(value: object) -> None:
        if not isinstance(value, dict):
            return
        model_path = value.get("model_path")
        if isinstance(model_path, (str, os.PathLike)) and str(model_path).strip():
            try:
                parent = Path(str(model_path)).expanduser().parent
                key = os.path.normcase(os.path.normpath(str(parent)))
            except (OSError, RuntimeError, ValueError):
                key = ""
                parent = None
            if key and key not in seen:
                seen.add(key)
                roots.append(str(parent))
        for child in value.values():
            _walk(child)

    _walk(raw.get("profile_runtime"))
    return roots


def _llama_cpp_discovery_roots(raw: dict[str, object]) -> list[Path]:
    """Return ordered roots used for profile-owned GGUF discovery.

    The checkout-drive ``C:\\AI`` default is retained for compatibility, but
    installations commonly keep large GGUF files on another drive.  An
    explicit root (config or environment) wins, followed by roots inferred
    from already persisted profile paths and the documented per-user model
    directory.  Inference only supplies directories; the selected profile's
    exact filename is still required by the discovery function.
    """

    configured: list[object] = []
    for value in (
        os.getenv("LLAMA_CPP_MODEL_ROOT"),
        os.getenv("AOITALK_LLAMA_CPP_MODEL_ROOT"),
        raw.get("model_root"),
        raw.get("model_dir"),
    ):
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, (list, tuple, set)):
            configured.extend(value)
        else:
            configured.append(value)

    configured.extend(_llama_cpp_profile_runtime_model_paths(raw))
    configured.append(_DEFAULT_HOT_LLM_ROOT)
    try:
        configured.append(Path.home() / "AoiTalk-models")
    except (OSError, RuntimeError):
        pass

    roots: list[Path] = []
    seen: set[str] = set()
    for value in configured:
        text = str(value or "").strip()
        if not text:
            continue
        # Permit a small PATH-like list without splitting Windows drive
        # letters: os.pathsep is `;` on Windows and `:` on POSIX.
        values = text.split(os.pathsep) if os.pathsep in text else [text]
        for item in values:
            candidate = Path(item.strip()).expanduser()
            key = os.path.normcase(os.path.normpath(str(candidate)))
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)
    return roots

_PROFILE_RUNTIME_CANONICAL_KEY_PREFIX = "encoded_"


def _llama_cpp_profile_runtime_key(
    model: str | None,
    *,
    model_profile: dict[str, object] | None = None,
) -> str:
    profile = model_profile or llama_cpp_model_profile(model)
    if profile:
        return str(profile.get("id") or profile.get("served_alias") or model or "").strip()
    return str(model or "").strip()


def _llama_cpp_profile_runtime_storage_key(profile_key: str) -> str:
    """Return the literal-safe key used below ``profile_runtime``.

    ``app_config_store`` treats every dot in a patch key as a nested path
    separator.  Encode every new key, including dotless custom IDs, so a
    user-chosen ID cannot collide with the reserved canonical prefix of a
    dotted profile.  Hex is deliberately used instead of a reversible
    punctuation escape: it cannot introduce another dot (or any other
    config-path separator).
    """

    value = str(profile_key or "").strip()
    if not value:
        return value
    return f"{_PROFILE_RUNTIME_CANONICAL_KEY_PREFIX}{value.encode('utf-8').hex()}"


def _llama_cpp_profile_runtime_entry(settings: dict[str, object]) -> dict[str, object]:
    entry: dict[str, object] = {}
    for key in _PROFILE_RUNTIME_SETTING_KEYS:
        value = settings.get(key)
        if value is None or value == "":
            continue
        entry[key] = value
    return entry


def should_resolve_llama_cpp_runtime_for_engine_switch(
    config: object | None,
    *,
    model: str,
    llama_cpp_settings: dict[str, object] | None = None,
    next_profile: dict[str, object] | None = None,
    previous_runtime_managed: bool = False,
    profile_changed: bool = False,
) -> bool:
    """Return True only when nested llama.cpp runtime should be resolved/persisted."""

    model_id = str(model or "").strip()
    if not model_id or model_id.casefold() == "local-model":
        return False
    if next_profile is not None:
        return True
    if llama_cpp_settings:
        return _llama_cpp_runtime_applies_to_selection(
            config,
            model_id,
            overrides=llama_cpp_settings,
        )
    if not profile_changed or not previous_runtime_managed:
        return False
    return _llama_cpp_runtime_applies_to_selection(
        config,
        model_id,
        overrides=llama_cpp_settings,
    )


def build_llama_cpp_profile_runtime_patch(
    config: object | None,
    *,
    model: str | None,
    settings: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> dict[str, object]:
    """Return config keys to persist per-profile runtime settings."""

    selected_model = _llama_cpp_selected_model(config, model)
    if selected_model.casefold() == "local-model":
        # local-model is an operator-owned external endpoint, not a persisted
        # llama.cpp profile.
        return {}
    model_profile = llama_cpp_model_profile(selected_model)
    profile_key = _llama_cpp_profile_runtime_key(
        selected_model,
        model_profile=model_profile,
    )
    if not profile_key:
        return {}
    resolved = settings or _llama_cpp_settings(
        config,
        model=selected_model,
        is_windows=is_windows,
    )
    entry = _llama_cpp_profile_runtime_entry(resolved)
    if not entry:
        return {}
    storage_key = _llama_cpp_profile_runtime_storage_key(profile_key)
    prefix = f"openai_compatible_local.llama_cpp.profile_runtime.{storage_key}"
    return {f"{prefix}.{field}": entry[field] for field in entry}


def _llama_cpp_previous_local_model(config: object | None) -> str:
    """Identity of the previous *local* selection, not the active cloud model.

    Cloud leftovers such as ``llm_model=gpt-4o`` must not be treated as a
    prior llama.cpp selection.  Fall back to ``llm_model`` only while the
    persisted provider is already ``openai_compatible_local``.
    """

    previous_model = str(
        _config_get(config, "openai_compatible_local.model", "") or ""
    ).strip()
    if previous_model:
        return previous_model
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return ""
    return str(_config_get(config, "llm_model", "") or "").strip()


def _llama_cpp_profile_id_from_path(model_path: str = "") -> str:
    if not str(model_path or "").strip():
        return ""
    profile = llama_cpp_model_profile(model_path=model_path)
    return str((profile or {}).get("id") or "").strip().casefold()


def _llama_cpp_profile_id_from_alias(model_alias: str = "") -> str:
    alias = str(model_alias or "").strip()
    if not alias:
        return ""
    profile = llama_cpp_model_profile(served_alias=alias)
    if profile is None:
        profile = llama_cpp_model_profile(alias)
    return str((profile or {}).get("id") or "").strip().casefold()


def _llama_cpp_leftover_profile_ids(
    *,
    model_path: str = "",
    model_alias: str = "",
) -> set[str]:
    """Profile ids implied by leftover nested path/alias, if any."""

    ids = {
        _llama_cpp_profile_id_from_path(model_path),
        _llama_cpp_profile_id_from_alias(model_alias),
    }
    ids.discard("")
    return ids


def _restore_llama_cpp_profile_runtime(
    raw: dict[str, object],
    model_profile: dict[str, object],
    *,
    override_keys: set[str],
) -> None:
    profile_key = _llama_cpp_profile_runtime_key(
        str(model_profile.get("id") or ""),
        model_profile=model_profile,
    )
    if not profile_key:
        return
    store = raw.get("profile_runtime")
    if not isinstance(store, dict):
        return

    # New writes use a literal-safe encoded key.  Read both the historical
    # literal map entry and the malformed nested shape produced when a dotted
    # profile key was written through app_config_store before that encoding was
    # introduced (for example store["qwen3"]["8-27b"]).
    saved = store.get(_llama_cpp_profile_runtime_storage_key(profile_key))
    if not isinstance(saved, dict):
        saved = store.get(profile_key)
    if not isinstance(saved, dict) and "." in profile_key:
        cursor: object = store
        for part in profile_key.split("."):
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(part)
        if isinstance(cursor, dict):
            saved = cursor
    if not isinstance(saved, dict):
        return
    for field in _PROFILE_RUNTIME_SETTING_KEYS:
        if field in override_keys:
            continue
        if raw.get(field) not in (None, ""):
            continue
        value = saved.get(field)
        if value is not None and value != "":
            raw[field] = value


def _llama_cpp_settings(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> dict[str, object]:
    """Normalize the generic llama-server runtime configuration.

    Values are intentionally resolved here (rather than through a shell) so
    Windows and Linux use exactly the same argument construction.  Explicit
    environment variables are useful for portable native launches and override
    persisted values; otherwise the nested API/config values are used.
    """

    raw = _llama_cpp_raw_settings(config)
    # Keep an untouched copy for ownership/migration checks.  ``overrides``
    # describe the target selection and must not make the previous model look
    # managed merely because the new request supplied a path or alias.
    previous_raw = dict(raw)
    if overrides:
        raw.update({key: value for key, value in overrides.items() if value is not None})
    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    # A known profile supplies only model-specific defaults.  Explicit API,
    # config, and environment values still win over profile defaults.
    model_profile = llama_cpp_model_profile(selected_model)
    previous_model = _llama_cpp_previous_local_model(config)
    previous_profile = llama_cpp_model_profile(previous_model)
    previous_alias = str(previous_raw.get("model_alias") or "").strip()
    previous_path = str(previous_raw.get("model_path") or "").strip()
    previous_runtime_managed = bool(
        previous_profile
        or (
            previous_model
            and previous_model.casefold() != "local-model"
            and (
                previous_alias
                or previous_path
                or (
                    str(os.getenv("LLAMA_CPP_MODEL_ALIAS") or "")
                    .strip()
                    .casefold()
                    == previous_model.casefold()
                )
            )
        )
    )
    previous_selection_id = str(
        previous_profile.get("id") if previous_profile else previous_model
    ).strip().casefold()
    target_selection_id = str(
        model_profile.get("id") if model_profile else selected_model
    ).strip().casefold()
    leftover_path_profile_id = _llama_cpp_profile_id_from_path(previous_path)
    leftover_alias_profile_id = _llama_cpp_profile_id_from_alias(previous_alias)
    leftover_profile_ids = _llama_cpp_leftover_profile_ids(
        model_path=previous_path,
        model_alias=previous_alias,
    )
    leftover_identity_split = bool(
        leftover_path_profile_id
        and leftover_alias_profile_id
        and leftover_path_profile_id != leftover_alias_profile_id
    )
    leftover_belongs_to_target_only = bool(
        target_selection_id and leftover_profile_ids == {target_selection_id}
    )
    leftover_belongs_to_other_profile = bool(
        leftover_profile_ids and target_selection_id not in leftover_profile_ids
    )
    # Keep leftovers that already belong to the *target* profile even when
    # previous identity is ``local-model`` or a cloud ``llm_model``.
    # Path/alias that resolve to two different profiles are inconsistent and
    # must not be kept just because one of them matches the target.
    # An unofficial GGUF filename that matches no profile is not a split;
    # a matching target alias may keep that leftover path.
    selection_changed = bool(
        previous_model and previous_selection_id != target_selection_id
    )
    previous_external_local = previous_model.casefold() == "local-model"
    should_strip_stale_runtime = False
    if leftover_identity_split:
        should_strip_stale_runtime = True
    elif leftover_belongs_to_target_only:
        should_strip_stale_runtime = False
    elif leftover_belongs_to_other_profile or (
        selection_changed
        and (previous_runtime_managed or model_profile or previous_external_local)
    ):
        should_strip_stale_runtime = True
    if should_strip_stale_runtime:
        # Do not carry a prior model's fixed alias/GGUF or profile defaults
        # into a new model during a hot switch.  Explicit request overrides
        # remain authoritative and are therefore retained.
        override_keys = set(overrides or {})
        for key in (
            "model_path",
            "model_alias",
            "context_size",
            "extra_args",
            "gpu_layers",
            "reasoning_effort",
            "mtp_enabled",
            "auto_start",
        ):
            if key not in override_keys:
                raw.pop(key, None)
        model_profile = llama_cpp_model_profile(selected_model)

    # Restore target-profile values whenever they are absent, not only during
    # a detected hot-switch.  This also repairs configurations written by the
    # old dotted-key patcher when the application restarts on the same profile.
    # _restore_llama_cpp_profile_runtime only fills missing values, so an
    # explicit current setting (including auto_start=False) remains authoritative.
    if model_profile:
        _restore_llama_cpp_profile_runtime(
            raw,
            model_profile,
            override_keys=set(overrides or {}),
        )

    def _text(key: str, *env_names: str, default: str = "") -> str:
        env_value = next(
            (
                os.getenv(env_name)
                for env_name in env_names
                if os.getenv(env_name) is not None
                and str(os.getenv(env_name)).strip() != ""
            ),
            None,
        )
        if env_value is not None:
            return str(env_value).strip()
        raw_value = raw.get(key)
        if raw_value is not None and str(raw_value).strip() != "":
            return str(raw_value).strip()
        return str(default).strip()

    def _int(key: str, *env_names: str, default: int, allow_negative: bool = False) -> int:
        env_value = next(
            (
                os.getenv(env_name)
                for env_name in env_names
                if os.getenv(env_name) is not None
                and str(os.getenv(env_name)).strip() != ""
            ),
            None,
        )
        if env_value is not None:
            value: object = env_value
        else:
            raw_value = raw.get(key)
            if raw_value is not None and str(raw_value).strip() != "":
                value = raw_value
            else:
                return default
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        if parsed < 0 and not allow_negative:
            return default
        return parsed

    def _float(key: str, *env_names: str, default: float) -> float:
        value: object = next(
            (
                os.getenv(env_name)
                for env_name in env_names
                if os.getenv(env_name) is not None
                and str(os.getenv(env_name)).strip() != ""
            ),
            raw.get(key),
        )
        if (value is None or str(value).strip() == "") and key == "readiness_timeout":
            value = raw.get("readiness_timeout_seconds")
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _bool(key: str, *env_names: str, default: bool) -> bool:
        value: object = next(
            (
                os.getenv(env_name)
                for env_name in env_names
                if os.getenv(env_name) is not None
                and str(os.getenv(env_name)).strip() != ""
            ),
            raw.get(key),
        )
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    executable = _text(
        "executable",
        "LLAMA_CPP_EXECUTABLE",
        "LLAMA_SERVER_EXE",
        default="",
    )
    model_path_env_names = ["LLAMA_CPP_MODEL_PATH"]
    if llama_cpp_profile_legacy_kind(model_profile) == "muse":
        model_path_env_names.append("MUSE_GLIMMER_MODEL_PATH")
    model_path = _text(
        "model_path",
        *model_path_env_names,
        default=_DEFAULT_MUSE_GLIMMER_MODEL_PATH,
    )
    discovery_roots = _llama_cpp_discovery_roots(raw)
    if not str(model_path or "").strip() and model_profile:
        try:
            discovered = _discover_llama_cpp_model_path(
                model_profile,
                roots=discovery_roots,
            )
        except TypeError as exc:
            # Keep lightweight integrations that monkeypatch the historical
            # one-argument helper working while the optional roots contract
            # rolls out.
            if "roots" not in str(exc):
                raise
            discovered = _discover_llama_cpp_model_path(model_profile)
        if discovered:
            model_path = discovered
    model_alias = _text(
        "model_alias",
        "LLAMA_CPP_MODEL_ALIAS",
        default=(
            str(model_profile.get("served_alias") or "")
            if model_profile
            else selected_model
        ),
    )
    if not model_alias:
        model_alias = (
            str(model_profile.get("served_alias") or "")
            if model_profile
            else selected_model
        )
    host = _text("host", "LLAMA_CPP_HOST", default=LLAMA_CPP_DEFAULT_HOST) or LLAMA_CPP_DEFAULT_HOST
    port = _int("port", "LLAMA_CPP_PORT", default=LLAMA_CPP_DEFAULT_PORT)
    context_size = _int(
        "context_size",
        "LLAMA_CPP_CONTEXT_SIZE",
        default=(
            int(model_profile.get("default_context_size"))
            if model_profile and model_profile.get("default_context_size")
            else LLAMA_CPP_DEFAULT_CONTEXT_SIZE
        ),
    )
    gpu_layers = _int(
        "gpu_layers",
        "LLAMA_CPP_GPU_LAYERS",
        default=LLAMA_CPP_DEFAULT_GPU_LAYERS,
        allow_negative=True,
    )
    timeout = _float(
        "readiness_timeout",
        "readiness_timeout_seconds",
        "LLAMA_CPP_READINESS_TIMEOUT",
        default=LLAMA_CPP_DEFAULT_READINESS_TIMEOUT,
    )
    env_extra_args = os.getenv("LLAMA_CPP_EXTRA_ARGS")
    extra_args: object = (
        env_extra_args
        if env_extra_args is not None and env_extra_args.strip()
        else raw.get("extra_args")
    )
    if extra_args is None or extra_args == "":
        extra_args = (
            list(model_profile.get("default_args") or [])
            if model_profile
            else ""
        )
    if isinstance(extra_args, str):
        try:
            extra_args = shlex.split(
                extra_args,
                posix=not bool(is_windows if is_windows is not None else _IS_WINDOWS),
            )
        except ValueError as exc:
            raise RuntimeError(f"llama.cpp extra_args が不正です: {exc}") from exc
    if not isinstance(extra_args, (list, tuple)):
        raise RuntimeError("llama.cpp extra_args は配列または文字列で指定してください")
    normalized_extra_args = [str(item) for item in extra_args if str(item).strip()]
    effort_metadata = llama_cpp_reasoning_effort_metadata(profile=model_profile)
    configured_effort = str(raw.get("reasoning_effort") or "").strip().lower()
    reasoning_effort = (
        configured_effort
        if effort_metadata and configured_effort in effort_metadata["options"]
        else (str(effort_metadata["default"]) if effort_metadata else None)
    )
    # MTP is resolved strictly from profile metadata.  Unknown profiles and
    # the external local-model endpoint receive an inert projection so stale
    # settings cannot leak into a future managed launch.
    mtp_metadata = llama_cpp_mtp_metadata(profile=model_profile)
    if model_profile and mtp_metadata:
        mtp_enabled = _bool(
            "mtp_enabled",
            default=bool(mtp_metadata.get("default_enabled")),
        )
        mtp_resolution = _resolve_llama_cpp_mtp(
            model_profile,
            enabled=mtp_enabled,
            base_model_path=model_path,
        )
    else:
        mtp_enabled = False
        mtp_resolution = {
            "supported": False,
            "available": False,
            "status": "not_applicable" if selected_model.casefold() == "local-model" else "unavailable",
            "reason": (
                "local-modelは外部OpenAI互換serverのため、AoiTalkはMTPを管理しません。"
                if selected_model.casefold() == "local-model"
                else "選択したllama.cpp profileに互換性のあるMTP artifactがありません。"
            ),
            "artifact_path": "",
            "mode": "unavailable",
        }
    return {
        "executable": executable,
        "model_path": model_path,
        "model_root": str(discovery_roots[0]) if discovery_roots else "",
        "model_alias": model_alias,
        "host": host,
        "port": port,
        "context_size": context_size,
        "gpu_layers": gpu_layers,
        "extra_args": normalized_extra_args,
        "auto_start": _bool("auto_start", "LLAMA_CPP_AUTO_START", default=True),
        "readiness_timeout": timeout,
        "reasoning_effort": reasoning_effort,
        # User-controlled settings are deliberately limited to these two
        # persisted keys.  Availability/status/reason are computed below and
        # must never be restored from profile_runtime.
        "mtp_enabled": mtp_enabled,
        # Read-only resolved path; user input is intentionally not accepted.
        "mtp_model_path": str(mtp_resolution.get("artifact_path") or ""),
        "mtp_supported": bool(mtp_resolution.get("supported")),
        "mtp_available": bool(mtp_resolution.get("available")),
        "mtp_status": str(mtp_resolution.get("status") or "unavailable"),
        "mtp_reason": str(mtp_resolution.get("reason") or ""),
        "mtp_artifact_path": str(mtp_resolution.get("artifact_path") or ""),
        "mtp_resolved_model_path": str(mtp_resolution.get("artifact_path") or ""),
        "mtp_mode": str(mtp_resolution.get("mode") or "unavailable"),
        # Read-only metadata consumed by catalog/UI and version validation.
        "profile_id": str(model_profile.get("id") or "") if model_profile else "",
        "minimum_llama_cpp_build": (
            model_profile.get("minimum_llama_cpp_build") if model_profile else None
        ),
        "reasoning_tools_minimum_llama_cpp_build": (
            model_profile.get("reasoning_tools_minimum_llama_cpp_build")
            if model_profile
            else None
        ),
        "required_args": list(model_profile.get("required_args") or [])
        if model_profile
        else [],
        "jinja_required": bool(model_profile.get("jinja_required"))
        if model_profile
        else False,
        "native_context_length": (
            (
                model_profile.get("native_context_size")
                or model_profile.get("native_context_length")
            )
            if model_profile
            else None
        ),
        "native_context_size": (
            model_profile.get("native_context_size") if model_profile else None
        ),
    }


def _llama_cpp_mtp_runtime_fields(settings: dict[str, object]) -> dict[str, object]:
    """Project computed MTP fields alongside canonical runtime state."""

    return {
        "mtp_enabled": bool(settings.get("mtp_enabled")),
        "mtp_supported": bool(settings.get("mtp_supported")),
        "mtp_available": bool(settings.get("mtp_available")),
        "mtp_status": str(settings.get("mtp_status") or "unavailable"),
        "mtp_reason": str(settings.get("mtp_reason") or ""),
        "mtp_artifact_path": str(settings.get("mtp_artifact_path") or ""),
        "mtp_resolved_model_path": str(
            settings.get("mtp_resolved_model_path")
            or settings.get("mtp_artifact_path")
            or ""
        ),
        "mtp_mode": str(settings.get("mtp_mode") or "unavailable"),
    }


def _llama_cpp_mtp_cli_supported(executable: str) -> bool | None:
    """Best-effort capability probe for MTP argv support.

    A build number cannot be inferred reliably from all packaged binaries.
    When ``--help`` explicitly omits the MTP flags, callers can fall back to
    base-only launch instead of making this optimization a hard failure. If
    the executable cannot be probed (for example a test/dummy path), return
    ``None`` and preserve the existing launch behavior.  The result is cached
    by executable identity, so repeated calls never synchronously spawn the
    same ``--help`` probe.
    """

    identity = _llama_cpp_executable_identity(executable)
    if identity is None:
        # Do not cache a result when the executable cannot be stat'ed.  There
        # is no stable identity to distinguish a later binary at this path.
        try:
            result = subprocess.run(
                [executable, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return (
            None
            if not output.strip()
            else "--spec-type" in output and "draft-mtp" in output
        )

    # Only the short cache/lock-map operations use the global lock.  The
    # potentially five-second subprocess is serialized by an identity-scoped
    # lock, so unrelated executables do not block each other.
    with _LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK:
        cached = _LLAMA_CPP_MTP_CAPABILITY_CACHE.get(
            identity,
            _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING,
        )
        probe_lock = _LLAMA_CPP_MTP_CAPABILITY_PROBE_LOCKS.setdefault(
            identity,
            threading.Lock(),
        )
    if cached is not _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING:
        return cached

    with probe_lock:
        # Another caller may have completed the probe while this caller was
        # waiting for the per-identity lock.
        with _LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK:
            cached = _LLAMA_CPP_MTP_CAPABILITY_CACHE.get(
                identity,
                _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING,
            )
        if cached is not _LLAMA_CPP_MTP_CAPABILITY_CACHE_MISSING:
            return cached
        try:
            result = subprocess.run(
                [executable, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            capability: bool | None = None
        else:
            output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            capability = (
                None
                if not output.strip()
                else "--spec-type" in output and "draft-mtp" in output
            )
        with _LLAMA_CPP_MTP_CAPABILITY_CACHE_LOCK:
            _LLAMA_CPP_MTP_CAPABILITY_CACHE[identity] = capability
        return capability


def _llama_cpp_settings_with_mtp_cli_capability(
    settings: dict[str, object],
    executable: str,
    *,
    probe: bool = True,
) -> dict[str, object]:
    """Project explicit CLI incompatibility without blocking base startup.

    Launch/preflight callers use the default ``probe=True`` and therefore
    populate the process-global capability cache exactly once.  Runtime
    resolution passes ``probe=False`` so an unprobed executable remains
    unknown and this side-effect-free hot path never spawns ``--help``.
    """

    if not bool(settings.get("mtp_enabled")) or not bool(settings.get("mtp_available")):
        return settings
    probe_identity = _llama_cpp_executable_identity(executable) if probe else None
    capability = (
        _llama_cpp_mtp_cli_supported(executable)
        if probe
        else _llama_cpp_mtp_cli_cached(executable)
    )
    # Tests/integrations may replace the probe function.  Persist that result
    # too, so a subsequent side-effect-free resolution observes the same
    # capability without calling the replacement again.
    if probe:
        _llama_cpp_mtp_cli_cache_result(
            executable,
            capability,
            identity=probe_identity,
        )
    if capability is not False:
        return settings
    adjusted = dict(settings)
    adjusted["mtp_available"] = False
    adjusted["mtp_status"] = "unsupported_build"
    adjusted["mtp_reason"] = (
        "llama-serverが--spec-type draft-mtpを提供しないため、"
        "MTPを無効化して本体を通常モードで起動します。"
    )
    logger.warning(
        "llama-server does not advertise draft-mtp; starting the base model without MTP"
    )
    return adjusted


def resolve_llama_cpp_runtime(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> dict[str, object]:
    """Resolve one canonical llama.cpp runtime contract and readiness state.

    Every caller that needs to decide whether a registered profile is usable
    should consume this projection instead of independently interpreting the
    nested settings.  Resolution is side-effect free: GGUF discovery is
    read-only and executable/version checks are deferred to launch, while the
    returned ``error`` is suitable for preflight/UI diagnostics.

    ``local-model`` is intentionally represented as ``external`` even when a
    stale nested ``llama_cpp`` mapping remains in the persisted config.  This
    keeps operator-owned OpenAI-compatible endpoints from being hijacked by a
    previous managed profile.
    """

    selected_model = _llama_cpp_selected_model(config, model)
    external = selected_model.casefold() == "local-model"
    active_provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    explicit_target_model = bool(str(model or "").strip())
    has_nested_runtime = bool(
        _config_get(config, "openai_compatible_local.llama_cpp", None)
    )
    # A persisted local runtime may remain nested while the global engine has
    # switched back to a cloud provider.  Do not let catalog/global checks
    # inherit that stale profile.  Request-scoped target configs explicitly
    # pass a model (or runtime overrides), so they remain eligible even when
    # the persisted global provider is cloud-based.
    runtime_context_applies = bool(
        active_provider == "openai_compatible_local"
        or (
            not active_provider
            and (has_nested_runtime or bool(overrides))
        )
        or (
            active_provider
            and active_provider != "openai_compatible_local"
            and (explicit_target_model or bool(overrides))
        )
    )
    applies = bool(
        not external
        # Lightweight provider clients in unit/schema paths may carry only a
        # model name and no provider/runtime config.  They use the generic
        # OpenAI-compatible transport and must not be forced through a
        # managed llama.cpp preflight solely because the model ID is known.
        and runtime_context_applies
        and _llama_cpp_runtime_applies_to_selection(
            config,
            selected_model,
            overrides=overrides,
            is_windows=is_windows,
        )
    )
    settings = _llama_cpp_settings(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=is_windows,
    )
    profile = _llama_cpp_profile_for_selection(
        selected_model,
        model_alias=str(settings.get("model_alias") or ""),
        model_path=str(settings.get("model_path") or ""),
    )
    # Path validation must use the explicitly selected profile, not a profile
    # inferred from a stale/wrong GGUF filename.  Unknown/custom selections
    # therefore keep their permissive path contract.
    selected_profile = llama_cpp_model_profile(selected_model)
    managed = bool(
        applies
        and _llama_cpp_is_managed_selection(
            config,
            selected_model,
            settings,
            overrides=overrides,
        )
    )
    auto_start = bool(settings.get("auto_start"))
    model_path = str(settings.get("model_path") or "").strip()
    raw = _llama_cpp_raw_settings(config)
    raw_path = str(raw.get("model_path") or "").strip()
    env_path = str(
        os.getenv("LLAMA_CPP_MODEL_PATH")
        or (
            os.getenv("MUSE_GLIMMER_MODEL_PATH")
            if llama_cpp_profile_legacy_kind(profile) == "muse"
            else ""
        )
        or ""
    ).strip()
    path_source = (
        "configured"
        if model_path and raw_path and Path(raw_path).expanduser() == Path(model_path).expanduser()
        else "environment"
        if model_path and env_path
        else "discovered"
        if model_path and not raw_path and not env_path
        else "configured"
        if model_path and raw_path
        else "missing"
    )
    if not managed:
        state = "external" if external else "unmanaged"
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": False,
            "auto_start": auto_start,
            "state": state,
            "model_path": model_path,
            "model_path_source": path_source,
            "model_path_status": "not_applicable",
            "executable_status": "not_applicable",
            "minimum_build": None,
            "error": None,
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    if not auto_start:
        # A managed profile may intentionally attach to a manually started
        # endpoint.  Do not call this an executable/path failure; the manual
        # connection validator owns reachability checks for this mode.
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": True,
            "auto_start": False,
            "state": "manual",
            "model_path": model_path,
            "model_path_source": path_source,
            "model_path_status": "configured" if model_path else "missing",
            "executable_status": "not_required",
            "minimum_build": _llama_cpp_effective_minimum_build(profile),
            "error": None,
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    path_profile_error = _llama_cpp_model_path_profile_mismatch_error(
        selected_model,
        selected_profile,
        model_path,
    )
    if path_profile_error:
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": True,
            "auto_start": True,
            "state": "model_path_profile_mismatch",
            "model_path": model_path,
            "model_path_source": path_source,
            "model_path_status": "mismatch",
            "executable_status": "not_checked",
            "minimum_build": _llama_cpp_effective_minimum_build(profile),
            "error": path_profile_error,
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    if not model_path:
        label = str((profile or {}).get("label") or selected_model or "llama.cpp")
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": True,
            "auto_start": True,
            "state": "missing_model_path",
            "model_path": "",
            "model_path_source": "missing",
            "model_path_status": "missing",
            "executable_status": "not_checked",
            "minimum_build": _llama_cpp_effective_minimum_build(profile),
            "error": (
                f"{label} のGGUF model_pathが未設定です（model_path is not configured）。"
                "自動検出対象の保存先にGGUFを配置するか、"
                "openai_compatible_local.llama_cpp.model_pathを指定してください。"
            ),
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    path = Path(model_path).expanduser()
    if not path.is_file():
        label = str((profile or {}).get("label") or selected_model or "llama.cpp")
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": True,
            "auto_start": True,
            "state": "model_path_not_found",
            "model_path": model_path,
            "model_path_source": path_source,
            "model_path_status": "not_found",
            "executable_status": "not_checked",
            "minimum_build": _llama_cpp_effective_minimum_build(profile),
            "error": f"{label} のGGUF model_pathが存在しません（model_path does not exist）: {model_path}",
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    try:
        executable = _resolve_llama_cpp_executable(
            str(settings.get("executable") or ""),
            is_windows=is_windows,
        )
    except Exception as exc:
        return {
            "model": selected_model,
            "profile": profile,
            "settings": settings,
            "managed": True,
            "auto_start": True,
            "state": "executable_not_found",
            "model_path": model_path,
            "model_path_source": path_source,
            "model_path_status": "ok",
            "executable_status": "not_found",
            "minimum_build": _llama_cpp_effective_minimum_build(profile),
            "error": str(exc),
            **_llama_cpp_mtp_runtime_fields(settings),
        }

    # Resolution is called from session turn construction and generation
    # hot paths.  Only project an already-known incompatibility here; the
    # first actual launch/preflight performs the one-time ``--help`` probe.
    settings = _llama_cpp_settings_with_mtp_cli_capability(
        settings,
        executable,
        probe=False,
    )

    return {
        "model": selected_model,
        "profile": profile,
        "settings": settings,
        "managed": True,
        "auto_start": True,
        "state": "ready",
        "model_path": model_path,
        "model_path_source": path_source,
        "model_path_status": "ok",
        "executable_status": "ok",
        "minimum_build": _llama_cpp_effective_minimum_build(profile),
        "error": None,
        **_llama_cpp_mtp_runtime_fields(settings),
    }


def _llama_cpp_effective_minimum_build(
    model_profile: dict[str, object] | None,
) -> int | None:
    """Return the profile's effective load/tool minimum from metadata."""

    if not model_profile:
        return None
    minimum = model_profile.get("minimum_llama_cpp_build")
    capability_minimum = model_profile.get("reasoning_tools_minimum_llama_cpp_build")
    capabilities = model_profile.get("capabilities")
    supports_reasoning = bool(
        capabilities.get("reasoning") if isinstance(capabilities, dict) else model_profile.get("supports_reasoning")
    )
    supports_tools = bool(
        capabilities.get("tools") if isinstance(capabilities, dict) else model_profile.get("supports_tools")
    )
    if supports_reasoning and supports_tools and capability_minimum is not None:
        try:
            capability_value = int(capability_minimum)
            minimum = max(int(minimum or 0), capability_value)
        except (TypeError, ValueError):
            pass
    try:
        return int(minimum) if minimum is not None else None
    except (TypeError, ValueError):
        return None


def llama_cpp_runtime_requirement(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> str | None:
    """Render the selected profile's minimum build from registry metadata."""

    resolved = resolve_llama_cpp_runtime(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    minimum = resolved.get("minimum_build")
    profile = resolved.get("profile")
    if minimum is None:
        return None
    label = (
        str(profile.get("label") or resolved.get("model") or "llama.cpp")
        if isinstance(profile, dict)
        else str(resolved.get("model") or "llama.cpp")
    )
    return f"{label}にはllama.cpp b{minimum}以上が必要です。"


def _llama_cpp_managed_extra_flag(token: object) -> str | None:
    """Return the managed option name represented by one extra argv token."""

    text = str(token or "").strip()
    if not text.startswith("-"):
        return None
    name = text.split("=", 1)[0].casefold()
    return name if name in _LLAMA_CPP_MANAGED_EXTRA_FLAGS else None


def _validate_llama_cpp_extra_args(extra_args: object) -> None:
    """Reject runtime-owned llama.cpp flags from user-provided extras."""

    if isinstance(extra_args, str):
        try:
            tokens = shlex.split(extra_args, posix=not _IS_WINDOWS)
        except ValueError as exc:
            raise RuntimeError(f"llama.cpp extra_args が不正です: {exc}") from exc
    elif isinstance(extra_args, (list, tuple)):
        tokens = [str(item) for item in extra_args]
    else:
        tokens = []
    for token in tokens:
        managed = _llama_cpp_managed_extra_flag(token)
        if managed:
            raise RuntimeError(
                "llama.cpp extra_args では管理対象引数を指定できません: "
                f"{token!r}（{managed} は nested 設定で指定してください）"
            )


def _llama_cpp_is_muse_selection(
    model: str | None,
    *,
    model_alias: str = "",
    model_path: str = "",
) -> bool:
    # Compatibility predicate retained for existing callers/tests.  New
    # model-specific behaviour is resolved from the profile registry.
    profile = llama_cpp_model_profile(
        model,
        model_path=model_path,
        served_alias=model_alias,
    )
    return llama_cpp_profile_legacy_kind(profile) == "muse"


def _discover_llama_cpp_model_path(
    model_profile: dict[str, object] | None,
    *,
    roots: list[Path] | None = None,
) -> str:
    """Best-effort GGUF discovery for registered llama.cpp profiles."""

    if not model_profile:
        return ""
    discovery_roots = list(roots or [_DEFAULT_HOT_LLM_ROOT])
    official = str(
        model_profile.get("gguf_filename")
        or model_profile.get("filename")
        or model_profile.get("model_filename")
        or ""
    ).strip()
    for root in discovery_roots:
        if not root.is_dir():
            continue
        if official:
            for candidate in root.rglob(official):
                if candidate.is_file():
                    return str(candidate.resolve())

    # Registered non-Muse profiles declare an exact GGUF filename.  Never
    # silently pick an arbitrary newest file from their profile directory:
    # that can load a different model while the served alias still appears
    # superficially valid.  Muse retains its historical directory fallback
    # because its published k-quant artifact has no one canonical filename.
    if official and llama_cpp_profile_legacy_kind(model_profile) != "muse":
        return ""

    search_dirs: list[Path] = []
    for root in discovery_roots:
        if llama_cpp_profile_legacy_kind(model_profile) == "muse":
            search_dirs.extend(
                [
                    root / "Muse-Glimmer-30B",
                    root / "muse-glimmer-30b",
                ]
            )
        profile_id = str(model_profile.get("id") or "").strip()
        if profile_id:
            search_dirs.append(root / profile_id)
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        ggufs = sorted(
            directory.glob("*.gguf"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if ggufs:
            return str(ggufs[0].resolve())
    return ""


def _discover_llama_cpp_mtp_artifact_path(
    model_profile: dict[str, object] | None,
    *,
    base_model_path: str = "",
) -> str:
    """Discover only companion filenames declared by profile metadata.

    The existing managed-model root is reused, but unlike base GGUF
    discovery this function never chooses an arbitrary ``*.gguf``.  That is
    important for Qwen3.8 Heretic: its profile intentionally declares no
    compatible companion and therefore cannot accidentally pair with the
    official Qwen MTP weights.
    """

    metadata = llama_cpp_mtp_metadata(profile=model_profile)
    if not metadata or not bool(metadata.get("supported")):
        return ""
    filenames = [
        str(filename).strip()
        for filename in metadata.get("companion_filenames", [])
        if str(filename).strip()
    ]
    if not filenames:
        return ""

    search_dirs: list[Path] = []
    if _DEFAULT_HOT_LLM_ROOT.is_dir():
        search_dirs.append(_DEFAULT_HOT_LLM_ROOT)
    base_path = Path(str(base_model_path or "")).expanduser()
    if base_path.is_file() and base_path.parent not in search_dirs:
        search_dirs.append(base_path.parent)
    profile_id = str((model_profile or {}).get("id") or "").strip()
    if profile_id:
        profile_dir = _DEFAULT_HOT_LLM_ROOT / profile_id
        if profile_dir not in search_dirs:
            search_dirs.append(profile_dir)

    for filename in filenames:
        for directory in search_dirs:
            if not directory.is_dir():
                continue
            # Exact metadata-declared filenames only; no wildcard or newest
            # arbitrary GGUF fallback is permitted for MTP companions.
            try:
                matches = directory.rglob(filename)
            except OSError:
                continue
            for candidate in matches:
                if candidate.is_file() and candidate.name == filename:
                    return str(candidate.resolve())
    return ""


def _resolve_llama_cpp_mtp(
    model_profile: dict[str, object],
    *,
    enabled: bool,
    base_model_path: str = "",
) -> dict[str, object]:
    """Resolve the read-only MTP availability projection for one profile."""

    metadata = llama_cpp_mtp_metadata(profile=model_profile) or {
        "supported": False,
        "default_enabled": False,
        "mode": "unavailable",
        "companion_filenames": [],
        "reason": "選択したllama.cpp profileにMTP metadataがありません。",
    }
    supported = bool(metadata.get("supported"))
    mode = str(metadata.get("mode") or "unavailable")
    base_reason = str(metadata.get("reason") or "").strip()
    if not supported:
        return {
            "supported": False,
            "available": False,
            "status": (
                "disabled"
                if not enabled
                else "compatibility_unverified"
                if mode == "unavailable"
                else "unavailable"
            ),
            "reason": (
                "MTPはOFFです。"
                if not enabled
                else base_reason or "選択したGGUFではMTPを利用できません。"
            ),
            "artifact_path": "",
            "mode": mode,
        }

    if mode == "embedded":
        # Embedded NextN/MTP has no separate draft artifact. (Current AoiTalk
        # profiles remain conservative and use unavailable until verified.)
        return {
            "supported": True,
            "available": True,
            "status": "disabled" if not enabled else "ready",
            "reason": (
                "MTPはOFFです。"
                if not enabled
                else base_reason or "埋め込みMTP/NextNを利用できます。"
            ),
            "artifact_path": "",
            "mode": mode,
        }

    # Companion mode is deliberately explicit. Resolve only exact filenames
    # declared by profile metadata; never accept a free-form path or infer a
    # companion from a quantization/model-id substring.
    declared_filenames = {
        Path(str(item)).name
        for item in metadata.get("companion_filenames", [])
        if str(item).strip()
    }
    if not declared_filenames:
        return {
            "supported": True,
            "available": False,
            "status": "disabled" if not enabled else "unavailable",
            "reason": (
                "MTPはOFFです。"
                if not enabled
                else "互換性を確認したMTP artifact filenameが未宣言です。"
            ),
            "artifact_path": "",
            "mode": mode,
        }
    artifact_path = _discover_llama_cpp_mtp_artifact_path(
        model_profile,
        base_model_path=base_model_path,
    )
    if not artifact_path:
        filenames = ", ".join(
            str(item)
            for item in metadata.get("companion_filenames", [])
            if str(item).strip()
        )
        reason = base_reason or "互換性のあるMTP artifactを解決できません。"
        if filenames:
            reason = f"{reason} 宣言済み候補: {filenames}"
        return {
            "supported": True,
            "available": False,
            "status": "disabled" if not enabled else "unavailable",
            "reason": "MTPはOFFです。" if not enabled else reason,
            "artifact_path": "",
            "mode": mode,
        }
    return {
        "supported": True,
        "available": True,
        "status": "disabled" if not enabled else "ready",
        "reason": "MTPはOFFです。" if not enabled else "互換性のあるMTP artifactを利用できます。",
        "artifact_path": artifact_path,
        "mode": mode,
    }


def _llama_cpp_profile_for_selection(
    model: str | None,
    *,
    model_alias: str = "",
    model_path: str = "",
) -> dict[str, object] | None:
    """Resolve model-specific llama.cpp metadata without model-ID branches."""

    return llama_cpp_model_profile(
        model,
        model_path=model_path,
        served_alias=model_alias,
    )


def _llama_cpp_profile_expected_filename(
    model_profile: dict[str, object] | None,
) -> str:
    """Return the profile's declared GGUF filename, when one exists."""

    if not isinstance(model_profile, dict):
        return ""
    # ``gguf_filename`` is the current registry field.  Keep the aliases for
    # older/custom profile metadata so a missing contract remains permissive.
    for key in (
        "expected_gguf_filename",
        "expected_filename",
        "gguf_filename",
        "filename",
        "model_filename",
        "official_filename",
    ):
        value = str(model_profile.get(key) or "").strip()
        if value:
            return Path(value).name
    return ""


def _llama_cpp_filename_family_marker(filename: str) -> str:
    """Extract a conservative family/version marker from a model filename."""

    stem = Path(str(filename or "")).stem.casefold()
    match = re.search(r"([a-z][a-z0-9]*(?:[._-]\d+)+)", stem)
    if not match:
        return ""
    return re.sub(r"[-_]", ".", match.group(1))


def _llama_cpp_model_path_profile_mismatch_error(
    selected_model: str,
    model_profile: dict[str, object] | None,
    model_path: str,
) -> str | None:
    """Return an actionable mismatch error for a known profile/path pair.

    A known profile may still use a user-named/custom GGUF path, so an
    arbitrary filename is accepted.  We reject only an explicitly registered
    different profile or a filename with an unambiguous family/version marker
    (for example Qwen3.8 selected with a Qwen3.6 GGUF).  Unknown profiles and
    metadata without an expected filename retain their existing contract.
    """

    expected_filename = _llama_cpp_profile_expected_filename(model_profile)
    actual_filename = Path(str(model_path or "")).name
    if not expected_filename or not actual_filename:
        return None

    selected_profile_id = str(
        (model_profile or {}).get("id") or selected_model or ""
    ).strip().casefold()
    path_profile = llama_cpp_model_profile(model_path=actual_filename)
    path_profile_id = str((path_profile or {}).get("id") or "").strip().casefold()
    mismatch_reason = ""
    if path_profile_id and selected_profile_id and path_profile_id != selected_profile_id:
        mismatch_reason = (
            f"path profile={path_profile_id!r}"
            f" ({str((path_profile or {}).get('label') or path_profile_id)})"
        )
    else:
        expected_marker = _llama_cpp_filename_family_marker(expected_filename)
        actual_marker = _llama_cpp_filename_family_marker(actual_filename)
        if expected_marker and actual_marker and expected_marker != actual_marker:
            mismatch_reason = (
                f"model family marker={actual_marker!r}"
                f" (expected {expected_marker!r})"
            )

    if not mismatch_reason:
        return None
    label = str(
        (model_profile or {}).get("label") or selected_model or "llama.cpp"
    ).strip()
    return (
        f"{label} のmodel_pathが選択profileと一致しないGGUFです（profile mismatch）。"
        f" selected={selected_model!r}, expected filename={expected_filename!r},"
        f" model_path={actual_filename!r}; {mismatch_reason}。"
    )


def _validate_llama_cpp_model_path_profile(
    selected_model: str,
    model_profile: dict[str, object] | None,
    model_path: str,
) -> None:
    """Raise the canonical profile/path mismatch error used by launchers."""

    error = _llama_cpp_model_path_profile_mismatch_error(
        selected_model,
        model_profile,
        model_path,
    )
    if error:
        raise RuntimeError(error)


def _llama_cpp_is_profile_selection(
    model: str | None,
    *,
    model_alias: str = "",
    model_path: str = "",
) -> bool:
    return _llama_cpp_profile_for_selection(
        model,
        model_alias=model_alias,
        model_path=model_path,
    ) is not None


def _llama_cpp_is_managed_selection(
    config: object | None,
    selected_model: str,
    settings: dict[str, object],
    *,
    overrides: dict[str, object] | None = None,
) -> bool:
    """Whether this selection is explicitly owned by the llama.cpp runtime."""

    model_id = str(selected_model or "").strip()
    if not model_id or model_id.casefold() == "local-model":
        # Keep the historical generic local-model connection untouched.
        return False
    if _llama_cpp_is_profile_selection(
        model_id,
        model_alias=str(settings.get("model_alias") or ""),
        model_path=str(settings.get("model_path") or ""),
    ):
        return True
    alias = str(settings.get("model_alias") or "").strip()
    if alias and model_id.casefold() == alias.casefold():
        return True
    raw = _llama_cpp_raw_settings(config)
    override_keys = set(overrides or {})
    return bool(
        (str(settings.get("model_path") or "").strip() and (
            bool(str(raw.get("model_path") or "").strip())
            or "model_path" in override_keys
        ))
        or (
            alias
            and ("model_alias" in raw or "model_alias" in override_keys)
        )
    )


def _llama_cpp_runtime_applies_to_selection(
    config: object | None,
    selected_model: str,
    *,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """Avoid inheriting a prior Muse runtime for an unrelated custom model."""

    model_id = str(selected_model or "").strip()
    # ``local-model`` is the operator-managed external OpenAI-compatible
    # endpoint.  Never let stale nested llama.cpp settings make it managed.
    if model_id.casefold() == "local-model":
        return False
    if overrides:
        return True
    if _llama_cpp_is_profile_selection(model_id):
        return True
    raw = _llama_cpp_raw_settings(config)
    previous_model = str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    if (
        _llama_cpp_is_profile_selection(
            previous_model,
            model_alias=str(raw.get("model_alias") or ""),
            model_path=str(raw.get("model_path") or ""),
        )
        and not _llama_cpp_is_profile_selection(model_id)
    ):
        # During a hot switch this is the old persisted Muse selection.  Do
        # not apply its GGUF/alias to an unrelated custom target.
        return False
    persisted_alias = str(raw.get("model_alias") or "").strip()
    if persisted_alias and model_id.casefold() == persisted_alias.casefold():
        return True
    persisted_path = str(raw.get("model_path") or "").strip()
    if persisted_path and not _llama_cpp_is_profile_selection(
        model_id,
        model_alias=persisted_alias,
        model_path=persisted_path,
    ):
        # A non-Muse GGUF path is an explicit managed runtime even when the
        # selected model ID currently differs from its alias; validation must
        # reject that mismatch rather than silently use a custom endpoint.
        return True
    if (
        persisted_alias
        and "model_alias" in raw
    ):
        return True
    # Environment aliases are explicit runtime configuration too.
    env_alias = str(os.getenv("LLAMA_CPP_MODEL_ALIAS") or "").strip()
    return bool(
        env_alias
        and model_id.casefold() == env_alias.casefold()
        and model_id.casefold() != "local-model"
    )


def _validate_llama_cpp_model_alias(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """Validate the selected ID against the served alias for managed GGUFs."""

    selected_model = _llama_cpp_selected_model(config, model)
    settings = _llama_cpp_settings(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=is_windows,
    )
    managed = _llama_cpp_is_managed_selection(
        config,
        selected_model,
        settings,
        overrides=overrides,
    )
    if not managed:
        return False
    alias = str(settings.get("model_alias") or "").strip()
    model_profile = _llama_cpp_profile_for_selection(
        selected_model,
        model_alias=alias,
        model_path=str(settings.get("model_path") or ""),
    )
    if model_profile and alias != str(model_profile.get("served_alias") or ""):
        label = str(model_profile.get("label") or model_profile.get("id") or "モデル")
        raise RuntimeError(
            f"{label}のmodel_aliasは{model_profile.get('served_alias')}固定です。"
            f"指定値={alias!r}"
        )
    if selected_model.casefold() != alias.casefold():
        raise RuntimeError(
            "llama.cppのselected modelとmodel_aliasが一致しません。"
            f"selected={selected_model!r}, alias={alias!r}"
        )
    return True


def _llama_cpp_selected_model(config: object | None, model: str | None = None) -> str:
    """Resolve the persisted local-model identity, not the active provider.

    ``openai_compatible_local.model`` is kept after switching away from the
    local provider so a later switch back can reuse it.  Callers that launch
    llama-server must still require an active ``openai_compatible_local``
    selection; this helper alone is not a start decision.
    """

    return str(
        model
        or _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()


def _should_start_llama_cpp(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """Whether AoiTalk should auto-start a managed llama-server.

    Persisted ``openai_compatible_local.*`` settings, a registered profile, or
    a local GGUF are not enough.  The active ``llm_provider`` must currently
    be ``openai_compatible_local``, and the selected model must be a managed
    llama.cpp selection rather than ``local-model`` or another bundled local
    profile.
    """

    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider != "openai_compatible_local":
        return False
    selected_model = _llama_cpp_selected_model(config, model)
    explicit_profile = llama_cpp_model_profile(selected_model)
    if selected_model.casefold() == "local-model":
        return False
    profile = local_server_profile_for_model(selected_model)
    if profile:
        return False
    settings = _llama_cpp_settings(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=is_windows,
    )
    if not bool(settings["auto_start"]):
        return False
    alias = str(settings["model_alias"] or "").strip()
    model_path = str(settings["model_path"] or "").strip()
    # Unknown model IDs must not auto-start merely because a nested GGUF path
    # still points at a different registered profile.
    if explicit_profile is None:
        if llama_cpp_model_profile(model_path=model_path) is not None:
            return False
        if selected_model.casefold() != alias.casefold():
            return False
        return bool(model_path)
    # Registered profiles are always eligible; custom models must explicitly
    # use the configured alias to avoid hijacking a generic local server.
    if not _llama_cpp_is_profile_selection(
        selected_model,
        model_alias=alias,
        model_path=model_path,
    ) and selected_model.casefold() != alias.casefold():
        return False
    # No GGUF path means deferred/manual configuration, not an attempted
    # startup.  This keeps the default local-model path backwards compatible.
    return bool(str(settings["model_path"] or "").strip())


def llama_cpp_managed_launch_configured(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """True when AoiTalk can launch an owned llama-server for this selection."""

    resolved = resolve_llama_cpp_runtime(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    return bool(resolved.get("managed")) and resolved.get("state") == "ready"


def llama_cpp_managed_launch_configuration_error(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> str | None:
    """Return an actionable error when managed auto-start cannot be configured."""

    resolved = resolve_llama_cpp_runtime(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    if not resolved.get("managed") or not resolved.get("auto_start"):
        return None
    return str(resolved.get("error") or "").strip() or None


def llama_cpp_manual_managed_runtime(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> bool:
    """True when a managed llama.cpp profile uses an external/manual endpoint."""

    selected_model = _llama_cpp_selected_model(config, model)
    if not _llama_cpp_runtime_applies_to_selection(
        config,
        selected_model,
        overrides=overrides,
    ):
        return False
    settings = _llama_cpp_settings(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=is_windows,
    )
    return _llama_cpp_is_managed_selection(
        config,
        selected_model,
        settings,
        overrides=overrides,
    ) and not bool(settings.get("auto_start"))


# Explicit server-named aliases keep the generic runtime discoverable for
# callers that mirror the existing ``_should_start_llama_cpp_server`` API.
_should_start_llama_cpp_server = _should_start_llama_cpp
_llama_cpp_runtime_settings = _llama_cpp_settings


def _openai_compatible_local_model_id(config: object | None) -> str:
    return str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()


def _openai_compatible_local_model(config: object | None) -> str:
    return _openai_compatible_local_model_id(config).lower()


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


def _local_openai_model_ids_exact(base_url: str) -> set[str]:
    """Return served model IDs preserving case for alias equality checks."""

    try:
        normalized_base_url = normalize_openai_compatible_base_url(base_url)
        with urlopen(f"{normalized_base_url}/models", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return set()

    model_ids: set[str] = set()
    data = payload.get("data", []) if isinstance(payload, dict) else []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            model_ids.add(str(item["id"]).strip())
    return model_ids


# Descriptive alias used by the llama.cpp orchestration facade.
_llama_cpp_model_ids_exact = _local_openai_model_ids_exact


def _llama_cpp_base_url(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> str:
    settings = _llama_cpp_settings(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    host = str(settings["host"] or LLAMA_CPP_DEFAULT_HOST).strip()
    if host in {"0.0.0.0", "::", "[::]", "::0"}:
        host = "127.0.0.1"
    host_for_url = host if ":" not in host or host.startswith("[") else f"[{host}]"
    port = int(settings["port"])
    return normalize_openai_compatible_base_url(f"http://{host_for_url}:{port}/v1")


def _resolve_llama_cpp_executable(
    configured: str,
    *,
    is_windows: bool | None = None,
) -> str:
    value = str(configured or "").strip()
    if value:
        path = Path(value).expanduser()
        if path.is_file():
            return str(path)
        resolved = shutil.which(value)
        if resolved:
            return resolved
        raise RuntimeError(
            f"llama-server executable not found: {value}. "
            "Set openai_compatible_local.llama_cpp.executable or LLAMA_CPP_EXECUTABLE."
        )

    windows = bool(is_windows if is_windows is not None else _IS_WINDOWS)
    executable_name = "llama-server.exe" if windows else "llama-server"
    resolved = shutil.which(executable_name)
    if resolved:
        return resolved
    raise RuntimeError(
        f"{executable_name} not found on PATH. "
        "Set openai_compatible_local.llama_cpp.executable or LLAMA_CPP_EXECUTABLE."
    )


def _validate_llama_cpp_version(
    executable: str,
    *,
    muse: bool = False,
    minimum_build: int | None = None,
    model_label: str = "llama.cpp",
) -> str | None:
    """Reject a profile's known-old llama.cpp build while remaining portable.

    Release binaries do not all expose a uniform version string.  We parse
    build IDs when present and only warn when an installation cannot report a
    build; an explicitly old ``bNNNNN`` is always rejected with an actionable
    message.
    """

    if minimum_build is None and muse:
        minimum_build = _LLAMA_CPP_MIN_MUSE_BUILD
    if minimum_build is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    except Exception as exc:  # pragma: no cover - platform executable failure
        requirement = f"b{minimum_build}以上"
        raise RuntimeError(
            f"{model_label}向けllama.cppのバージョンを確認できませんでした。"
            f"{requirement}のllama-serverを用意してください。"
        ) from exc

    import re

    match = re.search(r"\bb(\d{4,6})\b", output, re.IGNORECASE)
    if not match:
        # Some packaged binaries print ``version: 9113`` without the usual
        # ``b`` prefix.  Treat that numeric build as authoritative as well.
        match = re.search(r"\bversion\s*[:=]?\s*(\d{4,6})\b", output, re.IGNORECASE)
    if not match:
        # Current official Windows release binaries identify the build in the
        # form ``version: 0.1.0-dev (build 10405, commit <sha>)``.  Keep the
        # marker explicit so a commit hash (or another unrelated number) is
        # never mistaken for a llama.cpp build ID.
        match = re.search(r"\bbuild\s*[:=]?\s*(\d{4,6})\b", output, re.IGNORECASE)
    if not match:
        requirement = f"b{minimum_build}以上"
        raise RuntimeError(
            f"{model_label}向けllama.cppのbuild番号を解析できないため起動を拒否しました。"
            f"{requirement}のllama-serverを使用してください。"
        )
    build = int(match.group(1))
    if build < minimum_build:
        effective_label = (
            "Muse Glimmer"
            if muse and model_label == "llama.cpp"
            else model_label
        )
        raise RuntimeError(
            f"{effective_label}にはllama.cpp b{minimum_build}以上が必要です。"
            f"検出されたbuildはb{build}です。llama-serverを更新してください。"
        )
    return output


def _llama_cpp_launch_plan(
    config: object | None,
    *,
    project_root: Path | None = None,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> tuple[list[str], Path]:
    settings = _llama_cpp_settings(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    _validate_llama_cpp_extra_args(settings["extra_args"])
    selected_model = _llama_cpp_selected_model(config, model)
    model_path = str(settings["model_path"] or "").strip()
    if not model_path:
        raise RuntimeError(
            "llama.cpp GGUF model_path is not configured. Set "
            "openai_compatible_local.llama_cpp.model_path or LLAMA_CPP_MODEL_PATH."
        )
    model_file = _require_existing_file(
        model_path,
        "llama.cpp GGUF",
        "openai_compatible_local.llama_cpp.model_path",
    )
    _validate_llama_cpp_model_path_profile(
        selected_model,
        llama_cpp_model_profile(selected_model),
        model_file,
    )
    _validate_llama_cpp_model_alias(
        config,
        model=selected_model,
        overrides=overrides,
        is_windows=is_windows,
    )
    executable = _resolve_llama_cpp_executable(
        str(settings["executable"] or ""),
        is_windows=is_windows,
    )
    model_profile = _llama_cpp_profile_for_selection(
        selected_model,
        model_alias=str(settings["model_alias"] or ""),
        model_path=model_file,
    )
    # Keep launch validation on the exact same profile metadata contract used
    # by the resolver/UI diagnostics.
    minimum_build = _llama_cpp_effective_minimum_build(model_profile)
    _validate_llama_cpp_version(
        executable,
        muse=False,
        minimum_build=int(minimum_build) if minimum_build is not None else None,
        model_label=str(model_profile.get("label") or selected_model)
        if model_profile
        else selected_model,
    )

    args = [
        executable,
        "--model",
        model_file,
        "--alias",
        str(settings["model_alias"]),
        "--host",
        str(settings["host"]),
        "--port",
        str(settings["port"]),
        "--ctx-size",
        str(settings["context_size"]),
        "--n-gpu-layers",
        str(settings["gpu_layers"]),
    ]
    args.extend(str(item) for item in settings["extra_args"])
    # MTP flags are generated only from the resolved profile capability and
    # artifact state.  Never add spec-draft-n-max here; llama.cpp owns its
    # current default (3) and AoiTalk does not persist a tuning knob for it.
    settings = _llama_cpp_settings_with_mtp_cli_capability(settings, executable)
    mtp_ready = bool(settings.get("mtp_enabled")) and bool(settings.get("mtp_available"))
    if mtp_ready:
        args.extend(["--spec-type", "draft-mtp"])
        artifact_path = str(settings.get("mtp_artifact_path") or "").strip()
        if artifact_path:
            args.extend(["--spec-draft-model", artifact_path])
    for required_arg in (
        list(model_profile.get("required_args") or []) if model_profile else []
    ):
        # Keep required flags (notably --jinja) at the end of the argv even
        # when a profile also lists them in default_args.  This preserves the
        # established launch contract while keeping generated MTP flags
        # adjacent to the user extras.
        required_text = str(required_arg)
        args = [item for item in args if item != required_text]
        args.append(required_text)
    return args, project_root or Path(__file__).resolve().parents[2]


def _llama_cpp_launch_args(
    config: object | None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> list[str]:
    """Build the argv array for tests and callers that do not need cwd."""

    return _llama_cpp_launch_plan(
        config,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )[0]


def _is_selected_local_model_running(model_ids: set[str], expected_ids: set[str]) -> bool:
    return bool(model_ids.intersection(expected_ids))


def _write_process_console_chunk(chunk: bytes) -> None:
    """Best-effort write one child-process output chunk to the parent console.

    The service manager may run without a console (for example when launched
    by a desktop host), and test doubles often expose a text-only ``stdout``.
    Console failures must never terminate the output reader: the persistent
    model log remains useful even when the parent stream is closed.
    """

    stdout = sys.stdout
    if stdout is None:
        return

    try:
        buffer = getattr(stdout, "buffer", None)
    except Exception:
        # A partially closed/invalid stream can even fail while looking up
        # ``buffer``.  Fall back to the text API where possible.
        buffer = None

    try:
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
            return

        stdout.write(chunk.decode("utf-8", errors="replace"))
        stdout.flush()
    except (BrokenPipeError, OSError, ValueError, AttributeError, TypeError):
        # Broken/closed parent consoles are expected during shutdown.  The
        # tee reader intentionally swallows this so it can continue draining
        # the child's pipe and writing the file log.
        return
    except Exception:
        # Custom console wrappers may raise a non-standard exception.  Treat
        # those exactly like the standard closed-pipe failures above.
        return


def _tee_process_output(stream: object, log_file: object) -> None:
    """Drain a binary child stream to both the persistent log and console.

    Only this daemon reader owns ``stream`` and ``log_file`` in mirror mode.
    A failure on either destination is isolated from the read loop so a full
    or closed destination cannot leave the child's PIPE blocked.
    """

    log_failed = False
    try:
        while True:
            try:
                chunk = stream.read(4096)  # type: ignore[attr-defined]
            except Exception:
                # There is no useful recovery for a failed child stream.  The
                # finally block still releases both handles best-effort.
                break
            if not chunk:
                break

            if not log_failed:
                try:
                    log_file.write(chunk)  # type: ignore[attr-defined]
                    log_file.flush()  # type: ignore[attr-defined]
                except Exception:
                    # Continue draining to EOF even if disk/log I/O fails.
                    log_failed = True

            try:
                _write_process_console_chunk(chunk)
            except Exception:
                # The helper itself is defensive, but a monkeypatch or
                # unusual wrapper must not stop the file/PIPE drain either.
                pass
    finally:
        try:
            stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            log_file.close()  # type: ignore[attr-defined]
        except Exception:
            pass


def _rollback_process_after_tee_start_failure(proc: subprocess.Popen) -> None:
    """Stop a child when its mirror reader could not be started.

    This path is intentionally best-effort.  It is only used after Popen has
    succeeded but before the process is tracked, so leaving the child alive
    would risk a permanently blocked stdout PIPE.
    """

    try:
        terminate = getattr(proc, "terminate", None)
        if callable(terminate):
            terminate()
    except Exception:
        pass

    try:
        wait = getattr(proc, "wait", None)
        if callable(wait):
            wait(timeout=1)
    except Exception:
        # If termination did not complete promptly, force-kill below.
        pass

    still_running = True
    try:
        poll = getattr(proc, "poll", None)
        if callable(poll):
            still_running = poll() is None
    except Exception:
        still_running = True

    if still_running:
        try:
            kill = getattr(proc, "kill", None)
            if callable(kill):
                kill()
        except Exception:
            pass
        try:
            wait = getattr(proc, "wait", None)
            if callable(wait):
                wait(timeout=1)
        except Exception:
            pass

    try:
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            stream.close()
    except Exception:
        pass


def _start_logged_openai_compatible_process(
    args: list[str],
    *,
    cwd: Path,
    log_path: Path,
    mirror_to_parent_console: bool = False,
) -> subprocess.Popen:
    from src.utils.log_housekeeping import rotate_log_if_over_size

    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log_if_over_size(log_path)
    log_file = log_path.open("ab")
    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE if mirror_to_parent_console else log_file,
        "stderr": subprocess.STDOUT,
    }
    if mirror_to_parent_console:
        # Keep the parent-side pipe unbuffered so partial llama.cpp output is
        # forwarded without waiting for a BufferedReader fill threshold.
        popen_kwargs["bufsize"] = 0
    if _IS_WINDOWS and not mirror_to_parent_console:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    elif not _IS_WINDOWS:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except BaseException:
        try:
            log_file.close()
        except Exception:
            pass
        raise

    if mirror_to_parent_console:
        stream = getattr(proc, "stdout", None)
        if stream is None:
            try:
                log_file.close()
            except Exception:
                pass
            _rollback_process_after_tee_start_failure(proc)
            raise RuntimeError("llama.cpp stdout PIPEを取得できませんでした")
        try:
            reader = threading.Thread(
                target=_tee_process_output,
                args=(stream, log_file),
                daemon=True,
                name="llama-cpp-output-tee",
            )
            reader.start()
        except BaseException:
            # The tee thread owns the handles only after a successful start;
            # roll back Popen without leaving a child blocked on its PIPE.
            try:
                log_file.close()
            except Exception:
                pass
            _rollback_process_after_tee_start_failure(proc)
            raise
    else:
        # Preserve the existing ownership contract for exo/MLX and callers
        # that do not opt into mirroring.
        log_file.close()
    _track_child_process(proc, openai_compatible_local=True)
    return proc


def _start_llama_cpp_server(
    project_root: Path,
    config: object | None = None,
    *,
    model: str | None = None,
    overrides: dict[str, object] | None = None,
    is_windows: bool | None = None,
) -> subprocess.Popen:
    args, cwd = _llama_cpp_launch_plan(
        config,
        project_root=project_root,
        model=model,
        overrides=overrides,
        is_windows=is_windows,
    )
    proc = _start_logged_openai_compatible_process(
        args,
        cwd=cwd,
        log_path=_models_log_dir(project_root) / "llama_cpp.log",
        mirror_to_parent_console=True,
    )
    # Keep endpoint ownership attached to the tracked process itself.  The
    # persisted config can change before a hot-switch validation runs, so
    # consulting the current config alone could incorrectly claim a new port
    # listener as our old process.
    try:
        setattr(
            proc,
            "_aoi_llama_cpp_base_url",
            _llama_cpp_base_url(
                config,
                model=model,
                overrides=overrides,
                is_windows=is_windows,
            ),
        )
    except Exception:
        logger.debug("llama.cpp process endpoint metadataを設定できません", exc_info=True)
    print(f"llama-serverを起動しました (PID {proc.pid})")
    return proc


def _start_exo_server(
    project_root: Path,
    config: object | None = None,
) -> None:
    args, cwd = _exo_launch_plan(config, project_root)
    proc = _start_logged_openai_compatible_process(
        args,
        cwd=cwd,
        log_path=_models_log_dir(project_root) / "exo.log",
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
        log_path=_models_log_dir(project_root) / "mlx_lm.log",
    )
    print(f"Started MLX LM OpenAI-compatible server (PID {proc.pid})")
