"""Pinned Strata setup and launch contracts; all inference uses the existing client.

Only the explicit prepare action may install anything. Normal startup executes the
same serve/server.py command emitted by upstream write_run_script(), never setup.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from src.features import Features
from src.llm.strata_manifest import STRATA_MANIFEST
from src.llm.openai_compatible_local_profiles import (
    STRATA_SOURCE_REPOSITORY,
    STRATA_SOURCE_REVISION,
    _config_get,
    normalize_openai_compatible_base_url,
    strata_model_profile,
)

SOURCE_URL = STRATA_MANIFEST.archive_url
ENGINE_URL = STRATA_MANIFEST.engine_url
MAX_SOURCE_BYTES = 128 * 1024 * 1024
# The setup source is small enough to pin as an archive and a canonical
# extracted-tree digest.  The latter lets an already extracted managed cache
# be verified without trusting the revision-only marker.
SOURCE_ARCHIVE_SHA256 = STRATA_MANIFEST.archive_sha256
SOURCE_TREE_SHA256 = STRATA_MANIFEST.tree_sha256
# The normalized form is accepted for a Windows checkout that converts shell
# script line endings while preserving the same pinned source contents.
SOURCE_TREE_SHA256_NORMALIZED = STRATA_MANIFEST.tree_sha256_normalized
SOURCE_TREE_FILE_COUNT = STRATA_MANIFEST.file_count
SOURCE_TREE_BYTES = STRATA_MANIFEST.tree_bytes
SOURCE_TREE_BYTES_NORMALIZED = STRATA_MANIFEST.tree_bytes_normalized
_SOURCE_MARKER_NAME = ".aoitalk-strata-source.json"
_GGUF_SHARD_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<index>\d{5})-of-(?P<count>\d{5})\.gguf$",
    re.IGNORECASE,
)
_STRATA_PERFORMANCE_KEYS = ("prefill", "vram_reserve_mib", "expert_cache")
_STRATA_PERFORMANCE_ARG_NAMES = {
    "prefill": "--prefill",
    "vram_reserve_mib": "--vram-reserve-mib",
    "expert_cache": "--expert-cache",
}
# The prompt/conversation cache flags decide whether a returning conversation
# resumes from its KV state, so a regenerated config must not silently drop them.
_STRATA_PRESERVED_VALUE_ARGS = (
    "--batch",
    "--batch-groups",
    "--ple-io",
    "--prompt-cache",
    "--prompt-cache-every",
    "--prompt-cache-root",
    "--conversation-cache-mib",
    "--conversation-cache-slots",
    "--conversation-cache-min-free-mib",
)
_STRATA_PRESERVED_SWITCH_ARGS = ("--batch-mtp",)
_DEFAULT_MTP_GB = 6.0
_DEFAULT_SETUP_RUNTIME_GB = 10.0
_DEFAULT_Q2_EXTRA_GB = 40.0
_DEFAULT_VISION_GB = 1.0
SETUP_NOTICE = (
    "公式setupはPython・依存・GPU runtimeとモデルを取得します。モデル約66〜84GB、"
    "MTP約6GB、画像有効時約1GB、Q2_0/AVX-512は追加約40GB、Linux HIPは追加約10GB。"
    "一時領域も必要です。統合GPU（Strix Haloを含む）は非対応です。"
)

_STARTUP_LOG_TAIL_BYTES = 64 * 1024
_STARTUP_PHASES = {
    "loading",
    "reading_weights",
    "allocating_cache",
    "initializing",
}
_STARTUP_SECONDS_RE = re.compile(r"\((?P<seconds>\d+) s(?: so far)?\)")


def _strata_startup_projection(
    owned_process: Any | None,
    *,
    log_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return a bounded, allow-listed projection for an owned startup.

    The process boundary and launch-time log offset prevent a previous run's
    tail from being reported as current progress. External Strata processes
    never reach this helper because they have no owned process object.
    """
    if owned_process is None:
        return None
    if str(getattr(owned_process, "_aoi_local_readiness_state", "")) != "loading":
        return None
    try:
        if owned_process.poll() is not None:
            return None
    except Exception:
        return None
    path_value = log_path or getattr(owned_process, "_aoi_local_startup_log_path", None)
    if not path_value:
        return None
    try:
        path = Path(str(path_value))
        offset = int(getattr(owned_process, "_aoi_local_startup_log_offset", 0))
        if offset < 0 or not path.is_file():
            return None
        size = path.stat().st_size
        if size < offset:
            # The bounded log rotated after this process recorded its
            # boundary; the new file is entirely current for this process.
            offset = 0
        start = max(offset, size - _STARTUP_LOG_TAIL_BYTES)
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(_STARTUP_LOG_TAIL_BYTES)
    except (OSError, ValueError, TypeError):
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > offset and lines and not text.startswith(("\n", "\r")):
        # The bounded window may begin in the middle of a line; discard that
        # fragment so a stale marker cannot become the current phase.
        lines = lines[1:]
    start_markers = [
        index
        for index, line in enumerate(lines)
        if "starting the engine: reading the model's weights" in line
        or line.startswith("loading the model ")
    ]
    if not start_markers:
        if any(
            marker in line
            for line in lines
            for marker in (
                "ready: http://",
                "model unloaded;",
                "the engine is running again",
                "the engine stopped",
            )
        ):
            return None
        started_at = getattr(owned_process, "_aoi_local_startup_started_at", None)
        if isinstance(started_at, (int, float)):
            return {
                "phase": "reading_weights",
                "elapsed_s": max(0, int(time.monotonic() - started_at)),
            }
        return None
    current = lines[max(start_markers):]
    phase = "reading_weights"
    elapsed_s: int | None = None
    for line in current:
        if any(
            marker in line
            for marker in (
                "ready: http://",
                "model unloaded;",
                "the engine is running again",
                "the engine stopped",
            )
        ):
            return None
        seconds_match = _STARTUP_SECONDS_RE.search(line)
        if seconds_match:
            elapsed_s = int(seconds_match.group("seconds"))
        if "experts loaded:" in line:
            phase = "loading"
        elif "filling the GPU's expert cache" in line:
            phase = "allocating_cache"
        elif "almost ready" in line:
            phase = "initializing"
        elif "the engine is running again" in line:
            phase = "initializing"
        elif "starting the engine: reading the model's weights" in line:
            phase = "reading_weights"
    if elapsed_s is None:
        started_at = getattr(owned_process, "_aoi_local_startup_started_at", None)
        if isinstance(started_at, (int, float)):
            elapsed_s = max(0, int(time.monotonic() - started_at))
    if phase not in _STARTUP_PHASES or elapsed_s is None:
        return None
    return {
        "phase": phase,
        "elapsed_s": elapsed_s,
    }


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def strata_vision_enabled(
    settings: dict[str, Any], prepared_config: dict[str, Any] | None = None
) -> bool:
    """Return the effective vision contract from settings and prepared config."""
    return str(settings.get("vision") or "none") != "none" or bool(
        isinstance(prepared_config, dict) and prepared_config.get("vision")
    )


def _absolute_path(value: Any, *, label: str) -> Path:
    """Return one absolute path before passing it to the shared root guard."""

    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Strata {label}は絶対パスで指定してください")
    return path


def _canonical_source_root(config: Any, value: Any) -> Path:
    """Canonicalize source code through the managed runtime-root boundary."""

    from src.services.local_llm_paths import resolve_managed_runtime_root

    candidate = _absolute_path(value, label="source_root")
    return resolve_managed_runtime_root(
        config,
        explicit=candidate,
        create=False,
    ).path


def _default_source_root(config: Any) -> Path:
    """Return the revision-specific child of the managed runtime cache."""

    from src.services.local_llm_paths import (
        resolve_managed_runtime_root,
        validate_managed_child,
    )

    runtime_root = resolve_managed_runtime_root(config).path
    return validate_managed_child(
        runtime_root,
        runtime_root / "strata" / STRATA_SOURCE_REVISION,
        kind="runtime",
    )


def _canonical_model_root(config: Any, value: Any, *, label: str) -> Path:
    """Canonicalize model/GGUF storage through the shared model-root guard."""

    from src.services.local_llm_paths import resolve_llama_cpp_model_root

    candidate = _absolute_path(value, label=label)
    return resolve_llama_cpp_model_root(
        config,
        explicit=candidate,
        create=False,
    ).path


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(os.lstat(str(path)), "st_file_attributes", 0))
    except (OSError, ValueError):
        return True
    return path.is_symlink() or os.path.islink(str(path)) or bool(attributes & 0x400)


def _canonical_tree_digest(
    root: Path,
    *,
    normalize_windows_text: bool = False,
    source_files: list[str] | None = None,
) -> tuple[str, int, int]:
    """Hash source files deterministically, excluding the mutable marker.

    GitHub's archive and an LF checkout follow upstream .gitattributes
    (bat=CRLF, sh=LF). The script-only normalized variant also matches the
    Git blob tree. Other text converted by core.autocrlf is diagnosed rather
    than silently rewritten; content changes still alter the digest.
    """

    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    try:
        if _is_reparse_point(root):
            raise ValueError("Strata source tree cannot contain symlinks or junctions")
        if source_files is None:
            entries = list(root.rglob("*"))
        else:
            # The marker's path list is untrusted. Its aggregate digest and
            # count must match the compiled manifest; runtime-generated files
            # (venv, engine, model configs) are outside that source inventory.
            if not isinstance(source_files, list) or not all(
                isinstance(value, str) for value in source_files
            ):
                raise ValueError("Strata source treeのfile listが不正です")
            if len(source_files) != len(set(source_files)):
                raise ValueError("Strata source treeのfile listが重複しています")
            entries = []
            for value in source_files:
                relative = PurePosixPath(value)
                if (
                    not value
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or relative.as_posix() != value
                    or any(char in value for char in "\\:\x00")
                    or _SOURCE_MARKER_NAME in relative.parts
                    or ".git" in relative.parts
                ):
                    raise ValueError("Strata source treeのfile listが不正です")
                path = root / value
                if not path.is_file():
                    raise ValueError("Strata source treeのfileが不足しています")
                entries.append(path)
            inventory = set(source_files)
            # An added module can affect Python imports or a CMake glob even
            # when every original file still hashes correctly. Ignore only
            # upstream's generated dependency/build/runtime directories.
            for directory, dirs, names in os.walk(root, followlinks=False):
                relative_dir = Path(directory).relative_to(root).as_posix()
                dirs[:] = [name for name in dirs if name != "__pycache__" and not (
                    relative_dir == "." and (
                        name in {".git", ".venv", "engine", "engine-cuda12", "build"}
                        or name.startswith("build-")
                    )
                ) and not (relative_dir == "third_party" and name == "llama.cpp")]
                for name in names:
                    path = Path(directory) / name
                    relative = path.relative_to(root).as_posix()
                    generated_script = relative_dir == "." and (
                        name.startswith("run-") or name.startswith("build-")
                    ) and path.suffix in {".bat", ".sh"}
                    if relative not in inventory and not generated_script and path.suffix.casefold() in {
                        ".py", ".pyc", ".pyd", ".so", ".dll", ".cpp", ".c", ".cu",
                        ".h", ".hpp", ".cmake", ".bat", ".cmd", ".sh", ".ps1",
                    }:
                        raise ValueError("Strata source treeに未検証の追加codeがあります")
        for entry in entries:
            if entry.name != _SOURCE_MARKER_NAME and _is_reparse_point(entry):
                raise ValueError("Strata source tree cannot contain symlinks or junctions")
        files = sorted(
            (path for path in entries if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in files:
            if path.name == _SOURCE_MARKER_NAME:
                continue
            relative_path = path.relative_to(root)
            if _is_reparse_point(path) or any(
                _is_reparse_point(root / parent)
                for parent in relative_path.parents
                if str(parent) != "."
            ):
                raise ValueError("Strata source tree cannot contain symlinks or junctions")
            relative = relative_path.as_posix()
            data = path.read_bytes()
            if normalize_windows_text and path.suffix.casefold() in {
                ".bat",
                ".cmd",
                ".ps1",
                ".sh",
            }:
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            file_count += 1
            total_bytes += len(data)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and "source tree" in str(exc):
            raise
        raise ValueError("Strata source treeを検証できません") from exc
    return digest.hexdigest(), file_count, total_bytes


def _source_tree_matches_pin(
    root: Path, expected: str, *, source_files: list[str] | None = None
) -> bool:
    digest, count, total_bytes = _canonical_tree_digest(root, source_files=source_files)
    if (
        expected in {SOURCE_TREE_SHA256, SOURCE_TREE_SHA256_NORMALIZED}
        and digest == SOURCE_TREE_SHA256
        and count == SOURCE_TREE_FILE_COUNT
        and total_bytes == SOURCE_TREE_BYTES
    ):
        return True
    normalized, normalized_count, normalized_bytes = _canonical_tree_digest(
        root,
        normalize_windows_text=True,
        source_files=source_files,
    )
    return (
        expected in {SOURCE_TREE_SHA256, SOURCE_TREE_SHA256_NORMALIZED}
        and normalized == SOURCE_TREE_SHA256_NORMALIZED
        and normalized_count == SOURCE_TREE_FILE_COUNT
        and normalized_bytes == SOURCE_TREE_BYTES_NORMALIZED
    )


def strata_external(config: Any) -> bool:
    # Enterprise never installs/owns Strata, even without an explicit backend.
    if Features.is_enterprise():
        return True
    from src.llm.deployment_resolver import resolve_llm_deployment

    deployment = resolve_llm_deployment(config)
    return bool(deployment and deployment.backend == "external")


def normalize_strata_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("strata はオブジェクトで指定してください")
    allowed = {
        "source_root",
        "data_dir",
        "gguf_dir",
        "host",
        "port",
        "context_size",
        "vision",
        "auto_start",
        "readiness_timeout",
        "prefill",
        "vram_reserve_mib",
        "expert_cache",
    }
    if set(raw) - allowed:
        raise ValueError("Strataの未対応設定です")
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"source_root", "data_dir", "gguf_dir", "host", "vision"}:
            if not isinstance(value, str) or any(c in value for c in "\x00\r\n"):
                raise ValueError(f"strata.{key} は文字列で指定してください")
            value = value.strip()
        elif key in {"port", "context_size"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"strata.{key} は整数で指定してください")
            bounds = (1, 65535) if key == "port" else (8192, 262144)
            if not bounds[0] <= value <= bounds[1]:
                raise ValueError(f"strata.{key} の値が範囲外です")
        elif key == "prefill":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError("strata.prefill は整数またはautoで指定してください")
            value = str(value).strip().lower()
            if not re.fullmatch(r"(?:auto(?::(?:8192|16384|32768))?|[1-9][0-9]*)", value):
                raise ValueError("strata.prefill の値が不正です")
            if value.isdigit() and not 1 <= int(value) <= 32768:
                raise ValueError("strata.prefill の値が範囲外です")
        elif key == "vram_reserve_mib":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("strata.vram_reserve_mib は整数で指定してください")
            if not 0 <= value <= 131072:
                raise ValueError("strata.vram_reserve_mib の値が範囲外です")
        elif key == "expert_cache":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError("strata.expert_cache は整数またはautoで指定してください")
            value = str(value).strip().lower()
            if value != "auto" and (not value.isdigit() or int(value) < 0):
                raise ValueError("strata.expert_cache の値が不正です")
        elif key == "readiness_timeout":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 1 <= value <= 3600
            ):
                raise ValueError(
                    "strata.readiness_timeout は1〜3600秒で指定してください"
                )
        elif key == "auto_start" and not isinstance(value, bool):
            raise ValueError("strata.auto_start は真偽値で指定してください")
        result[key] = value
    if result.get("vision", "none") not in {"none", "cpu", "gpu"}:
        raise ValueError("strata.vision はnone/cpu/gpuで指定してください")
    if result.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "managed Strataはloopbackのみです。LAN接続は外部endpointを使用してください"
        )
    return result


def strata_settings(
    config: Any, *, model: str, platform_name: str | None = None
) -> dict[str, Any]:
    profile = strata_model_profile(model)
    if profile is None:
        raise ValueError("未対応のStrata profileです")
    raw = _config_get(config, "openai_compatible_local.strata", {})
    raw = raw if isinstance(raw, dict) else {}
    platform_name = platform_name or sys.platform
    source = str(
        os.getenv("STRATA_SOURCE_ROOT") or raw.get("source_root") or ""
    ).strip()
    if not source:
        source_root = _default_source_root(config)
    else:
        source_root = _canonical_source_root(config, source)
    data_dir = str(os.getenv("STRATA_DATA_DIR") or raw.get("data_dir") or "").strip()
    if not data_dir:
        from src.services.local_llm_paths import resolve_llama_cpp_model_root

        model_root = resolve_llama_cpp_model_root(config).path
        data_root = model_root
        data_root = data_root / "strata"
        # The model resolver has already checked the root.  Validate the
        # Strata child as a model artifact path so a reparse point cannot be
        # introduced between resolution and setup.
        from src.services.local_llm_paths import validate_managed_child

        data_root = validate_managed_child(
            model_root,
            data_root,
            kind="model",
        )
    else:
        # Keep settings serialization compatible with the existing setup API;
        # the destructive setup path canonicalizes this root again immediately
        # before creating it.
        data_root = _canonical_model_root(config, data_dir, label="data_dir")
    gguf_value = str(raw.get("gguf_dir") or "").strip()
    gguf_root = (
        _canonical_model_root(config, gguf_value, label="gguf_dir")
        if gguf_value
        else None
    )
    raw_values: dict[str, Any] = {
        "source_root": str(source_root),
        "data_dir": str(data_root),
        "gguf_dir": str(gguf_root or ""),
        "host": str(raw.get("host") or "127.0.0.1"),
        "port": raw.get("port", 8080),
        "context_size": raw.get("context_size", 32768),
        "vision": str(raw.get("vision") or "none"),
        "auto_start": _bool(
            os.getenv("STRATA_AUTO_START", raw.get("auto_start")), True
        ),
        "readiness_timeout": raw.get("readiness_timeout", 600),
    }
    for key in _STRATA_PERFORMANCE_KEYS:
        if raw.get(key) is not None:
            raw_values[key] = raw[key]
    values = normalize_strata_settings(raw_values)
    values["auto_start"] = bool(values["auto_start"] and not strata_external(config))
    values["source_root"] = str(source_root)
    values.update(
        {
            "runtime": "strata",
            "server_profile": "strata",
            "model_alias": profile["served_alias"],
            "runtime_profile": profile,
            "config_path": str(source_root / profile["config_filename"]),
            "python": str(
                source_root
                / (
                    ".venv/Scripts/python.exe"
                    if platform_name.startswith("win")
                    else ".venv/bin/python"
                )
            ),
            "platform_supported": platform_name.startswith(("win", "linux")),
        }
    )
    generated_performance = _read_strata_performance_from_config(
        Path(values["config_path"])
    )
    for key, value in generated_performance.items():
        values.setdefault(key, value)
    return values


def strata_base_url(config: Any, *, model: str) -> str:
    from src.llm.deployment_resolver import resolve_llm_deployment

    deployment = resolve_llm_deployment(config)
    if (
        deployment
        and deployment.backend == "external"
        and deployment.effective_base_url
    ):
        return normalize_openai_compatible_base_url(deployment.effective_base_url)
    raw = _config_get(config, "openai_compatible_local.strata", {})
    raw = raw if isinstance(raw, dict) else {}
    configured = str(
        os.getenv("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
        or _config_get(config, "openai_compatible_local.base_url", "")
        or ""
    ).strip()
    managed = _bool(
        os.getenv("STRATA_AUTO_START", raw.get("auto_start")), True
    ) and not strata_external(config)
    if configured and not managed:
        return normalize_openai_compatible_base_url(configured)
    host = str(raw.get("host") or "127.0.0.1")
    port = raw.get("port", 8080)
    normalize_strata_settings({"host": host, "port": port})
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/v1"


def _read_strata_performance_from_config(config_path: Path) -> dict[str, Any]:
    """Read performance flags from the generated config without mutating it."""

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    args = payload.get("args") if isinstance(payload, dict) else None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return {}
    values: dict[str, Any] = {}
    for key, option in _STRATA_PERFORMANCE_ARG_NAMES.items():
        try:
            index = args.index(option)
        except ValueError:
            continue
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            continue
        raw = args[index + 1]
        if key == "vram_reserve_mib":
            if raw.isdigit():
                values[key] = int(raw)
        else:
            values[key] = raw
    try:
        return {
            key: normalize_strata_settings({key: value})[key]
            for key, value in values.items()
        }
    except ValueError:
        return {}


def _write_strata_config_atomic(config_path: Path, payload: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, config_path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _restore_strata_performance_args(settings: dict[str, Any]) -> dict[str, Any]:
    """Restore known successful performance flags after official setup writes JSON."""

    values = {
        key: settings[key]
        for key in _STRATA_PERFORMANCE_KEYS
        if settings.get(key) is not None
    }
    config_path = Path(settings["config_path"])
    if not values or not config_path.is_file():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    args = payload.get("args") if isinstance(payload, dict) else None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return {}
    changed = False
    for key, value in values.items():
        option = _STRATA_PERFORMANCE_ARG_NAMES[key]
        serialized = str(value)
        try:
            index = args.index(option)
        except ValueError:
            args.extend([option, serialized])
            changed = True
            continue
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            args.insert(index + 1, serialized)
            changed = True
        elif args[index + 1] != serialized:
            args[index + 1] = serialized
            changed = True
    if changed:
        _write_strata_config_atomic(config_path, payload)
    return payload


def _read_strata_user_config(config_path: Path) -> dict[str, Any]:
    """Capture the existing model's settings before upstream regenerates JSON."""
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("既存Strata configを読めません。更新前に修復してください") from exc
    if not isinstance(payload, dict):
        raise ValueError("既存Strata configが不正です")
    return payload


def _restore_strata_user_config(
    config_path: Path, previous: dict[str, Any]
) -> None:
    """Keep CPU share/watchdog env and explicit batch choices through setup.

    Upstream already preserves custom server fields but regenerates engine
    argv. Preserve explicit concurrency switches and the PLE storage mode here;
    artifact paths and GPU placement remain upstream's responsibility.
    """
    if not previous:
        return
    current = _read_strata_user_config(config_path)
    args, old_args = current.get("args"), previous.get("args")
    if not isinstance(args, list) or not isinstance(old_args, list):
        raise ValueError("Strata configのengine引数が不正です")
    old_env = previous.get("env")
    if isinstance(old_env, dict):
        current["env"] = {**(current.get("env") or {}), **old_env}
    for flag in _STRATA_PRESERVED_VALUE_ARGS:
        if flag not in old_args:
            continue
        index = old_args.index(flag)
        if index + 1 >= len(old_args) or str(old_args[index + 1]).startswith("--"):
            raise ValueError(f"既存Strata configの{flag}が不正です")
        while flag in args:
            index = args.index(flag)
            del args[index]
            if index < len(args) and not str(args[index]).startswith("--"):
                del args[index]
        args.extend([flag, old_args[old_args.index(flag) + 1]])
    for flag in _STRATA_PRESERVED_SWITCH_ARGS:
        if flag in old_args and flag not in args:
            args.append(flag)
    _write_strata_config_atomic(config_path, current)


def _strata_engine_metadata(executable: Path) -> dict[str, Any]:
    try:
        metadata = json.loads(
            (executable.parent / "BUILD.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Strata engineのBUILD.jsonが不足または不正です") from exc
    if not isinstance(metadata, dict) or metadata.get("version") != STRATA_MANIFEST.engine_version:
        raise ValueError(
            f"Strata engineは{STRATA_MANIFEST.engine_version}が必要です。固定版のsetupを実行してください"
        )
    return metadata


def verify_strata_engine(prepared: dict[str, Any]) -> str:
    """Probe the actual executable without loading any model or installing it."""
    root = Path(prepared["cwd"])
    executable = Path(prepared["exe"])
    if not executable.is_absolute():
        executable = root / executable
    _strata_engine_metadata(executable)
    env = dict(os.environ)
    # Match upstream's loader paths so Windows DLLs and Linux HIP libraries
    # are available to --version, which exits before GPU/model initialization.
    lib_dirs = prepared.get("lib_dirs") or []
    if isinstance(lib_dirs, list) and all(isinstance(value, str) for value in lib_dirs):
        for key in ("PATH", "LD_LIBRARY_PATH"):
            env[key] = os.pathsep.join([*lib_dirs, env.get(key, "")])
    try:
        result = subprocess.run(
            [str(executable), "--version"], cwd=root, env=env,
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Strata engineの実versionを確認できません") from exc
    if result.returncode != 0 or result.stdout.strip() != f"strata {STRATA_MANIFEST.engine_version}":
        raise ValueError("Strata engineの実versionと固定manifestが不一致です")
    return STRATA_MANIFEST.engine_version


def _strata_config_path_values(
    config: dict[str, Any],
    *,
    use_gguf_root: bool,
) -> list[tuple[str, Any, Any, str]]:
    """Return only the official artifact path slots from a Strata config."""

    slots: list[tuple[str, Any, Any, str]] = []
    args = config.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        options = {
            "--pack": "data",
            "--mtp": "data",
            "--native": "gguf" if use_gguf_root else "data",
            "--ple-gguf": "gguf" if use_gguf_root else "data",
        }
        for index, value in enumerate(args[:-1]):
            role = options.get(value)
            if role is None or args[index + 1].startswith("--"):
                continue
            slots.append((role, args, index + 1, args[index + 1]))
    tokenizer = config.get("tokenizer")
    if isinstance(tokenizer, str):
        slots.append(("data", config, "tokenizer", tokenizer))
    vision = config.get("vision")
    if isinstance(vision, dict):
        for key in ("model", "mmproj"):
            value = vision.get(key)
            if isinstance(value, str):
                slots.append(("gguf" if use_gguf_root else "data", vision, key, value))
    return slots


def _strata_path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def relocate_strata_artifact_paths(
    settings: dict[str, Any], *, apply: bool = True
) -> dict[str, Any]:
    """Rewire copied Strata artifacts to the effective storage roots.

    Strata's setup JSON contains absolute artifact paths.  Changing the
    AoiTalk ``data_dir``/``gguf_dir`` therefore does not move an already
    generated JSON away from the previous volume.  When the destination has
    the same relative tree and every replacement exists, update the JSON
    atomically.  Missing destinations fail closed so a partial copy can never
    silently leave the old-volume runtime active.

    Runtime-owned files (the executable, source cwd and expert profile) are
    excluded by the managed source-root boundary.  Only values that point to
    an existing external tree are candidates for relocation.
    """

    config_path = Path(settings["config_path"])
    source_root = Path(settings["source_root"])
    data_root = Path(settings["data_dir"])
    gguf_value = str(settings.get("gguf_dir") or "").strip()
    gguf_root = Path(gguf_value) if gguf_value else None
    if not config_path.is_file():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    try:
        data_root = data_root.resolve(strict=False)
        if gguf_root is not None:
            gguf_root = gguf_root.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Strataのartifact保存先を検証できません")
    if not data_root.is_absolute() or (gguf_root is not None and not gguf_root.is_absolute()):
        raise ValueError("Strataのartifact保存先は絶対パスで指定してください")

    slots = _strata_config_path_values(payload, use_gguf_root=gguf_root is not None)
    changed = False
    unresolved = False
    for role in ("data", "gguf"):
        role_slots = [slot for slot in slots if slot[0] == role]
        if not role_slots:
            continue
        target_root = gguf_root if role == "gguf" else data_root
        if target_root is None:
            target_root = data_root
        external = []
        for _role, _container, _key, value in role_slots:
            path = Path(value).expanduser()
            if not path.is_absolute() or _strata_path_is_within(path, source_root):
                continue
            try:
                if path.exists():
                    external.append(path.resolve(strict=False))
            except (OSError, RuntimeError):
                continue
        if not external:
            continue
        try:
            old_root = Path(os.path.commonpath([str(path) for path in external]))
            if len(external) == 1 and external[0].is_file():
                old_root = external[0].parent
            old_root = old_root.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "Strata artifact paths span incompatible roots; refusing old-volume fallback"
            ) from exc
        if old_root == target_root:
            continue

        for _role, container, key, value in role_slots:
            path = Path(value).expanduser()
            if not path.is_absolute() or _strata_path_is_within(path, source_root):
                continue
            try:
                relative = path.resolve(strict=False).relative_to(old_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    "Strata artifact path does not match its configured storage root"
                ) from exc
            candidate = target_root / relative
            try:
                present = candidate.exists() and candidate.is_dir() == path.is_dir()
            except OSError:
                present = False
            if not present:
                unresolved = True
                continue
            replacement_text = str(candidate)
            if replacement_text != value:
                container[key] = replacement_text
                changed = True

    if unresolved:
        raise ValueError(
            "Strata artifact destination is incomplete; copy and verify the "
            "configured data_dir/gguf_dir before starting"
        )
    if not changed:
        return payload

    if not apply:
        raise ValueError(
            "Strata config artifact paths do not match the configured data_dir/gguf_dir"
        )

    _write_strata_config_atomic(config_path, payload)
    return payload


def strata_prepared_config(settings: dict[str, Any]) -> dict[str, Any]:
    root = Path(settings["source_root"])
    if not settings["platform_supported"]:
        raise ValueError("Strata managed runtimeはWindows/Linux x86-64向けです")
    for path in (
        Path(settings["python"]),
        root / "serve/server.py",
        Path(settings["config_path"]),
    ):
        if not path.is_file():
            raise ValueError("Strataの初回セットアップが必要です")
    cfg = json.loads(Path(settings["config_path"]).read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict) or cfg.get("model_name") != settings["model_alias"]:
        raise ValueError("Strata configのmodel_nameが選択モデルと一致しません")
    args = cfg.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Strata configのengine引数が不正です")

    def resolved_path(value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else Path(cfg.get("cwd") or root) / path).expanduser()

    def present(value: str, *, kind: str = "any") -> bool:
        if not value:
            return False
        path = resolved_path(value)
        if kind == "file":
            return path.is_file()
        if kind == "dir":
            return path.is_dir()
        return path.exists()

    def regular_nonempty(value: str) -> bool:
        if not value:
            return False
        path = resolved_path(value)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def option_value(name: str) -> str | None:
        """Return one argv option's value without accepting a flag as a value."""
        try:
            index = args.index(name)
        except ValueError:
            return None
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            return None
        return args[index + 1]

    required = [cfg.get("exe"), *[arg for arg in args if arg.lower().endswith(".gguf")]]
    if not regular_nonempty(str(cfg.get("exe") or "")):
        raise ValueError(
            "Strata engine/GGUFが不足しています。セットアップを再実行してください"
        )
    if Path(str(cfg.get("cwd") or "")).resolve() != root.resolve() or not _strata_path_is_within(
        resolved_path(cfg["exe"]), root
    ):
        raise ValueError("Strata engine/cwdは固定source_root配下で指定してください")
    _strata_engine_metadata(resolved_path(cfg["exe"]))
    native_value = option_value("--native")
    native_path = resolved_path(native_value) if native_value else None
    native_match = _GGUF_SHARD_RE.match(native_path.name) if native_path else None
    if native_match and int(native_match.group("index")) != 1:
        raise ValueError("Strata GGUFのprimary shard名が不正です")
    for value in required[1:]:
        path = resolved_path(str(value))
        match = _GGUF_SHARD_RE.match(path.name)
        if native_match and match:
            continue
        if not regular_nonempty(str(value or "")):
            raise ValueError(
                "Strata engine/GGUFが不足しています。セットアップを再実行してください"
            )
    checked_shards: set[Path] = set()
    for value in required[1:]:
        primary = resolved_path(str(value))
        match = _GGUF_SHARD_RE.match(primary.name)
        if not match:
            continue
        index = int(match.group("index"))
        count = int(match.group("count"))
        # Official two-shard configs also carry --ple-gguf pointing at shard 2;
        # the native path remains the sole primary whose declared set we walk.
        if index != 1:
            if primary in checked_shards:
                continue
            continue
        if count < 1:
            raise ValueError("Strata GGUFのprimary shard名が不正です")
        for shard_index in range(1, count + 1):
            shard = primary.with_name(
                f"{match.group('prefix')}-{shard_index:05d}-of-{count:05d}.gguf"
            )
            if shard in checked_shards:
                continue
            checked_shards.add(shard)
            if not shard.is_file():
                raise ValueError(
                    "Strata GGUFの全shardが揃っていません。セットアップを再実行してください"
                )
            try:
                if shard.stat().st_size <= 0:
                    raise ValueError(
                        "Strata GGUF shardが空です。セットアップを再実行してください"
                    )
            except OSError as exc:
                raise ValueError(
                    "Strata GGUF shardを検証できません。セットアップを再実行してください"
                ) from exc

    pack_value = option_value("--pack")
    mtp_value = option_value("--mtp")
    profile_value = option_value("--expert-profile")
    if not pack_value or not present(pack_value, kind="dir"):
        raise ValueError(
            "Strata pack artifactが不足しています。セットアップを再実行してください"
        )
    pack = resolved_path(pack_value)
    # Official setup writes native_experts.txt for the normal IQ pack and
    # index.txt + experts.bin for the AVX-512 Q2_0 pack.
    normal_pack = (pack / "native_experts.txt").is_file()
    avx512_pack = (pack / "index.txt").is_file() and (pack / "experts.bin").is_file()
    if not (normal_pack or avx512_pack):
        raise ValueError(
            "Strata pack artifactが不完全です。セットアップを再実行してください"
        )
    if not mtp_value or not present(mtp_value, kind="dir"):
        raise ValueError(
            "Strata MTP artifactが不足しています。セットアップを再実行してください"
        )
    mtp = resolved_path(mtp_value)
    if not (mtp / "experts.bin").is_file():
        raise ValueError(
            "Strata MTP artifactが不完全です。セットアップを再実行してください"
        )
    if not profile_value or not present(profile_value, kind="file"):
        raise ValueError(
            "Strata expert profileが不足しています。セットアップを再実行してください"
        )
    tokenizer = Path(str(cfg.get("tokenizer") or ""))
    if not tokenizer.is_absolute():
        tokenizer = root / tokenizer
    if not all(
        (tokenizer / name).is_file()
        for name in ("vocab.json", "merges.txt", "token_type.json")
    ):
        raise ValueError("Strata tokenizerが不足しています")
    vision = cfg.get("vision")
    if vision and (
        not isinstance(vision, dict)
        or not all(
            present(str(vision.get(key) or "")) for key in ("exe", "mmproj", "model")
        )
    ):
        raise ValueError("Strata vision artifactが不足しています")
    return cfg


def strata_launch_plan(
    config: Any, *, model: str, platform_name: str | None = None
) -> tuple[list[str], Path]:
    settings = strata_settings(config, model=model, platform_name=platform_name)
    if not settings["auto_start"]:
        raise ValueError("Strata endpointはoperator管理です")
    verify_strata_source(Path(settings["source_root"]))
    relocate_strata_artifact_paths(settings)
    prepared = strata_prepared_config(settings)
    verify_strata_engine(prepared)
    # Matches upstream setup.py write_run_script(), omitting only --open.
    args = [
        settings["python"],
        str(Path(settings["source_root"]) / "serve/server.py"),
        "--engine",
        "strata",
        "--config",
        settings["config_path"],
        "--host",
        settings["host"],
        "--port",
        str(settings["port"]),
    ]
    # Text-only managed Strata can bind its API before the first inference;
    # vision keeps the existing eager initialization contract.
    if not strata_vision_enabled(settings, prepared):
        args.append("--lazy")
    return args, Path(settings["source_root"])


def _connection_refused_error(exc: BaseException) -> bool:
    """Return whether *exc* proves that the Strata port has no listener."""

    if isinstance(exc, URLError):
        reason = exc.reason
        return isinstance(reason, BaseException) and _connection_refused_error(reason)
    if isinstance(exc, ConnectionRefusedError):
        return True
    return isinstance(exc, OSError) and (
        exc.errno == errno.ECONNREFUSED or getattr(exc, "winerror", None) == 10061
    )


def _strata_lifecycle_lease_count(config: Any, *, model: str) -> int:
    """Return active generation/consumer leases without starting a runtime.

    ``service_manager`` imports this module, so keep the ownership peek lazy
    and fail closed when readiness is used by a lightweight embedding that has
    no lifecycle ledger yet.
    """

    try:
        from src.service_manager import _peek_llama_cpp_lifecycle_lease

        endpoint = strata_base_url(config, model=model)
        _alias, count = _peek_llama_cpp_lifecycle_lease(endpoint)
        return max(0, int(count or 0))
    except Exception:
        return 0


def strata_api_key(config: Any) -> str:
    return str(
        os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
        or _config_get(config, "openai_compatible_local.api_key", "")
        or _config_get(config, "openai_compatible_local_api_key", "")
        or ""
    ).strip()


def strata_readiness(
    config: Any,
    *,
    model: str,
    fetch_json=None,
    owned_process: Any | None = None,
    startup_log_path: Path | None = None,
) -> dict[str, Any]:
    base_url = strata_base_url(config, model=model)
    key = strata_api_key(config)

    def fetch(url: str) -> Any:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with urlopen(Request(url, headers=headers), timeout=2) as response:
            return json.load(response)

    try:
        health = (fetch_json or fetch)(base_url.removesuffix("/v1") + "/health")
        models = (fetch_json or fetch)(base_url + "/models")
        rows = models.get("data") if isinstance(models, dict) else None
        exact = (
            any(isinstance(row, dict) and row.get("id") == model for row in rows)
            if isinstance(rows, list)
            else False
        )
        health_ok = bool(
            isinstance(health, dict)
            and health.get("service") == "strata"
            and health.get("status") == "ok"
            and isinstance(health.get("loaded"), bool)
        )
        health_context = health.get("max_context") if isinstance(health, dict) else None
        configured_context = _config_get(
            config,
            "openai_compatible_local.strata.context_size",
            32768,
        )
        context_ok = bool(
            isinstance(health_context, int)
            and not isinstance(health_context, bool)
            and health_context >= 0
            and (
                health_context == 0
                or (
                    isinstance(configured_context, int)
                    and not isinstance(configured_context, bool)
                    and health_context == configured_context
                )
            )
        )
        try:
            owned_process_live = owned_process is not None and owned_process.poll() is None
        except Exception:
            owned_process_live = False
        # Strata's lazy wrapper identifies the configured model in /health
        # before the first load, while /v1/models deliberately returns an
        # empty data list until the engine has loaded its weights.  This is a
        # process-readiness signal only: it is never folded into ``ready`` or
        # the strict exact-model accepting_requests gate below.  The service
        # manager supplies an owned process before using this private signal,
        # so a foreign listener cannot become an admitted lazy target.
        lazy_process_ready = bool(
            owned_process is not None
            and owned_process_live
            and health_ok
            and context_ok
            and health.get("loaded") is False
            and health.get("model") == model
            and isinstance(rows, list)
            and not rows
            and str(getattr(owned_process, "_aoi_local_runtime", "") or "")
            == "strata"
            and str(getattr(owned_process, "_aoi_local_model_alias", "") or "")
            == model
            and normalize_openai_compatible_base_url(
                str(getattr(owned_process, "_aoi_local_base_url", "") or "")
            )
            == normalize_openai_compatible_base_url(base_url)
        )
        accepting_requests = bool(
            health_ok
            and exact
        )
        ready = accepting_requests and health.get("loaded") is True
        startup_progress = None
        waiting = False
        if accepting_requests and not ready:
            startup_progress = _strata_startup_projection(
                owned_process,
                log_path=startup_log_path,
            )
            waiting = (
                _strata_lifecycle_lease_count(config, model=model) == 0
                and startup_progress is None
            )
        result = {
            "ready": ready,
            # A healthy unloaded wrapper performs lazy model loading on its
            # next request. This is distinct from loaded-model readiness.
            "accepting_requests": accepting_requests,
            "process_ready": lazy_process_ready,
            "running": True,
            "status": "ready" if ready else "waiting" if waiting else "loading",
            "context_size": health.get("max_context")
            if isinstance(health, dict)
            else None,
            "vision": health.get("images") is True
            if isinstance(health, dict)
            else False,
            "endpoint_offline": False,
            "auto_start_on_send": waiting,
        }
        if startup_progress is not None and not ready:
            result["startup_progress"] = startup_progress
        return result
    except Exception as exc:
        startup_progress = _strata_startup_projection(
            owned_process,
            log_path=startup_log_path,
        )
        if startup_progress is not None:
            return {
                "ready": False,
                "accepting_requests": False,
                "process_ready": False,
                "running": True,
                "status": "loading",
                "vision": False,
                "endpoint_offline": False,
                "startup_progress": startup_progress,
            }
        return {
            "ready": False,
            "accepting_requests": False,
            "process_ready": False,
            "running": False,
            "status": "offline",
            "vision": False,
            # Keep an internal distinction between a proven stopped listener
            # and an endpoint that responded with an error or malformed data.
            "endpoint_offline": _connection_refused_error(exc),
        }


def strata_setup_plan(
    settings: dict[str, Any], *, platform_name: str | None = None
) -> list[str]:
    platform_name = platform_name or sys.platform
    root = Path(settings["source_root"])
    profile = settings["runtime_profile"]
    args = [
        "--setup",
        "--no-start",
        "--yes",
        "--prebuilt",
        ENGINE_URL,
        "--family",
        profile["setup_family"],
        "--model",
        profile["setup_model"],
        "--context",
        str(settings["context_size"]),
        "--vision",
        settings["vision"],
        "--host",
        settings["host"],
        "--port",
        str(settings["port"]),
        "--data-dir",
        settings["data_dir"],
    ]
    if settings.get("gguf_dir"):
        args += ["--gguf-dir", settings["gguf_dir"]]
    if settings.get("vram_reserve_mib") is not None:
        args += ["--vram-reserve-mib", str(settings["vram_reserve_mib"])]
    if platform_name.startswith("win"):
        # cmd parses metacharacters even with shell=False; reject rather than
        # trying to escape an arbitrary command. Spaces remain supported.
        if any(
            any(char in arg for char in '&|<>^%!?"\r\n') for arg in [str(root), *args]
        ):
            raise ValueError("StrataのWindowsパスにcmd特殊文字は使用できません")
        return ["cmd.exe", "/d", "/c", str(root / "START-HERE.bat"), *args]
    if platform_name.startswith("linux"):
        return ["/bin/sh", str(root / "setup.sh"), *args]
    raise ValueError("Strata setupはWindows/Linux向けです")


def _git_source_is_clean(root: Path) -> bool:
    """Accept only the pinned commit with no source changes."""

    if not (root / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        dirty = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or result.stdout.strip() != STRATA_SOURCE_REVISION:
        return False
    for line in dirty.stdout.splitlines():
        # The provenance marker is generated by AoiTalk and is allowed to be
        # untracked beside an otherwise clean checkout.  Every other change,
        # including untracked source files, invalidates the pin.
        status_path = line[3:].strip() if len(line) >= 3 else ""
        status_path = status_path.strip('"')
        if status_path.replace("\\", "/") != _SOURCE_MARKER_NAME:
            return False
    return dirty.returncode == 0


def _source_marker_payload(source_files: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": STRATA_SOURCE_REPOSITORY,
        "release_tag": STRATA_MANIFEST.release_tag,
        "revision": STRATA_SOURCE_REVISION,
        "engine_version": STRATA_MANIFEST.engine_version,
        "archive_sha256": SOURCE_ARCHIVE_SHA256,
        "tree_sha256": SOURCE_TREE_SHA256,
        "tree_sha256_normalized": SOURCE_TREE_SHA256_NORMALIZED,
        "file_count": SOURCE_TREE_FILE_COUNT,
        "tree_bytes": SOURCE_TREE_BYTES,
        "tree_bytes_normalized": SOURCE_TREE_BYTES_NORMALIZED,
        "source_files": sorted(source_files),
    }


def _read_source_marker(root: Path) -> dict[str, Any] | None:
    marker = root / _SOURCE_MARKER_NAME
    if not marker.exists():
        return None
    if _is_reparse_point(marker) or not marker.is_file():
        raise ValueError("Strata source markerが不正です")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Strata source markerが不正です") from exc
    if not isinstance(payload, dict):
        raise ValueError("Strata source markerが不正です")
    for key, expected in _source_marker_payload([]).items():
        value = payload.get(key)
        if key == "source_files":
            if not isinstance(value, list) or len(value) != SOURCE_TREE_FILE_COUNT:
                raise ValueError("Strata source markerのfile listが不一致です")
        elif type(value) is not type(expected) or value != expected:
            raise ValueError(f"Strata source markerの{key}が不一致です")
    return payload


def _existing_source_is_pinned(root: Path) -> bool:
    marker = _read_source_marker(root)
    if _git_source_is_clean(root):
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z"],
                capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        if not _source_tree_matches_pin(
            root, SOURCE_TREE_SHA256, source_files=result.stdout.rstrip("\0").split("\0")
        ):
            raise ValueError(
                "Strata Git checkoutのtree checksumが固定manifestと一致しません。"
                "cleanでもcore.autocrlf=trueによるPython/C++/MarkdownのCRLF変換は未対応です。"
                "checksum済みarchiveを標準経路にするか、別folderへgit -c core.autocrlf=false -c core.eol=lf cloneで"
                "LF checkoutを準備してください（公式.gitattributesのbat=CRLF/sh=LFは対応済み）。"
                "既存checkoutは変更・改行変換しません"
            )
        return True
    if marker is None:
        return False
    return _source_tree_matches_pin(
        root, marker["tree_sha256"], source_files=marker["source_files"]
    )


def verify_strata_source(root: Path) -> None:
    """Read-only source verification before setup or managed launch."""
    if not root.is_dir() or not _existing_source_is_pinned(root):
        raise ValueError(
            "Strata sourceは指定revisionのclean checkoutが必要です。既存folderは上書きしません"
        )


def install_strata_source(root: Path) -> None:
    """Download only the pinned small source archive; never overwrite user code."""
    root = _canonical_source_root(None, root)
    if root.exists():
        verify_strata_source(root)
        return
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".strata-source-", dir=root.parent
    ) as temporary:
        temp = Path(temporary)
        archive = temp / "source.tar.gz"
        archive_digest = hashlib.sha256()
        with urlopen(SOURCE_URL, timeout=60) as response, archive.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Strata source archiveが上限を超えました")
                archive_digest.update(chunk)
                output.write(chunk)
        if archive_digest.hexdigest() != SOURCE_ARCHIVE_SHA256:
            raise ValueError("Strata source archiveのchecksumが不一致です")
        unpacked = temp / "source"
        unpacked.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if sum(member.size for member in members) > MAX_SOURCE_BYTES:
                raise ValueError("Strata source展開サイズが上限を超えました")
            for member in members:
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError("不正なStrata source archiveです")
            bundle.extractall(unpacked, members=members, filter="data")
        sources = list(unpacked.iterdir())
        if len(sources) != 1 or not (sources[0] / "setup.py").is_file():
            raise ValueError("Strata source archiveの構成が不正です")
        tree_sha, file_count, tree_bytes = _canonical_tree_digest(sources[0])
        normalized_sha, normalized_count, normalized_bytes = _canonical_tree_digest(
            sources[0], normalize_windows_text=True
        )
        if (
            tree_sha != SOURCE_TREE_SHA256
            or file_count != SOURCE_TREE_FILE_COUNT
            or tree_bytes != SOURCE_TREE_BYTES
            or normalized_sha != SOURCE_TREE_SHA256_NORMALIZED
            or normalized_count != SOURCE_TREE_FILE_COUNT
            or normalized_bytes != SOURCE_TREE_BYTES_NORMALIZED
        ):
            raise ValueError("Strata source archiveのtree checksumが不一致です")
        marker = sources[0] / _SOURCE_MARKER_NAME
        source_files = [
            path.relative_to(sources[0]).as_posix()
            for path in sources[0].rglob("*") if path.is_file()
        ]
        marker.write_text(
            json.dumps(
                _source_marker_payload(source_files),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        shutil.move(str(sources[0]), root)


def _setup_requirement_gb(settings: dict[str, Any]) -> float:
    """Estimate setup volume usage while keeping profile knobs extensible."""

    profile = settings.get("runtime_profile") or {}

    def amount(key: str, default: float) -> float:
        value = profile.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Strata profileの{key}が不正です") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Strata profileの{key}が不正です")
        return value

    # Existing GGUF is an input source in offline mode.  MTP, pack,
    # tokenizer, runtime wheels and staging still land on data_dir.
    required = 0.0 if settings.get("gguf_dir") else amount("download_gb", 0.0)
    required += amount("mtp_download_gb", _DEFAULT_MTP_GB)
    required += amount("pack_download_gb", 0.0)
    required += amount("tokenizer_download_gb", 0.0)

    platform_name = str(settings.get("platform") or sys.platform).casefold()
    platform_extras = profile.get("setup_extra_gb_by_platform", {})
    if isinstance(platform_extras, dict):
        platform_key = "windows" if platform_name.startswith("win") else "linux"
        runtime_default = platform_extras.get(platform_key, _DEFAULT_SETUP_RUNTIME_GB)
    else:
        runtime_default = _DEFAULT_SETUP_RUNTIME_GB
    required += amount("runtime_download_gb", runtime_default)
    required += amount("staging_gb", 0.0)

    if str(profile.get("setup_model") or "") == "Q2_0":
        required += amount("q2_extra_gb", _DEFAULT_Q2_EXTRA_GB)
    if str(settings.get("vision") or "none") != "none":
        vision_key = (
            "vision_gpu_download_gb"
            if str(settings.get("vision")) == "gpu"
            else "vision_download_gb"
        )
        required += amount(vision_key, amount("vision_download_gb", _DEFAULT_VISION_GB))

    backend = str(settings.get("backend") or "").casefold()
    backend_extras = profile.get("setup_extra_gb_by_backend", {})
    if backend and isinstance(backend_extras, dict):
        backend_value = backend_extras.get(backend, 0.0)
        try:
            backend_value = float(backend_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Strata profileのbackend setup容量が不正です") from exc
        if not math.isfinite(backend_value) or backend_value < 0:
            raise ValueError("Strata profileのbackend setup容量が不正です")
        required += backend_value
    return required


def run_strata_setup(
    settings: dict[str, Any],
    *,
    platform_name: str | None = None,
    cancel_event: threading.Event | None = None,
    on_process_started: Callable[[subprocess.Popen], None] | None = None,
    on_process_finished: Callable[[subprocess.Popen], None] | None = None,
) -> None:
    """Called exclusively by a confirmed setup task, never readiness/startup."""
    from src.services.local_llm_paths import repository_root

    repo = repository_root().resolve()
    effective_settings = dict(settings)
    root = _canonical_source_root(None, settings["source_root"])
    try:
        data = _canonical_model_root(None, settings["data_dir"], label="data_dir")
    except ValueError as exc:
        if "repository" in str(exc).casefold() or "inside an AoiTalk" in str(exc):
            raise ValueError(
                "Strataの大容量データ保存先はrepository外を指定してください"
            ) from exc
        raise
    gguf_value = str(settings.get("gguf_dir") or "").strip()
    gguf = _canonical_model_root(None, gguf_value, label="gguf_dir") if gguf_value else None
    effective_settings["source_root"] = str(root)
    effective_settings["data_dir"] = str(data)
    effective_settings["gguf_dir"] = str(gguf or "")
    effective_settings["platform"] = platform_name or sys.platform
    data.mkdir(parents=True, exist_ok=True)
    if gguf is not None and not gguf.is_dir():
        raise ValueError("オフラインGGUF folderが存在しません")
    required_gb = _setup_requirement_gb(effective_settings)
    if shutil.disk_usage(data).free < math.ceil(required_gb * 1_000_000_000):
        raise ValueError(
            "Strataの保存先空き容量が不足しています。モデル/MTP/vision/一時領域を確認してください"
        )
    command = strata_setup_plan(effective_settings, platform_name=platform_name)
    previous_config = _read_strata_user_config(Path(effective_settings["config_path"]))
    install_strata_source(root)
    log = repo / "logs/models/strata-setup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    from src.service_manager import (
        _remove_tracked_process,
        _terminate_process_tree,
        _track_child_process,
    )

    proc = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Strata公式setupがキャンセルされました")
        with log.open("ab") as output:
            kwargs: dict[str, Any] = {
                "cwd": str(root),
                "stdout": output,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                # Some upstream repair paths consult this env instead of
                # argv. Pin both, even when the parent process chose latest.
                "env": {**os.environ, "STRATA_PREBUILT_URL": ENGINE_URL},
            }
            if (platform_name or sys.platform).startswith("win"):
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(command, **kwargs)
            _track_child_process(proc)
            if on_process_started is not None:
                on_process_started(proc)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Strata公式setupがキャンセルされました")
            if proc.wait(timeout=8 * 60 * 60) != 0:
                raise RuntimeError(
                    "Strata公式setupが失敗しました。logs/models/strata-setup.logを確認し、同じ設定で再試行してください"
                )
        _restore_strata_user_config(Path(effective_settings["config_path"]), previous_config)
        _restore_strata_performance_args(effective_settings)
        relocate_strata_artifact_paths(effective_settings)
        verify_strata_source(root)
        verify_strata_engine(strata_prepared_config(effective_settings))
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    _terminate_process_tree(proc)
            finally:
                _remove_tracked_process(proc)
                if on_process_finished is not None:
                    on_process_finished(proc)
