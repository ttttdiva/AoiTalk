"""Canonical filesystem paths for AoiTalk-managed local LLM assets.

The local-runtime code historically grew several independent fallbacks (the
current working directory, a drive-level ``AI`` directory, and the user's
home directory).  This module is intentionally small and side-effect free by
default so every caller can use one path contract.  Managed runtimes live in
``.aoitalk-local-llm/runtime`` while managed model weights live beside them in
``.aoitalk-local-llm/models``.  A model root may still be explicitly supplied
through configuration or an environment variable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Iterable, Mapping


_LOCAL_LLM_STORAGE_NAME = ".aoitalk-local-llm"
_RUNTIME_ENV_NAMES = ("AOITALK_LOCAL_LLM_RUNTIME_ROOT",)
# Keep both names for compatibility with older native launches.  The generic
# LLAMA_CPP name remains first to preserve the existing service-manager
# precedence when both variables are present.
_MODEL_ENV_NAMES = ("LLAMA_CPP_MODEL_ROOT", "AOITALK_LLAMA_CPP_MODEL_ROOT")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})")


def repository_root() -> Path:
    """Return the repository root independently of the process CWD."""

    # ``src/services/local_llm_paths.py`` -> ``<repo>/src/services``.
    return Path(__file__).resolve().parents[2]


def local_llm_storage_root() -> Path:
    """Return the ignored repository-local managed-data directory."""

    return repository_root() / _LOCAL_LLM_STORAGE_NAME


def default_runtime_root() -> Path:
    """Return the repository-local managed runtime root."""

    return local_llm_storage_root() / "runtime"


def default_managed_models_root() -> Path:
    """Return the parent directory for managed model formats."""

    return local_llm_storage_root() / "models"


def default_llama_cpp_model_root() -> Path:
    """Return the default GGUF storage root for llama.cpp profiles."""

    return default_managed_models_root() / "llama_cpp"


@dataclass(frozen=True)
class ResolvedLocalLlmRoot:
    """Resolved path plus provenance used by settings/status APIs.

    ``path`` is the canonical absolute path.  ``default`` is kept on the
    value so callers need not independently reconstruct the default when
    rendering a settings envelope.  The object is path-like for compatibility
    with older call sites that passed a resolved root directly to ``Path`` or
    ``os.fspath``.
    """

    path: Path
    source: str
    default: Path
    override: str = ""

    @property
    def root(self) -> Path:
        return self.path

    @property
    def resolved_path(self) -> Path:
        return self.path

    @property
    def default_path(self) -> Path:
        return self.default

    @property
    def is_default(self) -> bool:
        return self.source == "default"

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ResolvedLocalLlmRoot):
            return (
                self.path == other.path
                and self.source == other.source
                and self.default == other.default
                and self.override == other.override
            )
        try:
            return self.path == Path(other)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    missing = object()
    if callable(getter):
        try:
            direct = getter(key, missing)
            if direct is not missing:
                return direct
        except TypeError:
            pass
    value = config
    for part in key.split("."):
        if isinstance(value, Mapping):
            if part not in value:
                return default
            value = value[part]
            continue
        value = getattr(value, part, missing)
        if value is missing:
            return default
    return value


def _raw_llama_settings(config: Any, settings: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = _config_get(config, "openai_compatible_local.llama_cpp", {})
    if isinstance(raw, Mapping):
        result = dict(raw)
    else:
        result = {}
    if settings:
        # Explicit settings passed by a resolver caller (for example the
        # runtime manager) are an overlay, not a second source of truth.
        result.update({key: value for key, value in settings.items() if value is not None})
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _relative_to_repository(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repository_root() / candidate
    return candidate


def _contains_symlink(path: Path) -> bool:
    """Return whether an existing path component is a symlink/junction."""

    try:
        current = Path(path.anchor) if path.anchor else Path()
        for part in path.parts[1:] if path.anchor else path.parts:
            current = current / part
            if current.is_symlink() or os.path.islink(str(current)):
                return True
            # Windows junctions/reparse points are not consistently reported
            # by Path.is_symlink().  Inspect the native file attributes when
            # available and fail closed on an unstatable component.
            try:
                attributes = int(getattr(os.lstat(str(current)), "st_file_attributes", 0))
            except (AttributeError, OSError):
                attributes = 0
            if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                return True
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _canonicalize(value: Any, *, kind: str, create: bool) -> Path:
    text = _text(value)
    if not text:
        raise ValueError(f"{kind} root cannot be empty")
    if "\x00" in text:
        raise ValueError(f"invalid {kind} root")
    if os.pathsep in text:
        raise ValueError(f"{kind} root must be one directory, not a path list")
    if os.name != "nt" and _WINDOWS_ABSOLUTE_RE.match(text):
        raise ValueError(f"invalid {kind} root: Windows drive/UNC paths are unsupported here")
    if os.name == "nt":
        try:
            windows_value = PureWindowsPath(text)
            # ``C:foo`` is drive-relative and ``\\foo`` is root-relative;
            # neither has stable semantics across process CWDs.
            if windows_value.drive and not windows_value.is_absolute():
                raise ValueError(f"{kind} root must be an absolute path")
            if not windows_value.drive and windows_value.root:
                raise ValueError(f"{kind} root must be an absolute path")
        except ValueError:
            raise
        except (TypeError, RuntimeError):
            raise ValueError(f"invalid {kind} root") from None
    candidate = _relative_to_repository(text)
    try:
        if _contains_symlink(candidate):
            raise ValueError(f"{kind} root cannot contain a symlink or junction")
        resolved = candidate.resolve(strict=False)
        repo = repository_root().resolve(strict=False)
        storage = local_llm_storage_root().resolve(strict=False)
        filesystem_root = Path(resolved.anchor)
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(f"{kind} root"):
            raise
        raise ValueError(f"invalid {kind} root") from exc

    if resolved == filesystem_root:
        raise ValueError(f"{kind} root cannot be a filesystem root")
    if resolved == home:
        raise ValueError(f"{kind} root cannot be the home directory")
    if resolved == repo:
        raise ValueError(f"{kind} root cannot be the repository root")
    # Never allow managed assets to be placed in source control metadata or a
    # source tree.  The dedicated ignored storage area is the sole permitted
    # repository-local location.
    if repo in resolved.parents:
        relative = resolved.relative_to(repo)
        first = relative.parts[0].casefold() if relative.parts else ""
        if first in {".git", "src", "frontend", "mobile", "tests", "scripts", "docs"}:
            raise ValueError(f"{kind} root cannot be inside an AoiTalk source directory")
        allowed = (
            (storage / "runtime")
            if kind == "runtime"
            else (storage / "models")
        ).resolve(strict=False)
        if resolved != allowed and allowed not in resolved.parents:
            raise ValueError(
                f"{kind} root inside the repository must use the dedicated "
                f"{allowed.relative_to(repo)} subtree"
            )
    # Runtime and model trees must stay separate even when a caller supplies a
    # path under the repository-local storage directory.
    runtime_root = default_runtime_root().resolve(strict=False)
    model_root = default_managed_models_root().resolve(strict=False)
    if kind == "runtime" and (resolved == model_root or model_root in resolved.parents):
        raise ValueError("runtime root cannot be inside the managed model directory")
    if kind == "model" and (resolved == runtime_root or runtime_root in resolved.parents):
        raise ValueError("model root cannot be inside the managed runtime directory")

    if candidate.exists() and not candidate.is_dir():
        raise ValueError(f"{kind} root must be a directory")
    if create:
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"unable to create {kind} root") from exc
    return resolved


def _env_value(names: Iterable[str]) -> tuple[str, str] | tuple[None, None]:
    for name in names:
        value = _text(os.getenv(name))
        if value:
            return value, name
    return None, None


def _create_default(path: Path, kind: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"unable to create {kind} root") from exc


def resolve_managed_runtime_root(
    config: Any,
    explicit: str | Path | None = None,
    *,
    create: bool = False,
) -> ResolvedLocalLlmRoot:
    """Resolve the managed runtime root (config > env > repository default)."""

    default = _canonicalize(default_runtime_root(), kind="runtime", create=False)
    if explicit is not None and _text(explicit):
        path = _canonicalize(explicit, kind="runtime", create=create)
        return ResolvedLocalLlmRoot(path, "explicit", default, _text(explicit))

    configured = _text(_config_get(config, "openai_compatible_local.runtime_root", ""))
    if configured:
        path = _canonicalize(configured, kind="runtime", create=create)
        return ResolvedLocalLlmRoot(path, "persisted", default, configured)
    environment, _name = _env_value(_RUNTIME_ENV_NAMES)
    if environment:
        path = _canonicalize(environment, kind="runtime", create=create)
        return ResolvedLocalLlmRoot(path, "environment", default, environment)
    if create:
        _create_default(default, "runtime")
    return ResolvedLocalLlmRoot(default, "default", default, "")


def canonicalize_llama_cpp_model_root_override(
    value: str | Path | None,
    *,
    create: bool = False,
) -> Path | None:
    """Canonicalize a user-supplied model root, or return ``None`` for reset."""

    text = _text(value)
    if not text:
        return None
    return _canonicalize(text, kind="model", create=create)


def resolve_llama_cpp_model_root(
    config: Any,
    settings: Mapping[str, Any] | None = None,
    explicit: str | Path | None = None,
    *,
    create: bool = False,
) -> ResolvedLocalLlmRoot:
    """Resolve one canonical llama.cpp model root (env > config > default).

    ``explicit`` is reserved for request-scoped overrides and therefore wins
    over environment/config.  Empty explicit/config values intentionally reset
    to the repository-local default.
    """

    default = _canonicalize(default_llama_cpp_model_root(), kind="model", create=False)
    if explicit is not None:
        text = _text(explicit)
        if text:
            path = _canonicalize(text, kind="model", create=create)
            return ResolvedLocalLlmRoot(path, "explicit", default, text)
        if create:
            _create_default(default, "model")
        return ResolvedLocalLlmRoot(default, "default", default, "")

    environment, _name = _env_value(_MODEL_ENV_NAMES)
    if environment:
        path = _canonicalize(environment, kind="model", create=create)
        return ResolvedLocalLlmRoot(path, "environment", default, environment)

    raw = _raw_llama_settings(config, settings)
    configured = _text(raw.get("model_root"))
    if configured:
        path = _canonicalize(configured, kind="model", create=create)
        return ResolvedLocalLlmRoot(path, "persisted", default, configured)
    legacy = _text(raw.get("model_dir"))
    # ``model_dir`` was an early persisted key.  It is a compatibility
    # fallback only when the canonical key has never been written.  Once a
    # user saves an empty ``model_root`` (the Settings reset action), that
    # explicit empty value must suppress the legacy path rather than silently
    # restoring an old machine-specific directory.
    if "model_root" not in raw and legacy:
        path = _canonicalize(legacy, kind="model", create=create)
        return ResolvedLocalLlmRoot(path, "legacy_config", default, legacy)
    if create:
        _create_default(default, "model")
    return ResolvedLocalLlmRoot(default, "default", default, "")


def llama_cpp_model_discovery_roots(
    config: Any,
    settings: Mapping[str, Any] | None = None,
    *,
    explicit: str | Path | None = None,
    create: bool = False,
) -> list[Path]:
    """Return the one canonical model root used by discovery/status/launch."""

    resolved = resolve_llama_cpp_model_root(
        config,
        settings=settings,
        explicit=explicit,
        create=create,
    )
    return [resolved.path]


def validate_managed_child(
    root: str | os.PathLike[str],
    child: str | os.PathLike[str],
    *,
    kind: str = "model",
    create_parent: bool = False,
) -> Path:
    """Resolve one managed artifact path while enforcing root containment."""

    safe_root = _canonicalize(root, kind=kind, create=create_parent)
    candidate = Path(child)
    if not candidate.is_absolute():
        candidate = safe_root / candidate
    if _contains_symlink(candidate):
        raise ValueError(f"{kind} child cannot contain a symlink or junction")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"invalid {kind} child") from exc
    if resolved == safe_root or safe_root not in resolved.parents:
        raise ValueError(f"{kind} child escaped its managed root")
    if create_parent:
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"unable to create {kind} child parent") from exc
        if _contains_symlink(resolved.parent):
            raise ValueError(f"{kind} child parent cannot contain a symlink or junction")
        resolved = resolved.resolve(strict=False)
        if safe_root not in resolved.parents:
            raise ValueError(f"{kind} child escaped its managed root")
    return resolved


__all__ = [
    "ResolvedLocalLlmRoot",
    "repository_root",
    "local_llm_storage_root",
    "default_runtime_root",
    "default_managed_models_root",
    "default_llama_cpp_model_root",
    "resolve_managed_runtime_root",
    "resolve_llama_cpp_model_root",
    "canonicalize_llama_cpp_model_root_override",
    "llama_cpp_model_discovery_roots",
    "validate_managed_child",
]
