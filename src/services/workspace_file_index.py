"""User Files / Project Files inventory for the shared ``workspace_file`` media index.

The canonical source rows (``workspace_file_entries``) are produced here by a
non-link-following scan of ``workspaces/_users/user_<uuid>`` and
``workspaces/_projects/project_<uuid>``.  Embedding, verification, ACL and
original retrieval reuse the existing media index pipeline unchanged; this
module only decides which regular files are sources, keeps their identity
stable across moves/renames and reports coverage.

Additional storage roots (NAS etc.) and ``.trash`` are never scanned.
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import re
import stat
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .media_index_worker import PRIORITY_CLEANUP

log = logging.getLogger(__name__)

FILES_KIND = "workspace_file"
USER_SEGMENT = re.compile(r"^user_([0-9a-fA-F-]{36})$")
PROJECT_SEGMENT = re.compile(r"^project_([0-9a-fA-F-]{36})$")

# Text/code read as text units (4000 characters).  Japanese Windows assets
# (Tera Term macros, bat files, device dumps) are decoded with a CP932
# fallback by the media index.
TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".text", ".rst", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm", ".css", ".scss", ".sh", ".bash", ".ps1",
    ".psm1", ".bat", ".cmd", ".sql", ".rs", ".go", ".java", ".kt", ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".toml", ".ini", ".cfg", ".conf", ".env.example", ".xml", ".csv",
    ".tsv", ".log", ".ttl", ".lst", ".diff", ".patch", ".vb", ".bas", ".vbs", ".tex", ".srt", ".vtt",
})
OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
MAIL_SUFFIXES = frozenset({".msg", ".eml"})
PDF_SUFFIXES = frozenset({".pdf"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".mp3", ".mpga", ".m4a", ".ogg", ".opus", ".aac", ".wma"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"})

# Generic build/runtime artefacts.  Derived from the live inventory:
# - ``*.log`` is excluded only for User Files.  The admin daily/ folder holds
#   ~760 build/test logs, while FWD Project ``temp/*/log/*.log`` are firewall
#   policy dumps (primary material).
# - ``tmp``/``temp`` are *not* excluded: FWD keeps request xlsx/msg/ttl there.
# - ``daily/`` is not excluded: it also holds FW input/ttl/xlsx/macro outputs;
#   its archives/APKs/logs/caches are removed by the generic rules below.
DEFAULT_EXCLUDE_PATTERNS = (
    "node_modules/", ".git/", "__pycache__/", ".venv/", "venv/", ".next/", "dist/", "build/",
    ".cache/", "cache/", ".pytest_cache/", ".mypy_cache/", ".gradle/",
    "*.tmp", "*.temp", "*.lock", "*.pyc", "*.pyo", "*.log.exit", "*.exit", "*.apk", "*.aab", "*.ipa",
    "*.zip", "*.7z", "*.rar", "*.tar", "*.tar.gz", "*.tgz", "*.gz", "*.bz2", "*.xz", "*.zst",
    "*.exe", "*.dll", "*.so", "*.dylib", "*.bin", "*.iso", "*.dmp", "*.sqlite", "*.sqlite3", "*.db",
)

DEFAULT_FILES_INDEX_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "scopes": {"user": True, "project": True},
    "include_roots": [],
    "exclude_patterns": list(DEFAULT_EXCLUDE_PATTERNS),
    "include_patterns": [],
    "scope_exclude_patterns": {"user": ["*.log"], "project": []},
    "max_file_bytes": 256 * 1024 * 1024,
    "max_text_file_bytes": 8 * 1024 * 1024,
    "max_concurrency": 1,
    "scan_interval_seconds": 900,
    "max_failed_attempts": 5,
    "heavy_media_policy": {
        "defer_bytes": 100 * 1024 * 1024,
        "max_media_bytes": 4 * 1024 * 1024 * 1024,
        "max_duration_seconds": 7200,
    },
    "overrides": {"users": {}, "projects": {}},
}
MAX_CONCURRENCY = 4
MOVE_MATCH_WINDOW = timedelta(hours=24)
PRIORITY_NORMAL = 10
PRIORITY_HEAVY = 20
HASH_BATCH_FILES = 200
HASH_BATCH_BYTES = 512 * 1024 * 1024
HASH_BATCH_SECONDS = 20.0


# ---------------------------------------------------------------- settings

def default_files_index_settings() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_FILES_INDEX_SETTINGS)


def _string_list(value, *, name, strict, limit=500):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError(f"{name} must be a list of strings")
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            if strict:
                raise ValueError(f"{name} contains an invalid entry")
            continue
        normalized = item.strip().replace("\\", "/")
        if normalized not in result:
            result.append(normalized)
    if len(result) > limit:
        if strict:
            raise ValueError(f"{name} exceeds {limit} entries")
        result = result[:limit]
    return result


def _bounded_int(value, default, minimum, maximum, *, name, strict):
    if isinstance(value, bool) or value is None:
        if strict and value is not None:
            raise ValueError(f"{name} must be an integer")
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{name} must be an integer") from None
        return default
    if not minimum <= number <= maximum:
        if strict:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return min(max(number, minimum), maximum)
    return number


def _normalize_rules(value, *, strict, name):
    """Per-owner override: optional enabled/include_roots/exclude/include."""
    if not isinstance(value, dict):
        if strict:
            raise ValueError(f"{name} must be an object")
        return {}
    unknown = set(value) - {"enabled", "include_roots", "exclude_patterns", "include_patterns"}
    if unknown and strict:
        raise ValueError(f"{name} has unknown keys: {', '.join(sorted(unknown))}")
    result = {}
    if "enabled" in value and value["enabled"] is not None:
        if not isinstance(value["enabled"], bool):
            if strict:
                raise ValueError(f"{name}.enabled must be a boolean")
        else:
            result["enabled"] = value["enabled"]
    for key in ("include_roots", "exclude_patterns", "include_patterns"):
        if key in value and value[key] is not None:
            result[key] = _string_list(value[key], name=f"{name}.{key}", strict=strict)
    for root in result.get("include_roots", []):
        _validate_relative_root(root, strict=strict, name=f"{name}.include_roots")
    return result


def _validate_relative_root(root, *, strict, name):
    parts = [part for part in root.strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts) or root.startswith("/") or ":" in root:
        if strict:
            raise ValueError(f"{name} must contain relative folders")
        return False
    return True


def normalize_files_index_settings(value: Any, *, strict: bool = False) -> dict[str, Any]:
    """Validate a settings object; ``strict`` raises instead of repairing."""
    defaults = default_files_index_settings()
    if value is None:
        return defaults
    if not isinstance(value, dict):
        if strict:
            raise ValueError("files_index settings must be an object")
        return defaults
    known = set(defaults)
    unknown = set(value) - known
    if unknown and strict:
        raise ValueError(f"Unknown files_index keys: {', '.join(sorted(unknown))}")
    result = defaults
    if "enabled" in value:
        if isinstance(value["enabled"], bool):
            result["enabled"] = value["enabled"]
        elif strict:
            raise ValueError("enabled must be a boolean")
    scopes = value.get("scopes")
    if scopes is not None:
        if not isinstance(scopes, dict):
            if strict:
                raise ValueError("scopes must be an object")
        else:
            for scope in ("user", "project"):
                if scope in scopes:
                    if isinstance(scopes[scope], bool):
                        result["scopes"][scope] = scopes[scope]
                    elif strict:
                        raise ValueError(f"scopes.{scope} must be a boolean")
            if strict and set(scopes) - {"user", "project"}:
                raise ValueError("scopes accepts only user/project")
    for key in ("include_roots", "exclude_patterns", "include_patterns"):
        if key in value:
            result[key] = _string_list(value[key], name=key, strict=strict)
    result["include_roots"] = [root for root in result["include_roots"]
                               if _validate_relative_root(root, strict=strict, name="include_roots")]
    scope_patterns = value.get("scope_exclude_patterns")
    if scope_patterns is not None:
        if not isinstance(scope_patterns, dict):
            if strict:
                raise ValueError("scope_exclude_patterns must be an object")
        else:
            for scope in ("user", "project"):
                if scope in scope_patterns:
                    result["scope_exclude_patterns"][scope] = _string_list(
                        scope_patterns[scope], name=f"scope_exclude_patterns.{scope}", strict=strict)
    result["max_file_bytes"] = _bounded_int(value.get("max_file_bytes"), result["max_file_bytes"],
                                            1, 64 * 1024 ** 3, name="max_file_bytes", strict=strict)
    result["max_text_file_bytes"] = _bounded_int(value.get("max_text_file_bytes"), result["max_text_file_bytes"],
                                                 1, 256 * 1024 ** 2, name="max_text_file_bytes", strict=strict)
    result["max_concurrency"] = _bounded_int(value.get("max_concurrency"), result["max_concurrency"],
                                             1, MAX_CONCURRENCY, name="max_concurrency", strict=strict)
    result["scan_interval_seconds"] = _bounded_int(value.get("scan_interval_seconds"),
                                                   result["scan_interval_seconds"], 60, 7 * 24 * 3600,
                                                   name="scan_interval_seconds", strict=strict)
    result["max_failed_attempts"] = _bounded_int(value.get("max_failed_attempts"), result["max_failed_attempts"],
                                                 1, 100, name="max_failed_attempts", strict=strict)
    heavy = value.get("heavy_media_policy")
    if heavy is not None:
        if not isinstance(heavy, dict):
            if strict:
                raise ValueError("heavy_media_policy must be an object")
        else:
            if strict and set(heavy) - {"defer_bytes", "max_media_bytes", "max_duration_seconds"}:
                raise ValueError("heavy_media_policy has unknown keys")
            policy = result["heavy_media_policy"]
            policy["defer_bytes"] = _bounded_int(heavy.get("defer_bytes"), policy["defer_bytes"], 1,
                                                 64 * 1024 ** 3, name="heavy_media_policy.defer_bytes", strict=strict)
            policy["max_media_bytes"] = _bounded_int(heavy.get("max_media_bytes"), policy["max_media_bytes"], 1,
                                                     64 * 1024 ** 3, name="heavy_media_policy.max_media_bytes",
                                                     strict=strict)
            policy["max_duration_seconds"] = _bounded_int(
                heavy.get("max_duration_seconds"), policy["max_duration_seconds"], 1, 7 * 24 * 3600,
                name="heavy_media_policy.max_duration_seconds", strict=strict)
    overrides = value.get("overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            if strict:
                raise ValueError("overrides must be an object")
        else:
            for group, label in (("users", "user"), ("projects", "project")):
                entries = overrides.get(group) or {}
                if not isinstance(entries, dict):
                    if strict:
                        raise ValueError(f"overrides.{group} must be an object")
                    continue
                for owner, rules in entries.items():
                    try:
                        key = str(uuid.UUID(str(owner)))
                    except ValueError:
                        if strict:
                            raise ValueError(f"overrides.{group} keys must be {label} UUIDs") from None
                        continue
                    normalized = _normalize_rules(rules, strict=strict, name=f"overrides.{group}.{key}")
                    if normalized:
                        result["overrides"][group][key] = normalized
    return result


_settings_lock = threading.Lock()
_settings_cache: tuple[float, dict[str, Any]] | None = None
_settings_provider: Callable[[], Any] | None = None
SETTINGS_TTL_SECONDS = 15.0


def set_files_index_settings_provider(provider: Callable[[], Any] | None) -> None:
    """Test/embedding hook; ``None`` restores the DB-backed provider."""
    global _settings_provider, _settings_cache
    with _settings_lock:
        _settings_provider = provider
        _settings_cache = None


def invalidate_files_index_settings() -> None:
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


def _db_settings():
    from ..app_config_store import load_app_config_snapshot_sync

    config = load_app_config_snapshot_sync()
    search = config.get("search") if isinstance(config, dict) else None
    return search.get("files_index") if isinstance(search, dict) else None


def load_files_index_settings() -> dict[str, Any]:
    """DB-backed (``search.files_index``) settings with a short TTL cache."""
    global _settings_cache
    now = time.monotonic()
    with _settings_lock:
        cached = _settings_cache
        provider = _settings_provider
    if cached is not None and now - cached[0] < SETTINGS_TTL_SECONDS:
        return copy.deepcopy(cached[1])
    try:
        raw = provider() if provider is not None else _db_settings()
    except Exception:
        log.warning("Files index settings unavailable; using defaults", exc_info=True)
        raw = None
    settings = normalize_files_index_settings(raw)
    with _settings_lock:
        _settings_cache = (now, settings)
    return copy.deepcopy(settings)


def save_files_index_changes(changes: dict[str, Any]) -> bool:
    """Persist dotted ``search.files_index`` changes through the canonical store."""
    from ..app_config_store import update_app_config_keys_sync

    if _settings_provider is not None:
        raise RuntimeError("files_index settings provider is read-only")
    ok = update_app_config_keys_sync(changes)
    invalidate_files_index_settings()
    return ok


@dataclass(frozen=True)
class OwnerRules:
    enabled: bool
    include_roots: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    include_patterns: tuple[str, ...]
    max_file_bytes: int
    max_text_file_bytes: int
    defer_bytes: int
    max_media_bytes: int
    max_duration_seconds: int


def owner_rules(settings: dict[str, Any], scope: str, owner_id) -> OwnerRules:
    owner = str(owner_id)
    override = (settings.get("overrides") or {}).get("users" if scope == "user" else "projects", {}).get(owner, {})
    enabled = (bool(settings.get("enabled")) and bool((settings.get("scopes") or {}).get(scope, False))
               and override.get("enabled", True) is not False)
    roots = override.get("include_roots") if override.get("include_roots") is not None else settings.get("include_roots")
    excludes = [*settings.get("exclude_patterns", []),
                *(settings.get("scope_exclude_patterns") or {}).get(scope, []),
                *override.get("exclude_patterns", [])]
    includes = [*settings.get("include_patterns", []), *override.get("include_patterns", [])]
    heavy = settings.get("heavy_media_policy") or {}
    return OwnerRules(enabled, tuple(r.strip("/") for r in roots or []), tuple(excludes), tuple(includes),
                      int(settings["max_file_bytes"]), int(settings["max_text_file_bytes"]),
                      int(heavy.get("defer_bytes", 1)), int(heavy.get("max_media_bytes", 1)),
                      int(heavy.get("max_duration_seconds", 1)))


# ----------------------------------------------------------- classification

def index_format(name: str) -> str | None:
    lowered = name.lower()
    suffix = PurePosixPath(lowered).suffix
    if lowered.endswith(".env.example"):
        return "text"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    if suffix in MAIL_SUFFIXES:
        return "mail"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return None


def is_office_lock_file(name: str) -> bool:
    """Office's ``~$`` owner-lock stub (a few bytes of user name, not a document)."""
    return name.startswith("~$") and PurePosixPath(name.lower()).suffix in OFFICE_SUFFIXES


def _match_pattern(rel_path: str, pattern: str) -> bool:
    path = rel_path.casefold()
    pattern = pattern.casefold().strip()
    if not pattern:
        return False
    if pattern.endswith("/"):
        directory = pattern.rstrip("/")
        parts = path.split("/")[:-1]
        if "/" in directory:
            return any(fnmatch.fnmatchcase("/".join(parts[:index + 1]), directory)
                       for index in range(len(parts)))
        return any(fnmatch.fnmatchcase(part, directory) for part in parts)
    if "/" in pattern:
        return fnmatch.fnmatchcase(path, pattern.lstrip("/")) or fnmatch.fnmatchcase(path, pattern.lstrip("/") + "/*")
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)


def _directory_excluded(rel_dir: str, rules: OwnerRules) -> str | None:
    """Directory-level exclusion (not descended) unless an include may cover it."""
    name = rel_dir.rsplit("/", 1)[-1]
    reason = None
    if name.startswith("."):
        reason = "hidden"
    else:
        probe = rel_dir + "/__probe__"
        for pattern in rules.exclude_patterns:
            if pattern.endswith("/") and _match_pattern(probe, pattern):
                reason = f"pattern:{pattern}"
                break
    if reason is None:
        return None
    folded = rel_dir.casefold()
    for pattern in rules.include_patterns:
        literal = re.split(r"[*?\[]", pattern.casefold(), maxsplit=1)[0].strip("/")
        if literal.startswith(folded + "/") or literal == folded or folded.startswith(literal + "/") and literal:
            return None
        if _match_pattern(probe, pattern):
            return None
    return reason


def classify(rel_path: str, size: int, rules: OwnerRules) -> tuple[str, str | None]:
    """Return (status, excluded_reason) before hashing; indexable → pending_hash."""
    from .storage_roots import StorageError, relative_parts

    try:
        relative_parts(rel_path)
    except StorageError:
        return "excluded", "unsafe_path_name"
    if not rules.enabled:
        return "excluded", "scope_disabled"
    parts = rel_path.split("/")
    if is_office_lock_file(parts[-1]):
        return "excluded", "office_lock_file"
    included = any(_match_pattern(rel_path, pattern) for pattern in rules.include_patterns)
    if any(part.startswith(".") for part in parts) and not included:
        return "excluded", "hidden"
    if rules.include_roots and not included and not any(
            rel_path == root or rel_path.startswith(root + "/") for root in rules.include_roots):
        return "excluded", "outside_include_roots"
    if not included:
        for pattern in rules.exclude_patterns:
            if _match_pattern(rel_path, pattern):
                return "excluded", f"pattern:{pattern}"[:128]
    fmt = index_format(parts[-1])
    if fmt is None:
        return "unsupported", "unsupported_format"
    if size <= 0:
        return "excluded", "empty_file"
    if size > rules.max_file_bytes:
        return "excluded", "size_limit"
    if fmt == "text" and size > rules.max_text_file_bytes:
        return "excluded", "text_size_limit"
    if fmt in {"audio", "video"} and size > rules.max_media_bytes:
        return "excluded", "media_size_limit"
    return "pending_hash", None


def job_priority(name: str, size: int, rules_or_settings=None) -> int:
    """Files jobs run after other kinds; heavy audio/video after small files."""
    if isinstance(rules_or_settings, OwnerRules):
        defer = rules_or_settings.defer_bytes
    elif isinstance(rules_or_settings, dict):
        defer = int((rules_or_settings.get("heavy_media_policy") or {}).get("defer_bytes", 100 * 1024 * 1024))
    else:
        defer = DEFAULT_FILES_INDEX_SETTINGS["heavy_media_policy"]["defer_bytes"]
    fmt = index_format(name or "")
    if (fmt in {"audio", "video"} or (size or 0) > defer) and (size or 0) > min(defer, 16 * 1024 * 1024):
        return PRIORITY_HEAVY
    return PRIORITY_NORMAL


def guess_mime(name: str) -> str | None:
    lowered = name.lower()
    if lowered.endswith(".msg"):
        return "application/vnd.ms-outlook"
    if lowered.endswith(".eml"):
        return "message/rfc822"
    return mimetypes.guess_type(name)[0]


# ------------------------------------------------------------ root mapping

@dataclass(frozen=True)
class FilesRoot:
    scope: str
    owner_id: uuid.UUID

    @property
    def storage_relative(self) -> str:
        return (f"_users/user_{self.owner_id}" if self.scope == "user"
                else f"_projects/project_{self.owner_id}")

    def path(self, workspace_root: Path) -> Path:
        return Path(workspace_root, *self.storage_relative.split("/"))


def files_location(scope: str, owner_id, rel_path: str) -> dict[str, Any]:
    root = FilesRoot(scope, uuid.UUID(str(owner_id)))
    return {
        "scope": scope,
        "project_id": str(owner_id) if scope == "project" else None,
        "relative_path": rel_path,
        "path": f"{root.storage_relative}/{rel_path}",
    }


def map_workspace_path(path, workspace_root) -> tuple[FilesRoot, str] | None:
    """Map an absolute or workspace-relative path to (root, relative prefix)."""
    try:
        root = Path(os.path.abspath(os.fspath(workspace_root)))
        candidate = Path(os.fspath(path))
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError, TypeError):
        return None
    parts = [part for part in relative.as_posix().split("/") if part and part != "."]
    if len(parts) < 2:
        return None
    namespace = parts[0].casefold()
    pattern = USER_SEGMENT if namespace == "_users" else PROJECT_SEGMENT if namespace == "_projects" else None
    match = pattern.fullmatch(parts[1]) if pattern else None
    if match is None:
        return None
    try:
        owner = uuid.UUID(match.group(1))
    except ValueError:
        return None
    if any(part.casefold() == ".trash" for part in parts[2:]):
        return None
    return FilesRoot("user" if namespace == "_users" else "project", owner), "/".join(parts[2:])


def discover_roots(workspace_root: Path) -> list[FilesRoot]:
    from ..tools.file_explorer.storage_context import _is_storage_link

    roots = []
    for namespace, scope, pattern in (("_users", "user", USER_SEGMENT), ("_projects", "project", PROJECT_SEGMENT)):
        base = Path(workspace_root) / namespace
        try:
            if _is_storage_link(base) or not base.is_dir():
                continue
            entries = list(os.scandir(base))
        except OSError:
            continue
        for entry in entries:
            match = pattern.fullmatch(entry.name)
            if match is None:
                continue
            try:
                if entry.is_symlink() or _is_storage_link(Path(entry.path)) or not entry.is_dir(follow_symlinks=False):
                    continue
                roots.append(FilesRoot(scope, uuid.UUID(match.group(1))))
            except (OSError, ValueError):
                continue
    return sorted(roots, key=lambda root: (root.scope, str(root.owner_id)))


# -------------------------------------------------------------- filesystem

@dataclass
class WalkResult:
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    skipped_directories: Counter = field(default_factory=Counter)
    complete: bool = True
    directory_identity: str | None = None


def _root_directory_identity(root_path: Path) -> str | None:
    """OS filesystem + directory file-id, scoped to the absolute FilesRoot path.

    Filesystems without stable nonzero device/inode ids fail closed. Reparse
    roots are rejected by walk_root; this helper never follows the final link.
    """
    from .storage_roots import StorageError, assert_no_links

    try:
        assert_no_links(root_path)
        info = root_path.lstat()
    except (OSError, StorageError, RuntimeError):
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_dev <= 0 or info.st_ino <= 0
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        return None
    raw = json.dumps([os.path.abspath(root_path), info.st_dev, info.st_ino], ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    info = entry.stat(follow_symlinks=False)
    if getattr(entry, "is_junction", None) and entry.is_junction():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def walk_root(root_path: Path, rules: OwnerRules, prefixes: Iterable[str] | None = None) -> WalkResult | None:
    """Stat regular files without following links; ``None`` when root unusable."""
    from .storage_roots import StorageError, assert_no_links

    try:
        assert_no_links(root_path)
        if not root_path.is_dir():
            return None
        with os.scandir(root_path):
            pass
    except (OSError, StorageError, RuntimeError):
        return None
    identity = _root_directory_identity(root_path)
    result = WalkResult(directory_identity=identity)

    def visit_file(rel, entry_stat):
        if stat.S_ISREG(entry_stat.st_mode):
            result.files[rel] = (int(entry_stat.st_size), int(entry_stat.st_mtime_ns))

    def walk(directory: Path, rel_dir: str):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            result.complete = False
            return
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.name.casefold() == ".trash" and not rel_dir:
                continue
            try:
                if _entry_is_link(entry):
                    result.skipped_directories["link"] += 1
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                result.complete = False
                continue
            if stat.S_ISDIR(info.st_mode):
                reason = _directory_excluded(rel, rules)
                if reason is not None:
                    result.skipped_directories[reason] += 1
                    continue
                walk(Path(entry.path), rel)
            else:
                visit_file(rel, info)

    targets = sorted(set(prefixes)) if prefixes is not None else [""]
    for prefix in targets:
        if not prefix:
            walk(root_path, "")
            continue
        target = root_path.joinpath(*prefix.split("/"))
        parents = prefix.split("/")[:-1]
        # A hinted path must not be reached through a link or an excluded dir.
        blocked = False
        for index in range(len(parents)):
            current = root_path.joinpath(*parents[:index + 1])
            try:
                info = current.lstat()
            except FileNotFoundError:
                blocked = True
                break
            except OSError:
                result.complete = False
                blocked = True
                break
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or _directory_excluded(
                        "/".join(parents[:index + 1]), rules):
                blocked = True
                break
        if blocked:
            continue
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            result.complete = False
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            continue
        if stat.S_ISDIR(info.st_mode):
            if _directory_excluded(prefix, rules) is None:
                walk(target, prefix)
        else:
            visit_file(prefix, info)
    if identity is None or _root_directory_identity(root_path) != identity:
        result.directory_identity = None
        result.complete = False
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat_regular(path: Path):
    from .storage_roots import StorageError, assert_no_links

    try:
        assert_no_links(path)
        info = path.stat()
    except (OSError, StorageError):
        return None
    return info if stat.S_ISREG(info.st_mode) else None


# ------------------------------------------------------------------- hints

_hint_lock = threading.Lock()
_pending_hints: dict[FilesRoot, set[str] | None] = {}
_active_root: Path | None = None


def hint_workspace_paths(paths: Iterable[Any], *, workspace_root=None) -> None:
    """Cheap dirty notice from AoiTalk writers; never raises to the caller."""
    try:
        with _hint_lock:
            root = workspace_root or _active_root
        if root is None:
            return
        mapped = [value for value in (map_workspace_path(path, root) for path in paths if path) if value]
        if not mapped:
            return
        with _hint_lock:
            for files_root, prefix in mapped:
                current = _pending_hints.get(files_root, set())
                if current is None or not prefix:
                    _pending_hints[files_root] = None
                else:
                    current.add(prefix)
                    _pending_hints[files_root] = current
    except Exception:
        log.debug("Files index hint ignored", exc_info=True)


def _drain_hints() -> dict[FilesRoot, set[str] | None]:
    with _hint_lock:
        drained = dict(_pending_hints)
        _pending_hints.clear()
    return drained


def _requeue_hints(hints: dict[FilesRoot, set[str] | None]) -> None:
    with _hint_lock:
        for root, prefixes in hints.items():
            current = _pending_hints.get(root, set())
            if current is None or prefixes is None:
                _pending_hints[root] = None
            else:
                _pending_hints[root] = current | prefixes


# ---------------------------------------------------------------- scanner

@dataclass
class ScanStats:
    scanned_at: float = 0.0
    files_seen: int = 0
    inserted: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    rehash: int = 0
    skipped_directories: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    root_available: bool = True


def _under_prefixes(rel_path: str, prefixes) -> bool:
    if prefixes is None:
        return True
    return any(not prefix or rel_path == prefix or rel_path.startswith(prefix + "/") for prefix in prefixes)


class WorkspaceFileScanner:
    """Inventory writer.  ``notifier(actor, source_id, priority)`` queues jobs."""

    def __init__(self, session_factory, *, workspace_root, settings_provider=None, notifier=None,
                 clock=datetime.utcnow):
        self.session_factory = session_factory
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        self.settings_provider = settings_provider or load_files_index_settings
        self.notifier = notifier or _default_notifier
        self.clock = clock
        self.rehash: dict[uuid.UUID, FilesRoot] = {}
        self.stats: dict[FilesRoot, ScanStats] = {}

    async def _settings(self):
        return await asyncio.to_thread(self.settings_provider)

    async def _publish_notices(self, notices):
        """After commit, enqueue bounded pages without blocking the event loop."""
        from .media_index_worker import MAX_ENQUEUE_BATCH, notify_media_sources

        for start in range(0, len(notices), MAX_ENQUEUE_BATCH):
            page = [notice for notice in notices[start:start + MAX_ENQUEUE_BATCH] if notice[0]]
            if not page:
                continue
            if self.notifier is _default_notifier:
                await asyncio.to_thread(notify_media_sources, [
                    (actor, FILES_KIND, str(source), priority) for actor, source, priority in page])
            else:
                for notice in page:
                    self.notifier(*notice)
            await asyncio.sleep(0)

    async def _actors(self, session, roots) -> dict[FilesRoot, str | None]:
        """Indexing actor: the user, or the Project owner (live, not deleted)."""
        from sqlalchemy import select
        from ..memory.models import Project, User

        actors: dict[FilesRoot, str | None] = {}
        project_ids = [root.owner_id for root in roots if root.scope == "project"]
        user_ids = [root.owner_id for root in roots if root.scope == "user"]
        projects = {}
        if project_ids:
            for row in await session.execute(select(Project.id, Project.owner_id, Project.deleted_at)
                                             .where(Project.id.in_(project_ids))):
                projects[row.id] = row
        users = set()
        if user_ids:
            users = set((await session.execute(select(User.id).where(User.id.in_(user_ids),
                                                                     User.is_active.is_(True)))).scalars())
        for root in roots:
            if root.scope == "project":
                project = projects.get(root.owner_id)
                # A deleted Project keeps its owner as the withdrawal actor.
                actors[root] = (str(project.owner_id) if project is not None and project.owner_id is not None
                                else None)
            else:
                actors[root] = str(root.owner_id) if root.owner_id in users else None
        return actors

    async def project_gone(self, session, root: FilesRoot) -> bool:
        from ..memory.models import Project
        if root.scope != "project":
            return False
        project = await session.get(Project, root.owner_id, populate_existing=True)
        return project is None or project.deleted_at is not None

    async def _observe_root(self, session, root, walked, rules, prefixes, existing, now, interval):
        """Persist the proof needed to treat a full empty root as deletion.

        The elapsed confirmation interval is in addition to matching a previous
        healthy directory and scope; observing two arbitrary empty roots is not
        evidence. Errors and partial scans break the empty confirmation chain.
        """
        from sqlalchemy import select
        from ..memory.models import WorkspaceFileRootObservation as O

        observation = (await session.execute(select(O).where(
            O.scope == root.scope, O.owner_id == root.owner_id).with_for_update())).scalar_one_or_none()
        fingerprint = hashlib.sha256(json.dumps(asdict(rules), sort_keys=True).encode()).hexdigest()

        def reset_empty():
            if observation is not None:
                observation.empty_first_seen_at = None
                observation.empty_last_seen_at = None
                observation.empty_observations = 0

        usable = (walked is not None and walked.complete and walked.directory_identity is not None
                  and rules.enabled)
        matches = (usable and observation is not None
                   and observation.directory_identity == walked.directory_identity
                   and observation.rules_fingerprint == fingerprint)
        if not usable or prefixes is not None:
            reset_empty()
            return bool(matches and prefixes is not None and walked.files), observation
        if walked.files:
            # A replacement containing a single new file must not bless all
            # missing paths from the former root. Account for every live row.
            accounted = all(row.rel_path in walked.files for row in existing)
            if accounted:
                if observation is None:
                    observation = O(scope=root.scope, owner_id=root.owner_id)
                    session.add(observation)
                observation.directory_identity = walked.directory_identity
                observation.rules_fingerprint = fingerprint
                observation.healthy_at = now
            reset_empty()
            return bool(matches or accounted), observation
        if not matches:
            reset_empty()
            return False, observation
        if observation.empty_first_seen_at is None or now < observation.empty_last_seen_at:
            observation.empty_first_seen_at = now
            observation.empty_observations = 0
        observation.empty_last_seen_at = now
        observation.empty_observations += 1
        confirmed = (observation.empty_observations >= 2
                     and now - observation.empty_first_seen_at >= timedelta(seconds=max(60, interval)))
        return confirmed, observation

    async def scan_root(self, root: FilesRoot, prefixes: set[str] | None = None) -> ScanStats:
        """Phase A: stat/classify; new/changed indexable files become pending_hash."""
        from sqlalchemy import select, update
        from ..memory.models import WorkspaceFileEntry as E

        settings = await self._settings()
        rules = owner_rules(settings, root.scope, root.owner_id)
        now = self.clock()
        stats = ScanStats(scanned_at=time.time())
        notices = []
        session = await self.session_factory()
        try:
            actor = (await self._actors(session, [root])).get(root)
            gone = await self.project_gone(session, root)
            existing = [row for row in (await session.execute(select(
                E.id, E.rel_path, E.size_bytes, E.mtime_ns, E.content_hash, E.status, E.excluded_reason,
                E.name).where(E.scope == root.scope, E.owner_id == root.owner_id, E.deleted_at.is_(None)))).all()
                if _under_prefixes(row.rel_path, prefixes)]
            if gone:
                for row in existing:
                    await session.execute(update(E).where(E.id == row.id).values(
                        status="deleted", excluded_reason="project_deleted", deleted_at=now, last_scanned_at=now))
                    stats.deleted += 1
                    if row.status == "indexable":
                        notices.append((actor, row.id, PRIORITY_CLEANUP))
                await session.commit()
                await self._publish_notices(notices)
                self.stats[root] = stats
                return stats
            walked = await asyncio.to_thread(walk_root, root.path(self.workspace_root), rules, prefixes)
            allow_missing, observation = await self._observe_root(
                session, root, walked, rules, prefixes, existing, now, settings["scan_interval_seconds"])
            if walked is None:
                # Root outage/unmount is not evidence of deletion. Persist the
                # invalidated confirmation so a restart cannot reuse it.
                await session.commit()
                stats.root_available = False
                stats.complete = False
                self.stats[root] = stats
                return stats
            stats.files_seen = len(walked.files)
            stats.skipped_directories = dict(walked.skipped_directories)
            by_path = {row.rel_path: row for row in existing}
            # Recheck after the observation DB await as well. A swap here must
            # invalidate the durable chain, not merely suppress this deletion.
            if (walked.directory_identity is None
                    or _root_directory_identity(root.path(self.workspace_root)) != walked.directory_identity):
                walked.complete = False
                allow_missing = False
                if observation is not None:
                    observation.empty_first_seen_at = None
                    observation.empty_last_seen_at = None
                    observation.empty_observations = 0
            stats.complete = walked.complete
            new_files = []
            for rel_path, (size, mtime_ns) in walked.files.items():
                row = by_path.pop(rel_path, None)
                status, reason = classify(rel_path, size, rules)
                name = rel_path.rsplit("/", 1)[-1]
                if row is None:
                    new_files.append((rel_path, size, mtime_ns, status, reason, name))
                    continue
                changed = (row.size_bytes, row.mtime_ns) != (size, mtime_ns)
                values = {}
                if status == "pending_hash":
                    if row.status == "indexable":
                        if changed:
                            # Keep serving the verified hash until the rehash
                            # proves a content change (resolve rejects drift).
                            self.rehash[row.id] = root
                            stats.rehash += 1
                    elif row.status == "pending_hash":
                        if changed:
                            values.update(size_bytes=size, mtime_ns=mtime_ns)
                    elif row.content_hash and not changed:
                        values.update(status="indexable", excluded_reason=None)
                    else:
                        values.update(status="pending_hash", excluded_reason=None, size_bytes=size,
                                      mtime_ns=mtime_ns)
                else:
                    if (row.status, row.excluded_reason) != (status, reason):
                        values.update(status=status, excluded_reason=reason)
                        if row.status == "indexable":
                            notices.append((actor, row.id, PRIORITY_NORMAL))
                    if changed:
                        values.update(size_bytes=size, mtime_ns=mtime_ns)
                if values:
                    values.update(last_seen_at=now, last_scanned_at=now)
                    await session.execute(update(E).where(E.id == row.id).values(**values))
                    stats.updated += 1
            # Moves/renames seen in this pass: a vanished indexed row with the
            # same size and sha256 keeps its id (no withdraw/re-embed race).
            vanished = {} if not allow_missing or not walked.complete else {
                row.id: row for row in by_path.values() if row.status == "indexable" and row.content_hash}
            root_path = root.path(self.workspace_root)
            for rel_path, size, mtime_ns, status, reason, name in new_files:
                previous = None
                if status == "pending_hash":
                    candidates = [row for row in vanished.values() if int(row.size_bytes or 0) == size]
                    if candidates:
                        try:
                            digest = await asyncio.to_thread(sha256_file, root_path.joinpath(*rel_path.split("/")))
                        except OSError:
                            digest = None
                        matches = [row for row in candidates if row.content_hash == digest]
                        matches.sort(key=lambda row: row.name != name)
                        previous = matches[0] if matches else None
                if previous is not None:
                    vanished.pop(previous.id)
                    by_path.pop(previous.rel_path, None)
                    await session.execute(update(E).where(E.id == previous.id).values(
                        rel_path=rel_path, name=name[:512], ext=PurePosixPath(name.lower()).suffix[:64],
                        mime=guess_mime(name), size_bytes=size, mtime_ns=mtime_ns, last_seen_at=now,
                        last_scanned_at=now))
                    notices.append((actor, previous.id, job_priority(name, size, rules)))
                    stats.moved += 1
                    continue
                session.add(E(id=uuid.uuid4(), scope=root.scope, owner_id=root.owner_id, rel_path=rel_path,
                              name=name[:512], ext=PurePosixPath(name.lower()).suffix[:64],
                              mime=guess_mime(name), size_bytes=size, mtime_ns=mtime_ns,
                              status=status, excluded_reason=reason, first_seen_at=now,
                              last_seen_at=now, last_scanned_at=now))
                stats.inserted += 1
            if allow_missing and walked.complete:
                for row in by_path.values():
                    await session.execute(update(E).where(E.id == row.id).values(
                        status="deleted", deleted_at=now, last_scanned_at=now))
                    stats.deleted += 1
                    if row.status == "indexable":
                        notices.append((actor, row.id, PRIORITY_CLEANUP))
            touched = update(E).where(E.scope == root.scope, E.owner_id == root.owner_id,
                                      E.deleted_at.is_(None))
            if prefixes is None:
                await session.execute(touched.values(last_scanned_at=now))
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
        await self._publish_notices(notices)
        self.stats[root] = stats
        return stats

    async def hash_pending(self, *, max_files=HASH_BATCH_FILES, max_bytes=HASH_BATCH_BYTES,
                           max_seconds=HASH_BATCH_SECONDS) -> int:
        """Phase B: hash pending/changed rows (small first), detect moves, publish."""
        from sqlalchemy import select, update, delete
        from ..memory.models import WorkspaceFileEntry as E

        settings = await self._settings()
        started = time.monotonic()
        processed = 0
        budget = 0
        session = await self.session_factory()
        notices = []
        try:
            rehash_ids = list(self.rehash)[:max_files]
            rows = []
            if rehash_ids:
                rows.extend((await session.execute(select(E).where(E.id.in_(rehash_ids)))).scalars())
                for identifier in rehash_ids:
                    self.rehash.pop(identifier, None)
            remaining = max_files - len(rows)
            if remaining > 0:
                rows.extend((await session.execute(select(E).where(
                    E.status == "pending_hash", E.deleted_at.is_(None)).order_by(
                        E.size_bytes, E.id).limit(remaining))).scalars())
            actors = await self._actors(session, {FilesRoot(row.scope, row.owner_id) for row in rows})
            for row in rows:
                if row.deleted_at is not None:
                    continue
                if processed and (budget >= max_bytes or time.monotonic() - started > max_seconds):
                    if row.status == "indexable":
                        self.rehash[row.id] = FilesRoot(row.scope, row.owner_id)
                    continue
                root = FilesRoot(row.scope, row.owner_id)
                rules = owner_rules(settings, row.scope, row.owner_id)
                actor = actors.get(root)
                path = root.path(self.workspace_root).joinpath(*row.rel_path.split("/"))
                info = await asyncio.to_thread(_stat_regular, path)
                now = self.clock()
                if info is None:
                    # Vanished between phases: a later full scan confirms.
                    continue
                status, reason = classify(row.rel_path, info.st_size, rules)
                if status != "pending_hash":
                    await session.execute(update(E).where(E.id == row.id).values(
                        status=status, excluded_reason=reason, size_bytes=info.st_size,
                        mtime_ns=info.st_mtime_ns, last_scanned_at=now))
                    if row.status == "indexable":
                        notices.append((actor, row.id, PRIORITY_NORMAL))
                    processed += 1
                    continue
                try:
                    digest = await asyncio.to_thread(sha256_file, path)
                except OSError:
                    continue
                after = await asyncio.to_thread(_stat_regular, path)
                if after is None or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    continue  # still being written; next pass retries
                processed += 1
                budget += info.st_size
                priority = job_priority(row.name, info.st_size, rules)
                if row.status == "indexable":
                    values = dict(size_bytes=info.st_size, mtime_ns=info.st_mtime_ns, last_scanned_at=now,
                                  last_seen_at=now)
                    if digest != row.content_hash:
                        values["content_hash"] = digest
                        notices.append((actor, row.id, priority))
                    await session.execute(update(E).where(E.id == row.id).values(**values))
                    continue
                previous = None
                if row.first_seen_at is not None:
                    candidates = (await session.execute(select(E).where(
                        E.scope == row.scope, E.owner_id == row.owner_id, E.content_hash == digest,
                        E.status == "deleted", E.deleted_at.is_not(None),
                        E.deleted_at >= now - MOVE_MATCH_WINDOW, E.id != row.id))).scalars().all()
                    if candidates:
                        candidates.sort(key=lambda item: (item.name != row.name, -(item.deleted_at.timestamp())))
                        previous = candidates[0]
                if previous is not None:
                    # Move/rename: keep the indexed identity, drop the new row.
                    target = dict(rel_path=row.rel_path, name=row.name, ext=row.ext, mime=row.mime,
                                  size_bytes=info.st_size, mtime_ns=info.st_mtime_ns, content_hash=digest,
                                  status="indexable", excluded_reason=None, deleted_at=None,
                                  last_seen_at=now, last_scanned_at=now)
                    await session.execute(delete(E).where(E.id == row.id))
                    await session.flush()
                    await session.execute(update(E).where(E.id == previous.id).values(**target))
                    notices.append((actor, previous.id, priority))
                    self.stats.setdefault(root, ScanStats()).moved += 1
                else:
                    await session.execute(update(E).where(E.id == row.id).values(
                        content_hash=digest, status="indexable", excluded_reason=None,
                        size_bytes=info.st_size, mtime_ns=info.st_mtime_ns, last_seen_at=now,
                        last_scanned_at=now))
                    notices.append((actor, row.id, priority))
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
        await self._publish_notices(notices)
        return processed

    async def disable_roots(self, roots: Iterable[FilesRoot], reason="scope_disabled") -> int:
        """Withdraw rows of a scope/owner that settings no longer index."""
        from sqlalchemy import select, update
        from ..memory.models import WorkspaceFileEntry as E

        roots = list(roots)
        if not roots:
            return 0
        session = await self.session_factory()
        changed = 0
        notices = []
        try:
            actors = await self._actors(session, roots)
            now = self.clock()
            for root in roots:
                rows = (await session.execute(select(E.id, E.status).where(
                    E.scope == root.scope, E.owner_id == root.owner_id, E.deleted_at.is_(None),
                    E.status.in_(["indexable", "pending_hash"])))).all()
                for row in rows:
                    await session.execute(update(E).where(E.id == row.id).values(
                        status="excluded", excluded_reason=reason, last_scanned_at=now))
                    if row.status == "indexable":
                        notices.append((actors.get(root), row.id, PRIORITY_NORMAL))
                    changed += 1
            await session.commit()
        finally:
            await session.close()
        await self._publish_notices(notices)
        return changed

def _default_notifier(actor, source_id, priority=PRIORITY_NORMAL):
    from .media_index_worker import notify_media_source

    notify_media_source(actor, FILES_KIND, str(source_id), priority=priority)


class WorkspaceFileIndexService:
    """Background loop: hints first, due full scans, then bounded hashing."""

    def __init__(self, session_factory, *, workspace_root, settings_provider=None, notifier=None,
                 poll_seconds=2.0):
        self.scanner = WorkspaceFileScanner(session_factory, workspace_root=workspace_root,
                                            settings_provider=settings_provider, notifier=notifier)
        self.poll_seconds = poll_seconds
        self.task = None
        self.scanning = False
        self._next_full: dict[FilesRoot, float] = {}
        self.last_error: str | None = None

    @property
    def workspace_root(self) -> Path:
        return self.scanner.workspace_root

    async def start(self):
        global _active_root
        with _hint_lock:
            _active_root = self.workspace_root
        _set_active_service(self)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="workspace-files-index-scan")

    async def stop(self):
        global _active_root
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        _set_active_service(None)
        with _hint_lock:
            _active_root = None

    def request_rescan(self, roots: Iterable[FilesRoot]):
        for root in roots:
            self._next_full[root] = 0.0

    async def run(self):
        while True:
            try:
                busy = await self.run_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning("Files index scan iteration failed", exc_info=True)
                busy = False
            if not busy:
                await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> bool:
        # The daemon task remains alive while idle. Expose only the active
        # iteration to clients, and clear it on errors and cancellation too.
        self.scanning = True
        try:
            return await self._run_once()
        finally:
            self.scanning = False

    async def _run_once(self) -> bool:
        settings = await asyncio.to_thread(self.scanner.settings_provider)
        if not settings.get("enabled"):
            _drain_hints()
            return False
        busy = False
        hints = _drain_hints()
        for root, prefixes in hints.items():
            try:
                await self.scanner.scan_root(root, prefixes)
                busy = True
            except Exception:
                _requeue_hints({root: prefixes})
                raise
        now = time.time()
        roots = await asyncio.to_thread(discover_roots, self.workspace_root)
        disabled = [root for root in roots if not owner_rules(settings, root.scope, root.owner_id).enabled]
        enabled_roots = [root for root in roots if root not in disabled]
        for root in enabled_roots:
            if self._next_full.get(root, 0.0) <= now:
                self._next_full[root] = now + int(settings["scan_interval_seconds"])
                await self.scanner.scan_root(root)
                busy = True
                break
        for root in disabled:
            if self._next_full.get(root, 0.0) <= now:
                self._next_full[root] = now + int(settings["scan_interval_seconds"])
                await self.scanner.disable_roots([root])
        if await self.scanner.hash_pending():
            busy = True
        # Preserve canonical tombstones for retry/reconciliation. Age alone or
        # a current-identity cleanup checkpoint cannot prove all old collections
        # are empty, so automatic canonical purging must not discard the locator.
        return busy


_active_service: WorkspaceFileIndexService | None = None


def _set_active_service(service):
    global _active_service
    _active_service = service


def get_active_files_index_service() -> WorkspaceFileIndexService | None:
    return _active_service


# ------------------------------------------------------------- visibility

async def visible_roots(session, user_id, *, project_id=None, scope=None, include_user=True,
                        space_id=None) -> tuple[list[FilesRoot], dict[uuid.UUID, str]]:
    """Roots the actor may search: own User Files and readable Projects only.

    Admins see readable Projects through the canonical Project policy but
    never another user's User Files.  Returns (roots, project names).
    """
    from sqlalchemy import select
    from ..memory.models import Project
    from .search_sources import SearchScope, SearchSourceResolver, SourceUnavailable

    resolver = SearchSourceResolver(session, SearchScope(uuid.UUID(str(user_id)), space_id=space_id))
    actor = await resolver._actor()
    roots: list[FilesRoot] = []
    names: dict[uuid.UUID, str] = {}
    if include_user and scope in {None, "user"} and project_id is None:
        roots.append(FilesRoot("user", actor.id))
    if scope in {None, "project"}:
        if project_id is not None:
            ids = [uuid.UUID(str(project_id))]
        else:
            ids = list((await session.execute(select(Project.id).where(Project.deleted_at.is_(None)))).scalars())
        policies = await resolver._acl_projects(ids, actor)
        readable = [pid for pid, policy in policies.items() if policy.read and policy.scoped_read
                    and (space_id is None or policy.space_id == space_id)]
        if project_id is not None and not readable:
            raise SourceUnavailable()
        if readable:
            for row in await session.execute(select(Project.id, Project.name).where(Project.id.in_(readable))):
                names[row.id] = row.name
        roots.extend(FilesRoot("project", pid) for pid in sorted(readable, key=str))
    return roots, names


def _ledger_states(ledger_path) -> dict[str, tuple[str, str | None, str | None, str | None]]:
    import sqlite3
    from ..rag.embedding_identity import EMBEDDING_FINGERPRINT

    path = Path(ledger_path)
    if not path.is_file():
        return {}
    with sqlite3.connect(path, timeout=30) as db:
        try:
            return {source_id: (status, verified_hash, updated_at, error) for source_id, status, verified_hash, updated_at, error in
                    db.execute("SELECT source_id,status,verified_hash,updated_at,error FROM embedding_sources "
                               "WHERE kind=? AND identity=?", (FILES_KIND, EMBEDDING_FINGERPRINT))}
        except sqlite3.OperationalError:
            return {}


def _queue_states(queue_path) -> dict[str, tuple[str, str | None]]:
    import sqlite3

    path = Path(queue_path)
    if not path.is_file():
        return {}
    states: dict[str, tuple[str, str | None]] = {}
    rank = {"running": 0, "pending": 1, "failed": 2, "done": 3}
    with sqlite3.connect(path, timeout=30) as db:
        try:
            for source_id, state, error in db.execute(
                    "SELECT source_id,state,error FROM media_jobs WHERE kind=?", (FILES_KIND,)):
                current = states.get(source_id)
                if current is None or rank.get(state, 9) < rank.get(current[0], 9):
                    states[source_id] = (state, error)
        except sqlite3.OperationalError:
            return {}
    return states


def index_state(entry_status, content_hash, ledger, job) -> str:
    """Per-entry index state used by status/entries APIs."""
    if entry_status != "indexable":
        return entry_status
    job_state = job[0] if job else None
    if job_state == "running":
        return "running"
    if job_state == "pending":
        return "pending"
    if ledger and ledger[0] == "ready" and ledger[1] == content_hash:
        return "ready"
    if ledger and ledger[0] == "excluded" and ledger[1] and ledger[1] == content_hash:
        return "excluded"
    if job_state == "failed" or (ledger and ledger[0] == "failed"):
        return "failed"
    if ledger and ledger[0] == "running":
        return "running"
    return "pending"


async def files_index_status(session, user_id, *, project_id=None, scope=None, include_user=True, space_id=None,
                             ledger_path="data/search/embeddinggemma2.sqlite",
                             queue_path="data/search/media-index-queue.sqlite", entries_filter=None,
                             offset=0, limit=0) -> dict[str, Any]:
    """Coverage/status for roots visible to ``user_id`` (no other owners)."""
    from sqlalchemy import select
    from ..memory.models import WorkspaceFileEntry as E

    settings = await asyncio.to_thread(load_files_index_settings)
    roots, names = await visible_roots(session, user_id, project_id=project_id, scope=scope,
                                       include_user=include_user, space_id=space_id)
    ledger, jobs = await asyncio.gather(asyncio.to_thread(_ledger_states, ledger_path),
                                        asyncio.to_thread(_queue_states, queue_path))
    totals = Counter()
    reasons = Counter()
    per_root = []
    entries = []
    last_scan = None
    last_indexed = None
    user_roots = [root.owner_id for root in roots if root.scope == "user"]
    project_roots = [root.owner_id for root in roots if root.scope == "project"]
    rows = []
    for scope_name, owners in (("user", user_roots), ("project", project_roots)):
        for start in range(0, len(owners), 500):
            batch = owners[start:start + 500]
            rows.extend((await session.execute(select(
                E.id, E.scope, E.owner_id, E.rel_path, E.name, E.size_bytes, E.status, E.excluded_reason,
                E.content_hash, E.last_scanned_at).where(E.scope == scope_name, E.owner_id.in_(batch),
                                                        E.deleted_at.is_(None)))).all())
    grouped: dict[FilesRoot, Counter] = {root: Counter() for root in roots}
    for row in rows:
        key = str(row.id)
        state = index_state(row.status, row.content_hash, ledger.get(key), jobs.get(key))
        root = FilesRoot(row.scope, row.owner_id)
        counter = grouped.setdefault(root, Counter())
        counter["total"] += 1
        counter[state] += 1
        if row.status == "indexable":
            counter["indexable"] += 1
        totals["total"] += 1
        totals[state] += 1
        if row.status == "indexable":
            totals["indexable"] += 1
        if row.status in {"excluded", "unsupported"} and row.excluded_reason:
            reasons[row.excluded_reason] += 1
        elif state == "excluded" and row.status == "indexable":
            reasons[(ledger.get(key) or (None,) * 4)[3] or "index_excluded"] += 1
        if row.last_scanned_at and (last_scan is None or row.last_scanned_at > last_scan):
            last_scan = row.last_scanned_at
        if state == "ready" and ledger.get(key) and ledger[key][2]:
            value = str(ledger[key][2])
            last_indexed = value if last_indexed is None or value > last_indexed else last_indexed
        if entries_filter is not None and state in entries_filter:
            entries.append({
                "id": key, "scope": row.scope,
                "project_id": str(row.owner_id) if row.scope == "project" else None,
                "rel_path": row.rel_path, "name": row.name, "size_bytes": int(row.size_bytes or 0),
                "status": row.status, "index_state": state,
                "excluded_reason": row.excluded_reason or (
                    (ledger.get(key) or (None,) * 4)[3] if state == "excluded" else None),
                "error": (jobs.get(key) or (None, None))[1] or ((ledger.get(key) or (None, None, None))[0] == "failed"
                                                                and "index_failed" or None),
                "last_scanned_at": row.last_scanned_at.isoformat() if row.last_scanned_at else None,
                "files_location": files_location(row.scope, row.owner_id, row.rel_path),
            })
    service = get_active_files_index_service()
    for root in roots:
        counter = grouped.get(root, Counter())
        stats = service.scanner.stats.get(root) if service else None
        rules = owner_rules(settings, root.scope, root.owner_id)
        per_root.append({
            "scope": root.scope,
            "project_id": str(root.owner_id) if root.scope == "project" else None,
            "project_name": names.get(root.owner_id) if root.scope == "project" else None,
            "enabled": rules.enabled,
            "counts": _counts(counter),
            "skipped_directories": dict(stats.skipped_directories) if stats else {},
            "root_available": stats.root_available if stats else None,
            "last_full_scan_at": datetime.utcfromtimestamp(stats.scanned_at).isoformat() if stats and
            stats.scanned_at else None,
        })
    pending_jobs = sum(1 for row in rows if row.status == "indexable"
                       and (jobs.get(str(row.id)) or ("",))[0] == "pending")
    result = {
        "enabled": bool(settings.get("enabled")),
        "semantic_available": _semantic_available(),
        "counts": _counts(totals),
        "excluded_reasons": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
        "queue_waiting": pending_jobs,
        "last_scan_at": last_scan.isoformat() if last_scan else None,
        "last_indexed_at": last_indexed,
        "scanner_running": bool(service and service.scanning),
        "scanner_error": service.last_error if service else None,
        "roots": per_root,
    }
    if entries_filter is not None:
        entries.sort(key=lambda item: (item["scope"], item["project_id"] or "", item["rel_path"]))
        result["entries_total"] = len(entries)
        result["entries"] = entries[offset:offset + limit] if limit else entries[offset:]
    return result


def _counts(counter: Counter) -> dict[str, int]:
    return {key: int(counter.get(key, 0)) for key in (
        "total", "indexable", "ready", "pending", "running", "failed", "pending_hash", "excluded", "unsupported")}


def _semantic_available() -> bool:
    try:
        from ..rag.embedding import get_embedding
        from ..rag.config import get_rag_config

        embedding = get_embedding(get_rag_config().embedding)
        return not bool(getattr(embedding, "last_error", None))
    except Exception:
        return False


def coverage_summary(status: dict[str, Any]) -> dict[str, Any]:
    """Compact model-facing coverage of the searched Files scope."""
    counts = status.get("counts") or {}
    indexable = int(counts.get("indexable", 0))
    ready = int(counts.get("ready", 0))
    waiting = int(counts.get("pending", 0)) + int(counts.get("running", 0)) + int(counts.get("pending_hash", 0))
    return {
        "enabled": bool(status.get("enabled")),
        "total_files": int(counts.get("total", 0)),
        "indexable": indexable + int(counts.get("pending_hash", 0)),
        "ready": ready,
        "pending": waiting,
        "failed": int(counts.get("failed", 0)),
        "excluded": int(counts.get("excluded", 0)) + int(counts.get("unsupported", 0)),
        "complete": bool(status.get("enabled")) and waiting == 0 and int(counts.get("failed", 0)) == 0,
        "last_scan_at": status.get("last_scan_at"),
    }
