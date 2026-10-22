"""
File System utilities for AoiTalk

Provides file system operations:
- List directory contents
- Search files by name or content
- Get file information

Based on Open Interpreter's files.py patterns.
"""

import fnmatch
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --- content search limits ---------------------------------------------------
# A result page ends a single tool call; it never ends the user's task.  The
# caller continues with ``next_offset``.  All limits bound memory/output of one
# call: rg stdout is consumed incrementally and never captured whole.
SEARCH_OUTPUT_MODES = ("content", "files", "count")
_SEARCH_MAX_RESULTS = 500
_SEARCH_MAX_COLUMNS = 300
_SEARCH_MAX_CONTEXT = 5
_SEARCH_DEFAULT_PER_FILE = 20
_SEARCH_MAX_PER_FILE = 200
_SEARCH_STDOUT_BYTES = 8 * 1024 * 1024
_SEARCH_READ_LINE_CAP = 64 * 1024
_SEARCH_OUTPUT_CHARS = 60_000
_SEARCH_TIME_BUDGET_SECONDS = 120.0
_SEARCH_ARGV_BUDGET = 24_000
_SEARCH_PY_CHUNK = 1024 * 1024
_SEARCH_BINARY_PROBE = 8192
_SEARCH_STDERR_TAIL = 4096


def find_ripgrep() -> Optional[str]:
    """Return a native ripgrep executable, or None (explicit Python fallback).

    The AoiTalk service PATH may differ from an interactive shell, so the
    per-user WinGet link directory is probed as well.  WSL is never required.
    """

    candidates: List[str] = []
    override = os.environ.get("AOITALK_RG_PATH", "").strip()
    if override:
        candidates.append(override)
    found = shutil.which("rg")
    if found:
        candidates.append(found)
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(
            os.path.join(local_appdata, "Microsoft", "WinGet", "Links", "rg.exe")
        )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _current_cancel_handle() -> Any:
    try:
        from ...llm.generation_cancellation import get_current_generation_cancellation
    except ImportError:  # pragma: no cover - stripped builds
        return None
    return get_current_generation_cancellation()


def _run_scope_active() -> bool:
    try:
        from ...security.agent_run_scope import get_current_run_scope
    except ImportError:  # pragma: no cover
        return False
    return get_current_run_scope() is not None


def _is_junction(path: str) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is None:  # pragma: no cover - Python < 3.12
        return False
    try:
        return bool(checker(path))
    except OSError:
        return False


class _SearchStop(Exception):
    """Internal: ends the current search call (page full, budget, cancel)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _SearchControl:
    """Run-scoped cancellation + time budget for one search call.

    Reuses the active generation cancellation handle (no second global
    mechanism).  Only processes started by this call are ever terminated, via
    their own Popen handles.
    """

    def __init__(self, time_budget: float):
        self.handle = _current_cancel_handle()
        self.deadline = time.monotonic() + time_budget
        self.stop_reason: Optional[str] = None
        self._lock = threading.Lock()
        self._procs: List["_OwnedProcess"] = []
        self._remove_callback: Optional[Callable[[], None]] = None
        self._timer = threading.Timer(time_budget, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        if self.handle is not None:
            try:
                self._remove_callback = self.handle.add_cancel_callback(self._on_cancel)
            except Exception:  # pragma: no cover - defensive
                self._remove_callback = None

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            procs = list(self._procs)
        for owned in procs:
            owned.kill()

    def _on_cancel(self) -> None:
        self._stop("interrupted")

    def _on_timeout(self) -> None:
        self._stop("time_budget")

    def cancelled(self) -> bool:
        handle = self.handle
        return bool(handle is not None and handle.cancel_requested.is_set())

    def check(self) -> None:
        if self.stop_reason is None:
            if self.cancelled():
                self._stop("interrupted")
            elif time.monotonic() >= self.deadline:
                self._stop("time_budget")
        if self.stop_reason is not None:
            raise _SearchStop(self.stop_reason)

    def attach(self, owned: "_OwnedProcess") -> None:
        with self._lock:
            self._procs.append(owned)
            stopped = self.stop_reason is not None
        if stopped:
            owned.kill()

    def detach(self, owned: "_OwnedProcess") -> None:
        with self._lock:
            self._procs = [item for item in self._procs if item is not owned]

    def close(self) -> None:
        self._timer.cancel()
        if self._remove_callback is not None:
            try:
                self._remove_callback()
            except Exception:  # pragma: no cover
                pass
        with self._lock:
            procs = list(self._procs)
        for owned in procs:
            owned.close()


class _OwnedProcess:
    """A child process owned by one search call; stderr drained concurrently."""

    def __init__(self, argv: List[str], cwd: str, control: _SearchControl):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.control = control
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
        )
        self.killed = False
        self._stderr = bytearray()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._closed = False
        control.attach(self)

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self._stderr.extend(chunk)
                if len(self._stderr) > _SEARCH_STDERR_TAIL:
                    del self._stderr[: len(self._stderr) - _SEARCH_STDERR_TAIL]
        except (OSError, ValueError):
            pass

    def readline(self) -> bytes:
        stream = self.proc.stdout
        if stream is None:
            return b""
        try:
            return stream.readline(_SEARCH_READ_LINE_CAP)
        except (OSError, ValueError):
            return b""

    def kill(self) -> None:
        try:
            if self.proc.poll() is None:
                self.killed = True
                self.proc.kill()
        except OSError:
            pass

    def stderr_text(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace").strip()

    def close(self) -> Optional[int]:
        if self._closed:
            return self.proc.returncode
        self._closed = True
        self.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            logger.warning("search subprocess %s did not exit after kill", self.proc.pid)
        for stream in (self.proc.stdout,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._stderr_thread.join(timeout=2)
        try:
            if self.proc.stderr is not None:
                self.proc.stderr.close()
        except OSError:
            pass
        self.control.detach(self)
        return self.proc.returncode


@dataclass
class _FileHit:
    rel: str
    lines: List[Tuple[int, Optional[int], str, bool]] = field(default_factory=list)
    match_count: int = 0
    count: int = 0
    more: bool = False
    binary: bool = False
    closed: bool = False


class _SearchPage:
    """Collects one page of per-file results (max_results counts FILES)."""

    def __init__(self, offset: int, limit: int, per_file: int, char_budget: int):
        self.offset = offset
        self.limit = limit
        self.per_file = per_file
        self.char_budget = char_budget
        self.files_seen = 0
        self.hits: List[_FileHit] = []
        self.chars = 0
        self.next_offset: Optional[int] = None
        self.limit_reason: Optional[str] = None

    def begin(self, rel: str) -> Optional[_FileHit]:
        """Register a newly matched file; None when skipped by ``offset``."""
        self.files_seen += 1
        if self.files_seen <= self.offset:
            return None
        if len(self.hits) >= self.limit or self.chars >= self.char_budget:
            self.next_offset = self.offset + len(self.hits)
            self.limit_reason = (
                "max_results" if len(self.hits) >= self.limit else "output_chars"
            )
            raise _SearchStop("page_full")
        hit = _FileHit(rel=rel)
        self.hits.append(hit)
        self.chars += len(rel) + 4
        return hit

    def add_line(
        self,
        hit: _FileHit,
        lineno: int,
        column: Optional[int],
        text: str,
        is_match: bool,
    ) -> None:
        if hit.closed:
            return
        if is_match:
            if hit.match_count >= self.per_file:
                hit.more = True
                hit.closed = True
                return
            hit.match_count += 1
        hit.lines.append((lineno, column, text, is_match))
        self.chars += len(text) + 10


@dataclass
class FileInfo:
    """Information about a file or directory."""
    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified_at: Optional[str] = None
    extension: Optional[str] = None


class FileSystemError(Exception):
    """Exception raised by FileSystem operations."""
    pass


def _run_scoped_read_path(path: str) -> Path:
    """Resolve a read path through the active AgentRunScope, if present."""

    try:
        from ...security.agent_run_scope import RunScopeViolation, get_current_run_scope
    except ImportError:  # pragma: no cover - defensive for stripped builds
        return Path(path).resolve()

    scope = get_current_run_scope()
    if scope is None:
        return Path(path).resolve()
    try:
        return scope.assert_read_allowed(path)
    except RunScopeViolation as exc:
        raise FileSystemError(str(exc)) from exc


class FileSystem:
    """
    File system operations utility.
    
    Features:
    - List directory contents with depth control
    - Search files by name pattern
    - Search files by content (grep-like)
    - Get file/directory information
    """
    
    # Extensions to skip when searching content
    BINARY_EXTENSIONS = {
        ".exe", ".dll", ".so", ".dylib", ".bin",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
        ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac",
        ".zip", ".tar", ".gz", ".rar", ".7z",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".pyc", ".pyo", ".class", ".o", ".obj",
        ".db", ".sqlite", ".sqlite3",
    }
    
    # Directories pruned below the search root (dependency / build / cache
    # trees).  Hidden directories are pruned as well.  An explicitly requested
    # root is never pruned, so e.g. ``path="node_modules/pkg"`` stays searchable.
    SKIP_DIRS = {
        ".git", ".svn", ".hg",
        "node_modules", "__pycache__", ".venv", "venv",
        ".idea", ".vscode",
        "dist", "build", "target",
        ".next", ".turbo", ".gradle", ".expo", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", "site-packages", "bower_components",
    }
    
    def __init__(self, allowed_paths: Optional[List[str]] = None):
        """
        Initialize the file system utility.
        
        Args:
            allowed_paths: List of paths where operations are allowed.
                          If None, loads from AOITALK_ALLOWED_PATHS env var.
        """
        if allowed_paths is None:
            env_paths = os.environ.get("AOITALK_ALLOWED_PATHS", "")
            if env_paths:
                self.allowed_paths = [p.strip() for p in env_paths.split(",") if p.strip()]
            else:
                self.allowed_paths = []
        else:
            self.allowed_paths = allowed_paths
            
    def _validate_path(self, path: str) -> Path:
        """Validate and resolve a path."""
        resolved = _run_scoped_read_path(path)
        
        if self.allowed_paths:
            allowed = False
            for allowed_path in self.allowed_paths:
                try:
                    resolved.relative_to(Path(allowed_path).resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise FileSystemError(
                    f"Path is outside allowed directories: {path}. "
                    f"Allowed: {self.allowed_paths}"
                )
                
        return resolved
        
    def list_directory(
        self,
        path: str,
        max_depth: int = 2,
        show_hidden: bool = False,
        pattern: Optional[str] = None
    ) -> str:
        """
        List contents of a directory.
        
        Args:
            path: Directory path to list.
            max_depth: Maximum depth to recurse (1 = current dir only).
            show_hidden: Whether to show hidden files/directories.
            pattern: Optional glob pattern to filter results.
            
        Returns:
            Formatted listing of directory contents.
        """
        dir_path = self._validate_path(path)
        
        if not dir_path.exists():
            raise FileSystemError(f"Directory does not exist: {path}")
        if not dir_path.is_dir():
            raise FileSystemError(f"Path is not a directory: {path}")
            
        items: List[FileInfo] = []
        
        def collect_items(current: Path, depth: int):
            if depth > max_depth:
                return
                
            try:
                for item in sorted(current.iterdir()):
                    # Skip hidden files unless requested
                    if not show_hidden and item.name.startswith("."):
                        continue
                        
                    # A recursive listing can encounter a symlink/junction
                    # even when the requested root itself was safe.  Under an
                    # active run scope, do not inspect or recurse through a
                    # component that leaves the read roots.
                    try:
                        _run_scoped_read_path(str(item))
                    except FileSystemError:
                        continue

                    # Skip common uninteresting directories
                    if item.is_dir() and item.name in self.SKIP_DIRS:
                        continue
                        
                    # Apply pattern filter if specified
                    if pattern and not fnmatch.fnmatch(item.name, pattern):
                        if item.is_dir():
                            collect_items(item, depth + 1)
                        continue
                        
                    try:
                        stat = item.stat()
                        info = FileInfo(
                            path=str(item),
                            name=item.name,
                            is_dir=item.is_dir(),
                            size=stat.st_size if not item.is_dir() else 0,
                            modified_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            extension=item.suffix.lower() if not item.is_dir() else None
                        )
                        items.append(info)
                    except (PermissionError, OSError):
                        # Skip files we can't access
                        continue
                        
                    # Recurse into directories
                    if item.is_dir() and depth < max_depth:
                        collect_items(item, depth + 1)
                        
            except PermissionError:
                pass
                
        collect_items(dir_path, 1)
        
        # Format output
        if not items:
            return f"Directory is empty or no matching files: {path}"
            
        # Group by directory
        output_lines = [f"Contents of {dir_path} (max depth: {max_depth}):"]
        output_lines.append("")
        
        for item in items:
            relative = Path(item.path).relative_to(dir_path)
            indent = "  " * (len(relative.parts) - 1)
            
            if item.is_dir:
                output_lines.append(f"{indent}📁 {item.name}/")
            else:
                size_str = self._format_size(item.size)
                output_lines.append(f"{indent}📄 {item.name} ({size_str})")
                
        output_lines.append("")
        output_lines.append(f"Total: {len(items)} items")
        
        return "\n".join(output_lines)
        
    def search_files(
        self,
        query: str,
        path: str,
        extensions: Optional[List[str]] = None,
        search_content: bool = False,
        max_results: int = 50,
        exclude_path: Optional[Callable[[Path], bool]] = None,
    ) -> str:
        """
        Search for files by name or content.
        
        Args:
            query: Search query (glob pattern for names, regex for content).
            path: Directory to search in.
            extensions: Filter by file extensions (e.g., [".py", ".js"]).
            search_content: If True, search file contents instead of names.
            max_results: Maximum number of results to return.
            
        Returns:
            Formatted search results.
        """
        if search_content:
            structured = self.search_content(
                query,
                path,
                extensions=extensions,
                max_results=max_results,
                exclude_path=exclude_path,
            )
            if not structured.get("success"):
                raise FileSystemError(str(structured.get("error") or "search failed"))
            return str(structured.get("content") or "")

        search_path = self._validate_path(path)

        if not search_path.exists():
            raise FileSystemError(f"Path does not exist: {path}")

        results = self._search_names(
            search_path, query, extensions, max_results, exclude_path
        )

        if not results:
            return f"No files found matching '{query}' in {path}"
            
        output = [f"Search results for '{query}' in {path}:"]
        output.append("")
        output.extend(results)
        
        if len(results) >= max_results:
            output.append(f"\n... (showing first {max_results} results)")
            
        return "\n".join(output)
        
    def _search_names(
        self,
        path: Path,
        pattern: str,
        extensions: Optional[List[str]],
        max_results: int,
        exclude_path: Optional[Callable[[Path], bool]] = None,
    ) -> List[str]:
        """Search files by name pattern."""
        results = []
        
        # Support both glob and simple substring matching
        if "*" in pattern or "?" in pattern:
            matcher = lambda name: fnmatch.fnmatch(name.lower(), pattern.lower())
        else:
            matcher = lambda name: pattern.lower() in name.lower()
            
        for root, dirs, files in os.walk(path):
            # Skip hidden and common uninteresting directories
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in self.SKIP_DIRS
                and not (exclude_path and exclude_path(Path(root) / d))
            ]
            
            for filename in files:
                if len(results) >= max_results:
                    return results
                    
                filepath = Path(root) / filename
                if exclude_path and exclude_path(filepath):
                    continue

                try:
                    _run_scoped_read_path(str(filepath))
                except FileSystemError:
                    continue
                
                # Check extension filter
                if extensions:
                    if filepath.suffix.lower() not in [e.lower() for e in extensions]:
                        continue
                        
                # Check name match
                if matcher(filename):
                    relative = filepath.relative_to(path)
                    results.append(f"📄 {relative}")
                    
        return results
        
    # ------------------------------------------------------------------
    # Structured content search (native ripgrep with explicit Python fallback)
    # ------------------------------------------------------------------
    def search_content(
        self,
        query: str,
        path: str,
        *,
        extensions: Optional[List[str]] = None,
        glob: Optional[str] = None,
        output_mode: str = "content",
        offset: int = 0,
        max_results: int = 50,
        context_lines: int = 0,
        max_matches_per_file: int = _SEARCH_DEFAULT_PER_FILE,
        fixed_strings: bool = False,
        include_ignored: bool = False,
        exclude_path: Optional[Callable[[Path], bool]] = None,
        time_budget: float = _SEARCH_TIME_BUDGET_SECONDS,
    ) -> Dict[str, Any]:
        """Search file contents and return one bounded, continuable page.

        ``max_results`` counts matching FILES (unchanged unit).  When the page
        fills, ``next_offset`` is returned; the caller continues with it.
        Statuses distinguish ``ok`` / ``no_matches`` / ``partial`` (time or
        output budget) / ``interrupted`` (run cancellation) /
        ``invalid_regex`` / ``error``.  The regex syntax is Python ``re``;
        ripgrep is used when it accepts the same pattern, otherwise the Python
        engine runs and ``fallback_reason`` says why.  Invalid patterns are
        never silently turned into literal searches.
        """

        started = time.monotonic()
        mode = str(output_mode or "content").strip().lower()
        base: Dict[str, Any] = {"query": query, "path": str(path), "mode": mode}
        if mode not in SEARCH_OUTPUT_MODES:
            return {
                **base,
                "success": False,
                "status": "error",
                "error": f"output_mode must be one of {', '.join(SEARCH_OUTPUT_MODES)}",
            }
        if not str(query or ""):
            return {**base, "success": False, "status": "error", "error": "query is empty"}
        try:
            offset = max(0, int(offset or 0))
            max_results = min(_SEARCH_MAX_RESULTS, max(1, int(max_results or 50)))
            context_lines = min(_SEARCH_MAX_CONTEXT, max(0, int(context_lines or 0)))
            per_file = min(
                _SEARCH_MAX_PER_FILE,
                max(1, int(max_matches_per_file or _SEARCH_DEFAULT_PER_FILE)),
            )
        except (TypeError, ValueError):
            return {
                **base,
                "success": False,
                "status": "error",
                "error": "offset/max_results/context_lines/max_matches_per_file must be integers",
            }

        pattern_text = re.escape(query) if fixed_strings else query
        try:
            regex = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            return {
                **base,
                "success": False,
                "status": "invalid_regex",
                "error": (
                    f"Invalid regular expression (Python re syntax): {exc}. "
                    "Escape special characters or pass fixed_strings=True to "
                    "search the text literally."
                ),
            }

        search_path = self._validate_path(path)
        if not search_path.exists():
            raise FileSystemError(f"Path does not exist: {path}")

        warnings: List[str] = []
        rg = find_ripgrep()
        engine = "rg" if rg else "python"
        fallback_reason: Optional[str] = None if rg else "rg_not_found"
        if rg:
            rejected = self._rg_preflight(rg, query, fixed_strings)
            if rejected is not None:
                kind, detail = rejected
                if kind == "spawn_failed":
                    rg = None
                    fallback_reason = f"rg_failed: {detail}"
                else:
                    fallback_reason = f"rg_rejected_pattern: {detail}"
                engine = "python"

        page = _SearchPage(offset, max_results, per_file, _SEARCH_OUTPUT_CHARS)
        control = _SearchControl(max(1.0, float(time_budget)))
        stop_reason: Optional[str] = None
        scope_kind = "file"
        pruned: List[str] = []
        try:
            if search_path.is_file():
                candidates: Iterator[Tuple[Path, str]] = iter(
                    []
                    if exclude_path and exclude_path(search_path)
                    else [(search_path, search_path.name)]
                )
                cwd = search_path.parent
            else:
                cwd = search_path
                matcher = self._glob_matcher(glob)
                if rg:
                    scope_kind = "gitignore+skip_dirs" if not include_ignored else "skip_dirs"
                    candidates = self._rg_candidates(
                        rg, search_path, extensions, matcher, include_ignored,
                        exclude_path, control, warnings,
                    )
                else:
                    scope_kind = "skip_dirs"
                    candidates = self._walk_candidates(
                        search_path, extensions, matcher, exclude_path, control, pruned,
                    )
            for batch in self._batches(candidates, control):
                if engine == "rg" and rg:
                    self._rg_search_batch(
                        rg, cwd, batch, query, fixed_strings, mode, context_lines,
                        per_file, page, control, warnings,
                    )
                else:
                    for abs_path, rel in batch:
                        control.check()
                        self._py_search_file(
                            abs_path, rel, regex, mode, context_lines, page, control,
                        )
        except _SearchStop as stop:
            stop_reason = stop.reason
        finally:
            control.close()

        if stop_reason is None and control.stop_reason in {"interrupted", "time_budget"}:
            stop_reason = control.stop_reason
        if stop_reason == "page_full":
            stop_reason = None

        hits = page.hits
        status = "ok" if hits else "no_matches"
        truncated = page.next_offset is not None
        limit_reason = page.limit_reason
        if stop_reason in {"time_budget", "output_bytes"}:
            status = "partial"
            truncated = True
            limit_reason = stop_reason
        elif stop_reason == "interrupted":
            status = "interrupted"
            truncated = True
            limit_reason = "interrupted"

        result: Dict[str, Any] = {
            **base,
            "success": status != "interrupted",
            "status": status,
            "engine": engine,
            "fallback_reason": fallback_reason,
            "scope": scope_kind,
            "offset": offset,
            "returned_files": len(hits),
            "next_offset": page.next_offset,
            "truncated": truncated,
            "limit_reason": limit_reason,
            "files_with_more_matches": sum(1 for hit in hits if hit.more),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if mode == "count":
            result["total_matches"] = sum(hit.count for hit in hits)
        if pruned:
            result["pruned_dirs"] = pruned[:10]
        if warnings:
            result["warnings"] = warnings[:5]
        if status == "interrupted":
            result["error"] = "検索は実行の停止要求により中断されました（部分結果のみ）"
        result["content"] = self._format_content_page(
            query, str(path), mode, page, offset, status, limit_reason,
        )
        return result

    # -- candidate enumeration ---------------------------------------------
    @staticmethod
    def _glob_matcher(glob: Optional[str]) -> Optional[Callable[[str], bool]]:
        patterns = [item.strip() for item in str(glob or "").split(",") if item.strip()]
        if not patterns:
            return None
        includes = [p.lower() for p in patterns if not p.startswith("!")]
        excludes = [p[1:].lower() for p in patterns if p.startswith("!") and len(p) > 1]

        def matches(pattern: str, rel: str, name: str) -> bool:
            if "/" not in pattern:
                return fnmatch.fnmatchcase(name, pattern)
            candidates = {pattern, pattern.replace("**/", ""), pattern.lstrip("/")}
            return any(fnmatch.fnmatchcase(rel, item) for item in candidates)

        def matcher(rel: str) -> bool:
            rel_l = rel.lower()
            name = rel_l.rsplit("/", 1)[-1]
            if includes and not any(matches(p, rel_l, name) for p in includes):
                return False
            return not any(matches(p, rel_l, name) for p in excludes)

        return matcher

    def _candidate_allowed(
        self,
        root: Path,
        rel: str,
        extensions: Optional[List[str]],
        matcher: Optional[Callable[[str], bool]],
        exclude_path: Optional[Callable[[Path], bool]],
        scope_active: bool,
        dir_cache: Dict[str, bool],
        is_link: bool = False,
    ) -> Optional[Path]:
        """Read-scope / enterprise / filter checks BEFORE a path reaches rg."""
        abs_path = root / rel
        suffix = abs_path.suffix.lower()
        if suffix in self.BINARY_EXTENSIONS:
            return None
        if extensions and suffix not in extensions:
            return None
        if matcher is not None and not matcher(rel):
            return None
        if exclude_path is not None:
            parts = rel.split("/")
            for index in range(1, len(parts)):
                key = "/".join(parts[:index])
                allowed = dir_cache.get(key)
                if allowed is None:
                    allowed = not exclude_path(root / key)
                    dir_cache[key] = allowed
                if not allowed:
                    return None
            if exclude_path(abs_path):
                return None
        if scope_active or is_link:
            # Resolves symlinks/junctions anywhere in the chain.
            try:
                self._validate_path(str(abs_path))
            except FileSystemError:
                return None
        return abs_path

    def _rg_candidates(
        self,
        rg: str,
        root: Path,
        extensions: Optional[List[str]],
        matcher: Optional[Callable[[str], bool]],
        include_ignored: bool,
        exclude_path: Optional[Callable[[Path], bool]],
        control: _SearchControl,
        warnings: List[str],
    ) -> Iterator[Tuple[Path, str]]:
        """Enumerate names with ``rg --files`` (reads no file contents)."""
        argv = [
            rg, "--no-config", "--files", "--hidden", "--sort", "path",
            "--no-messages", "-g", "!.*/",
        ]
        for name in sorted(self.SKIP_DIRS):
            if not name.startswith("."):
                argv.extend(["-g", f"!{name}/"])
        if include_ignored:
            argv.append("--no-ignore-vcs")
        argv.append(".")
        normalized_ext = [e.lower() for e in extensions] if extensions else None
        scope_active = _run_scope_active()
        dir_cache: Dict[str, bool] = {}
        owned = _OwnedProcess(argv, str(root), control)
        produced = 0
        try:
            while True:
                raw = owned.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    continue  # absurd path length; ignore fragment
                rel = raw.rstrip(b"\r\n").decode("utf-8", "replace").replace("\\", "/")
                if rel.startswith("./"):
                    rel = rel[2:]
                if not rel:
                    continue
                produced += 1
                if produced % 512 == 0:
                    control.check()
                abs_path = self._candidate_allowed(
                    root, rel, normalized_ext, matcher, exclude_path, scope_active, dir_cache,
                )
                if abs_path is not None:
                    yield abs_path, rel
        finally:
            killed = owned.killed
            code = owned.close()
            if code not in (0, 1, None) and not killed:
                detail = owned.stderr_text()[:300]
                if detail:
                    warnings.append(f"rg --files: {detail}")
        control.check()

    def _walk_candidates(
        self,
        root: Path,
        extensions: Optional[List[str]],
        matcher: Optional[Callable[[str], bool]],
        exclude_path: Optional[Callable[[Path], bool]],
        control: _SearchControl,
        pruned: List[str],
    ) -> Iterator[Tuple[Path, str]]:
        """Python fallback enumeration (gitignore is NOT applied)."""
        normalized_ext = [e.lower() for e in extensions] if extensions else None
        scope_active = _run_scope_active()
        dir_cache: Dict[str, bool] = {}
        stack: List[Tuple[Path, str]] = [(root, "")]
        seen = 0
        while stack:
            current, rel_dir = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError:
                continue
            subdirs: List[Tuple[Path, str]] = []
            for entry in entries:
                seen += 1
                if seen % 512 == 0:
                    control.check()
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    is_dir = entry.is_dir()
                    is_symlink = entry.is_symlink()
                except OSError:
                    continue
                if is_dir:
                    if is_symlink:
                        continue  # same as os.walk(followlinks=False)
                    if entry.name.startswith(".") or entry.name in self.SKIP_DIRS:
                        pruned.append(rel)
                        continue
                    child = Path(entry.path)
                    if exclude_path and exclude_path(child):
                        continue
                    if scope_active or _is_junction(entry.path):
                        try:
                            self._validate_path(entry.path)
                        except FileSystemError:
                            continue
                    subdirs.append((child, rel))
                    continue
                abs_path = self._candidate_allowed(
                    root, rel, normalized_ext, matcher, exclude_path, scope_active,
                    dir_cache, is_link=is_symlink,
                )
                if abs_path is not None:
                    yield abs_path, rel
            stack.extend(reversed(subdirs))

    @staticmethod
    def _batches(
        candidates: Iterator[Tuple[Path, str]], control: _SearchControl
    ) -> Iterator[List[Tuple[Path, str]]]:
        batch: List[Tuple[Path, str]] = []
        size = 0
        for item in candidates:
            batch.append(item)
            size += len(item[1]) + 3
            if size >= _SEARCH_ARGV_BUDGET or len(batch) >= 2000:
                control.check()
                yield batch
                batch, size = [], 0
        if batch:
            yield batch

    # -- ripgrep engine -------------------------------------------------------
    @staticmethod
    def _rg_preflight(rg: str, query: str, fixed_strings: bool) -> Optional[Tuple[str, str]]:
        """Compile the pattern in rg against empty stdin (no file access)."""
        argv = [rg, "--no-config", "--engine", "auto", "-i", "--crlf"]
        if fixed_strings:
            argv.append("-F")
        argv.extend(["-e", query, "-"])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                argv,
                input=b"",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=15,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ("spawn_failed", str(exc)[:200])
        if completed.returncode in (0, 1):
            return None
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        message = next(
            (line.strip() for line in detail if line.strip().startswith("error:")),
            detail[-1].strip() if detail else f"exit {completed.returncode}",
        )
        return ("pattern", message[:200])

    _RG_LINE_RE = re.compile(rb"(\d+)([:-])(.*)", re.S)
    _RG_COLUMN_RE = re.compile(rb"(\d+):(.*)", re.S)
    _RG_OMITTED = "[... omitted end of long line]"

    def _rg_search_batch(
        self,
        rg: str,
        cwd: Path,
        batch: List[Tuple[Path, str]],
        query: str,
        fixed_strings: bool,
        mode: str,
        context_lines: int,
        per_file: int,
        page: _SearchPage,
        control: _SearchControl,
        warnings: List[str],
    ) -> None:
        argv = [
            rg, "--no-config", "--no-ignore", "--hidden", "--color", "never",
            "--threads", "1", "--engine", "auto", "--crlf", "-i",
        ]
        if fixed_strings:
            argv.append("-F")
        if mode == "files":
            argv.append("--files-with-matches")
        elif mode == "count":
            argv.extend(["--count-matches", "--with-filename", "--null"])
        else:
            argv.extend([
                "--no-heading", "--with-filename", "--line-number", "--column",
                "--null", "--max-columns", str(_SEARCH_MAX_COLUMNS),
                "--max-columns-preview", "--max-count", str(per_file + 1),
            ])
            if context_lines:
                argv.extend(["-C", str(context_lines)])
        argv.extend(["-e", query, "--"])
        display: Dict[str, str] = {}
        for abs_path, rel in batch:
            arg = rel.replace("/", os.sep)
            argv.append(arg)
            display[rel] = rel
        owned = _OwnedProcess(argv, str(cwd), control)
        current: Optional[_FileHit] = None
        current_rel: Optional[str] = None
        read_bytes = 0
        try:
            while True:
                raw = owned.readline()
                if not raw:
                    break
                read_bytes += len(raw)
                if read_bytes > _SEARCH_STDOUT_BYTES:
                    raise _SearchStop("output_bytes")
                while not raw.endswith(b"\n"):
                    rest = owned.readline()  # discard the rest of an over-long record
                    if not rest:
                        break
                    read_bytes += len(rest)
                    if read_bytes > _SEARCH_STDOUT_BYTES:
                        raise _SearchStop("output_bytes")
                    raw = raw[:_SEARCH_READ_LINE_CAP] + (b"\n" if rest.endswith(b"\n") else b"")
                record = raw.rstrip(b"\n").rstrip(b"\r")
                if mode == "files":
                    rel = display.get(record.decode("utf-8", "replace").replace("\\", "/"))
                    if rel is not None:
                        page.begin(rel)
                    continue
                if b"\0" not in record:
                    if record == b"--":
                        continue
                    text = record.decode("utf-8", "replace")
                    marker = text.rfind(": binary file matches")
                    if marker > 0:
                        rel = display.get(text[:marker].replace("\\", "/"))
                        if rel is not None and rel != current_rel:
                            current_rel = rel
                            current = page.begin(rel)
                        if current is not None:
                            current.binary = True
                            current.closed = True
                    continue
                path_bytes, rest = record.split(b"\0", 1)
                rel = display.get(path_bytes.decode("utf-8", "replace").replace("\\", "/"))
                if rel is None:
                    continue
                if rel != current_rel:
                    current_rel = rel
                    current = page.begin(rel)
                if current is None:
                    continue  # skipped by offset
                if mode == "count":
                    try:
                        current.count = int(rest.strip() or b"0")
                    except ValueError:
                        current.count = 0
                    continue
                parsed = self._RG_LINE_RE.match(rest)
                if not parsed:
                    continue
                lineno = int(parsed.group(1))
                is_match = parsed.group(2) == b":"
                body = parsed.group(3)
                column: Optional[int] = None
                if is_match:
                    col = self._RG_COLUMN_RE.match(body)
                    if col:
                        column = int(col.group(1))
                        body = col.group(2)
                text = body.decode("utf-8", "replace")
                if text.endswith(self._RG_OMITTED) or len(text) > _SEARCH_MAX_COLUMNS + 40:
                    text = text[: _SEARCH_MAX_COLUMNS].rstrip() + " [... long line truncated]"
                else:
                    column = None
                page.add_line(current, lineno, column, text, is_match)
            control.check()
        finally:
            killed = owned.killed
            code = owned.close()
            if code not in (0, 1, None) and not killed:
                detail = owned.stderr_text()[:300]
                if detail:
                    warnings.append(f"rg: {detail}")

    # -- Python engine (explicit fallback) -----------------------------------
    @staticmethod
    def _preview(text: str, start: int) -> Tuple[str, bool]:
        if len(text) <= _SEARCH_MAX_COLUMNS:
            return text, False
        begin = max(0, start - 100)
        snippet = text[begin: begin + _SEARCH_MAX_COLUMNS]
        prefix = "[...] " if begin else ""
        return f"{prefix}{snippet} [... long line truncated]", True

    def _py_search_file(
        self,
        abs_path: Path,
        rel: str,
        regex: "re.Pattern[str]",
        mode: str,
        context_lines: int,
        page: _SearchPage,
        control: _SearchControl,
    ) -> None:
        """Stream one file in bounded chunks (no whole-file read)."""
        try:
            handle = open(abs_path, "rb")
        except OSError:
            return
        with handle:
            try:
                probe = handle.read(_SEARCH_BINARY_PROBE)
                handle.seek(0)
            except OSError:
                return
            binary = b"\0" in probe
            hit: Optional[_FileHit] = None
            started_hit = False
            before: "deque[Tuple[int, str]]" = deque(maxlen=context_lines or 1)
            after_left = 0
            lineno = 0
            column_base = 0
            continuing = False
            last_added = 0
            iterations = 0
            while True:
                try:
                    chunk = handle.readline(_SEARCH_PY_CHUNK)
                except OSError:
                    return
                if not chunk:
                    return
                iterations += 1
                if iterations % 2000 == 0:
                    control.check()
                if not continuing:
                    lineno += 1
                    column_base = 0
                ended = chunk.endswith(b"\n")
                text = chunk.decode("utf-8", "replace")
                if ended:
                    text = text[:-1]
                    if text.endswith("\r"):
                        text = text[:-1]
                continuing = not ended
                match = regex.search(text)
                if binary:
                    if match:
                        hit = page.begin(rel)
                        if hit is not None:
                            hit.binary = True
                            hit.closed = True
                        return
                    continue
                if match:
                    if not started_hit:
                        started_hit = True
                        hit = page.begin(rel)
                    if hit is None or mode == "files":
                        return
                    if mode == "count":
                        hit.count += sum(1 for _ in regex.finditer(text))
                        column_base += len(text)
                        continue
                    if lineno == last_added:
                        # Another chunk of an over-long line already reported.
                        column_base += len(text)
                        continue
                    for prev_line, prev_text in before:
                        if prev_line > last_added:
                            page.add_line(hit, prev_line, None, prev_text, False)
                    before.clear()
                    preview, clipped = self._preview(text, match.start())
                    page.add_line(
                        hit,
                        lineno,
                        (column_base + match.start() + 1) if clipped else None,
                        preview,
                        True,
                    )
                    last_added = lineno
                    if hit.closed:
                        return
                    after_left = context_lines
                elif hit is not None and mode == "content" and after_left > 0:
                    preview, _ = self._preview(text, 0)
                    if lineno > last_added:
                        page.add_line(hit, lineno, None, preview, False)
                        last_added = lineno
                    after_left -= 1
                elif context_lines and mode == "content":
                    preview, _ = self._preview(text, 0)
                    if not before or before[-1][0] != lineno:
                        before.append((lineno, preview))
                column_base += len(text)

    # -- formatting -----------------------------------------------------------
    @staticmethod
    def _format_content_page(
        query: str,
        path: str,
        mode: str,
        page: _SearchPage,
        offset: int,
        status: str,
        limit_reason: Optional[str],
    ) -> str:
        if not page.hits:
            if status == "interrupted":
                return f"No files found matching '{query}' in {path} (search interrupted)"
            if status == "partial":
                return (
                    f"No files found matching '{query}' in {path} "
                    f"(search stopped early: {limit_reason}; narrow path/glob)"
                )
            return f"No files found matching '{query}' in {path}"
        output = [f"Search results for '{query}' in {path}:", ""]
        for hit in page.hits:
            if mode == "count":
                output.append(f"📄 {hit.rel}")
                output.append(f"  matches: {hit.count}")
                continue
            if hit.binary:
                output.append(f"📄 {hit.rel}")
                output.append("  (binary file matches; content not shown)")
                continue
            output.append(f"📄 {hit.rel}")
            if mode == "files":
                continue
            previous = None
            for lineno, column, text, is_match in hit.lines:
                if previous is not None and lineno > previous + 1:
                    output.append("  --")
                marker = ":" if is_match else "-"
                col = f" (col {column})" if column else ""
                output.append(f"  {lineno}{marker}{col} {text}")
                previous = lineno
            if hit.more:
                output.append(
                    "  (more matches in this file: search this file directly or "
                    "raise max_matches_per_file)"
                )
        first = offset + 1
        last = offset + len(page.hits)
        if page.next_offset is not None:
            output.append(
                f"\n... (showing first {len(page.hits)} results from offset {offset} "
                f"[files {first}-{last}]; continue with offset={page.next_offset})"
            )
        elif status in {"partial", "interrupted"}:
            output.append(
                f"\n... (showing first {len(page.hits)} results; search stopped early: "
                f"{limit_reason}. Narrow path/glob for complete results)"
            )
        return "\n".join(output)


    def get_file_info(self, path: str) -> str:
        """
        Get detailed information about a file or directory.
        
        Args:
            path: Path to examine.
            
        Returns:
            Formatted file/directory information.
        """
        file_path = self._validate_path(path)
        
        if not file_path.exists():
            raise FileSystemError(f"Path does not exist: {path}")
            
        stat = file_path.stat()
        
        info = []
        info.append(f"Path: {file_path}")
        info.append(f"Type: {'Directory' if file_path.is_dir() else 'File'}")
        
        if not file_path.is_dir():
            info.append(f"Size: {self._format_size(stat.st_size)}")
            info.append(f"Extension: {file_path.suffix or '(none)'}")
            
        info.append(f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        info.append(f"Created: {datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}")
        
        if file_path.is_dir():
            try:
                count = sum(1 for _ in file_path.iterdir())
                info.append(f"Items: {count}")
            except PermissionError:
                info.append("Items: (access denied)")
                
        return "\n".join(info)
        
    @staticmethod
    def _format_size(size: int) -> str:
        """Format file size in human-readable format."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# Global instance
_fs: Optional[FileSystem] = None


def get_file_system() -> FileSystem:
    """Get or create the global FileSystem instance."""
    global _fs
    if _fs is None:
        _fs = FileSystem()
    return _fs
