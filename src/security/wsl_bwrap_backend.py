"""Windows WSL2 + bubblewrap file-scoped command backend.

The generic Windows shell cannot be made repository-safe by checking its cwd or
parsing command text.  This adapter runs the command inside a Debian WSL2
namespace where only the selected repository is mounted read/write; the host
``/mnt`` tree is intentionally not mounted, so Windows paths outside the
selected repository are not reachable from the shell or its descendants.

This module is deliberately small and dependency-free.  It is imported lazily
by :mod:`src.tools.os_operations.command_executor` so ordinary user/app shell
calls keep their legacy behaviour and machines without WSL2 remain usable for
unscoped operations.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import hashlib
import stat
import threading
import filecmp
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence, Iterable


class WslBwrapError(RuntimeError):
    """Raised when the file-scoped WSL/bwrap backend cannot be used safely."""


@dataclass(frozen=True)
class SandboxResult:
    """Small result object consumed by ``CommandExecutor``."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False
    error_message: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class _SnapshotEntry:
    """A small, content-aware snapshot record used by mediated publication."""

    kind: str
    digest: str = ""
    mode: int = 0
    link_target: str = ""
    size: int = 0


@dataclass
class _StageTree:
    """One canonical tree represented by a temporary command-visible copy."""

    canonical_root: Path
    stage_root: Path
    baseline: dict[str, _SnapshotEntry]
    # The target tree excludes ``.git`` and any scratch tree nested inside it;
    # those paths are mounted separately and must not be published twice.
    excluded_prefixes: tuple[str, ...] = ()
    exclude_sensitive: bool = False


class _FinalizationState:
    """Exactly-once publication state shared by foreground and async callers."""

    def __init__(
        self,
        backend: "WslBwrapBackend",
        scope: Any,
        trees: Sequence[_StageTree],
        *,
        stage_parent: Path | None = None,
        monitor_roots: Sequence[Path] = (),
        max_disk_bytes: int | None = None,
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.trees = tuple(trees)
        self._lock = threading.Lock()
        self._event = threading.Event()
        self.finalized = False
        self.cancelled = False
        self.cancel_reason = ""
        self.outcome: dict[str, Any] | None = None
        self.max_disk_bytes = max_disk_bytes
        self.stage_parent = stage_parent
        self.monitor_roots = tuple(monitor_roots)
        self.baseline_bytes = sum(
            entry.size
            for tree in self.trees
            for entry in tree.baseline.values()
            if entry.kind == "file"
        ) + sum(_regular_tree_bytes(root) for root in self.monitor_roots)

    def cancel(self, reason: str = "process stopped") -> None:
        """Discard staged changes when a process is stopped or times out."""

        with self._lock:
            if self.finalized:
                return
            self.cancelled = True
            self.cancel_reason = str(reason or "process stopped")

    def finalize(self, exit_code: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self.finalized:
                return dict(self.outcome or {"success": False, "error": "finalization unavailable"})
            self.finalized = True
            cancelled = self.cancelled
            cancel_reason = self.cancel_reason
        if cancelled or (exit_code is not None and exit_code != 0):
            # A killed/failed process may have left partial writes in staging;
            # never publish those implicitly.  The canonical host remains
            # unchanged while the caller receives a durable discard outcome.
            outcome = {
                "success": True,
                "published": 0,
                "discarded": True,
                "reason": cancel_reason or f"process exited with status {exit_code}",
            }
            with self._lock:
                self.outcome = dict(outcome)
                self._event.set()
            self.cleanup()
            return dict(outcome)
        try:
            if self.max_disk_bytes is not None:
                current_bytes = sum(
                    _regular_tree_bytes(tree.stage_root) for tree in self.trees
                ) + sum(_regular_tree_bytes(root) for root in self.monitor_roots)
                if current_bytes > self.baseline_bytes + self.max_disk_bytes:
                    raise WslBwrapError(
                        "sandbox disk growth exceeded trusted max_disk_bytes "
                        f"({self.max_disk_bytes})"
                    )
            outcome = self.backend._finalize_staging(self.scope, self.trees)
        except Exception as exc:  # pragma: no cover - defensive fail-closed guard
            outcome = {"success": False, "error": f"staged publication failed: {exc}"}
        finally:
            self.cleanup()
        with self._lock:
            self.outcome = dict(outcome)
            self._event.set()
        return dict(outcome)

    def wait(self, timeout: float | None = None) -> dict[str, Any] | None:
        if not self._event.wait(timeout):
            return None
        with self._lock:
            return dict(self.outcome or {})

    def cleanup(self) -> None:
        # ``TemporaryDirectory`` is deliberately not retained here: process
        # watchers and foreground runs both need deterministic cleanup, and a
        # best-effort rmtree is safe after publication has completed.
        for tree in self.trees:
            try:
                shutil.rmtree(tree.stage_root, ignore_errors=True)
            except Exception:
                pass
        if self.stage_parent is not None:
            try:
                shutil.rmtree(self.stage_parent, ignore_errors=True)
            except Exception:
                pass


def _path_key(path: Path | str) -> str:
    """Canonical comparison key without relying on ``Path`` repr details."""

    try:
        raw = os.fspath(path)
    except TypeError:
        raw = str(path)
    return os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(raw))))


def _is_within(path: Path | str, root: Path | str) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except (OSError, ValueError):
        return False


def _is_link_or_reparse(path: Path) -> bool:
    """Detect links/junctions/reparse points without following them."""

    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # An uninspectable node must not be published.
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & flag)


def _safe_alias(path: Path, prefix: str) -> str:
    """Return a deterministic, shell-safe mount alias for one canonical path."""

    leaf = path.name or path.drive or "root"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(leaf)).strip(".-") or "root"
    digest = hashlib.sha256(_path_key(path).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{prefix}-{slug[:40]}-{digest}"


def _snapshot_signature(path: Path) -> _SnapshotEntry:
    """Read a node's type/content signature without traversing links."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise WslBwrapError(f"cannot inspect staged path: {path}: {exc}") from exc
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise WslBwrapError(f"cannot inspect link target: {path}: {exc}") from exc
        return _SnapshotEntry("link", mode=mode, link_target=str(target))
    if stat.S_ISDIR(info.st_mode):
        return _SnapshotEntry("dir", mode=mode)
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise WslBwrapError(f"cannot read staged file: {path}: {exc}") from exc
        return _SnapshotEntry(
            "file",
            digest=digest.hexdigest(),
            mode=mode,
            size=int(info.st_size),
        )
    return _SnapshotEntry("other", mode=mode)


def _sensitive_relative_path(relative: str) -> bool:
    for part in relative.replace("\\", "/").split("/"):
        name = part.casefold()
        if (
            name in {
                "credentials", "credential", "secrets", "secret",
                ".ssh", "id_rsa", "id_ed25519",
            }
            or name == ".env"
            or name.startswith(".env.")
            or name.endswith((".key", ".pem", ".p12", ".pfx", ".secret", ".secrets"))
        ):
            return True
    return False


def _snapshot_tree(
    root: Path,
    *,
    excluded_prefixes: Sequence[str] = (),
    exclude_sensitive: bool = False,
) -> dict[str, _SnapshotEntry]:
    """Snapshot direct children of *root* while never following symlink dirs."""

    if not root.exists() or not root.is_dir():
        raise WslBwrapError(f"sandbox root is not an existing directory: {root}")
    excludes = tuple(str(item).replace("\\", "/").strip("/") for item in excluded_prefixes)
    result: dict[str, _SnapshotEntry] = {}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        # Remove link/reparse directories from traversal; retain them as an
        # entry so a newly-created link is rejected during finalization.
        for name in list(dirs):
            rel = (current_path / name).relative_to(root).as_posix()
            if (
                any(rel == ex or rel.startswith(ex + "/") for ex in excludes)
                or (exclude_sensitive and _sensitive_relative_path(rel))
            ):
                dirs.remove(name)
                continue
            child = current_path / name
            result[rel] = _snapshot_signature(child)
            if _is_link_or_reparse(child):
                dirs.remove(name)
        for name in files:
            rel = (current_path / name).relative_to(root).as_posix()
            if (
                any(rel == ex or rel.startswith(ex + "/") for ex in excludes)
                or (exclude_sensitive and _sensitive_relative_path(rel))
            ):
                continue
            child = current_path / name
            result[rel] = _snapshot_signature(child)
    return result


def _regular_tree_bytes(root: Path) -> int:
    """Return regular-file bytes without following links/reparse directories."""

    total = 0
    if not root.exists():
        return 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            if _is_link_or_reparse(current_path / name):
                dirs.remove(name)
        for name in files:
            path = current_path / name
            if _is_link_or_reparse(path):
                continue
            try:
                total += int(path.stat().st_size)
            except OSError as exc:
                raise WslBwrapError(f"cannot measure staged disk usage: {path}: {exc}") from exc
    return total


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    excluded_prefixes: Sequence[str] = (),
    exclude_sensitive: bool = False,
) -> None:
    """Copy a tree for staging while preserving baseline links as links."""

    excludes = tuple(str(item).replace("\\", "/").strip("/") for item in excluded_prefixes)
    destination.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(source).as_posix() if current_path != source else ""
        # Filter excluded trees (notably .git and nested scratch roots).
        for name in list(dirs):
            rel = "/".join(item for item in (relative_dir, name) if item)
            if (
                any(rel == ex or rel.startswith(ex + "/") for ex in excludes)
                or (exclude_sensitive and _sensitive_relative_path(rel))
            ):
                dirs.remove(name)
                continue
            child = current_path / name
            target = destination / Path(rel)
            if _is_link_or_reparse(child):
                dirs.remove(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.symlink_to(os.readlink(child), target_is_directory=True)
                except OSError as exc:
                    raise WslBwrapError(f"cannot stage link {child}: {exc}") from exc
            else:
                target.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copystat(child, target, follow_symlinks=False)
                except OSError:
                    pass
        for name in files:
            rel = "/".join(item for item in (relative_dir, name) if item)
            if (
                any(rel == ex or rel.startswith(ex + "/") for ex in excludes)
                or (exclude_sensitive and _sensitive_relative_path(rel))
            ):
                continue
            child = current_path / name
            target = destination / Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_link_or_reparse(child):
                try:
                    target.symlink_to(os.readlink(child), target_is_directory=False)
                except OSError as exc:
                    raise WslBwrapError(f"cannot stage link {child}: {exc}") from exc
            else:
                try:
                    shutil.copy2(child, target, follow_symlinks=False)
                except OSError as exc:
                    raise WslBwrapError(f"cannot stage file {child}: {exc}") from exc


def _escape_wsl_windows_argument(path: str) -> str:
    """Escape backslashes for the Windows ``wsl.exe`` command-line parser."""

    # ``subprocess`` builds a Windows command line before wsl.exe receives it;
    # a single backslash in ``D:\\repo`` is consumed as an escape.  Doubling
    # them is required for ``wslpath`` to see a valid Windows path.
    return str(path).replace("\\", "\\\\")


def _terminate_host_process_tree(process: subprocess.Popen) -> None:
    """Force-stop the host wrapper and its descendants within a short bound.

    ``bwrap --die-with-parent`` handles the Linux namespace after the
    ``wsl.exe`` wrapper exits.  Windows ``Popen.kill`` alone is not sufficient:
    it can leave a WSL child alive, so use the OS tree primitive first.
    """

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    else:
        try:
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass


class _BoundedPipeCapture:
    """Drain a child pipe without retaining unbounded output in host memory."""

    def __init__(self, limit: int) -> None:
        self.limit = max(int(limit), 1)
        self._head_limit = max(self.limit // 2, 1)
        self._tail_limit = max(self.limit - self._head_limit, 0)
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def append(self, chunk: bytes | str) -> None:
        data = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
        if not data:
            return
        self.total += len(data)
        if len(self._head) < self._head_limit:
            take = min(self._head_limit - len(self._head), len(data))
            self._head.extend(data[:take])
            data = data[take:]
        if self._tail_limit and data:
            self._tail.extend(data)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    def text(self) -> str:
        if self.total <= self.limit:
            payload = bytes(self._head + self._tail)
        else:
            marker = (
                f"\n...[sandbox output truncated; {self.total} bytes produced]...\n"
            ).encode("utf-8")
            available = max(self.limit - len(marker), 0)
            head_size = min(len(self._head), available // 2)
            tail_size = min(len(self._tail), available - head_size)
            tail = self._tail[-tail_size:] if tail_size else b""
            payload = bytes(self._head[:head_size]) + marker + bytes(tail)
        return payload.decode("utf-8", errors="replace")


def _drain_pipe_bounded(stream: Any, capture: _BoundedPipeCapture) -> None:
    read_some = getattr(stream, "read1", None) or stream.read
    try:
        while True:
            chunk = read_some(4096)
            if not chunk:
                break
            capture.append(chunk)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class WslBwrapBackend:
    """Run one repository-scoped shell in WSL2 bubblewrap."""

    file_scoped = True
    _GIT_PUBLICATION_OR_DESTRUCTIVE = re.compile(
        r"(?i)(?:^|[;&|]\s*)git\s+"
        r"(?:(?:-[A-Za-z0-9][^\s]*|--[^\s]+)(?:\s+[^\s]+)?\s+)*"
        r"(?:commit|push|reset|clean|rebase|checkout\s+--|branch\s+-D)\b"
    )

    def __init__(
        self,
        *,
        distribution: str = "Debian",
        wsl_executable: str = "wsl.exe",
        bwrap_executable: str = "/usr/bin/bwrap",
        workspace_mount: str = "/workspace",
        # A cold WSL distribution can take several seconds to start on
        # Windows; keep this bounded but above the observed cold-start cost.
        translation_timeout_seconds: float = 30.0,
    ) -> None:
        self.distribution = str(distribution or "Debian")
        self.wsl_executable = str(wsl_executable or "wsl.exe")
        self.bwrap_executable = str(bwrap_executable or "/usr/bin/bwrap")
        self.workspace_mount = str(workspace_mount or "/workspace").rstrip("/") or "/workspace"
        self.translation_timeout_seconds = max(float(translation_timeout_seconds), 0.1)
        self._publication_lock = threading.Lock()

    def is_available(self) -> bool:
        """Return whether the configured WSL executable is discoverable."""

        # The adapter is specifically for Windows-hosted WSL2.  Tests can
        # inject a fake executable or monkeypatch this method when exercising
        # argv construction on another host.
        return os.name == "nt" and shutil.which(self.wsl_executable) is not None

    def _require_available(self) -> None:
        if not self.is_available():
            raise WslBwrapError(
                "file-scoped WSL2/bubblewrap backend is unavailable "
                f"(executable={self.wsl_executable!r}, distribution={self.distribution!r})"
            )

    @staticmethod
    def _roots(scope: Any, name: str) -> tuple[Path, ...]:
        values = getattr(scope, name, ()) or ()
        if isinstance(values, (str, os.PathLike)):
            values = (values,)
        result: list[Path] = []
        seen: set[str] = set()
        for value in values:
            try:
                path = Path(value)
            except TypeError as exc:
                raise WslBwrapError(f"invalid {name} entry: {value!r}") from exc
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return tuple(result)

    @staticmethod
    def _contains_root(roots: Sequence[Path], path: Path) -> bool:
        return any(_path_key(path) == _path_key(root) for root in roots)

    @staticmethod
    def _path_inside_any(path: Path, roots: Sequence[Path]) -> bool:
        return any(_is_within(path, root) for root in roots)

    def _target_full_mutation(self, scope: Any) -> bool:
        """Whether the target can safely be bound directly read/write."""

        if str(getattr(scope, "workspace_access_level", "write")) != "write":
            return False
        root = Path(scope.canonical_root)
        return self._contains_root(self._roots(scope, "write_roots"), root) and self._contains_root(
            self._roots(scope, "delete_roots"), root
        )

    def _scratch_full_mutation(self, scope: Any, root: Path) -> bool:
        if str(getattr(scope, "workspace_access_level", "write")) != "write":
            return False
        return self._contains_root(self._roots(scope, "write_roots"), root) and self._contains_root(
            self._roots(scope, "delete_roots"), root
        )

    def _scratch_aliases(self, scope: Any) -> dict[Path, str]:
        return {root: _safe_alias(root, "scratch") for root in self._roots(scope, "scratch_roots")}

    @staticmethod
    def _trusted_upper_scope(scope: Any) -> Any | None:
        """Return the matching server-issued upper scope, or fail closed."""

        try:
            from .harness_execution_scope import get_current_harness_execution_scope

            upper = get_current_harness_execution_scope()
        except Exception:
            upper = None
        if upper is None:
            # Legacy Personal repository scopes remain supported, but an
            # Enterprise process lane without the server-issued upper
            # capability would lose principal identity, resource ceilings,
            # secret masking and forced copy-on-write publication.
            try:
                from ..features import Features

                enterprise = bool(Features.is_enterprise())
            except Exception:
                enterprise = True
            if enterprise:
                raise WslBwrapError(
                    "Enterprise WSL execution requires a server-issued "
                    "HarnessExecutionScope"
                )
            return None
        try:
            if str(getattr(upper, "run_id", "")) != str(getattr(scope, "run_id", "")):
                raise WslBwrapError(
                    "trusted harness scope run identity does not match AgentRunScope"
                )
            if _path_key(Path(upper.canonical_root)) != _path_key(Path(scope.canonical_root)):
                raise WslBwrapError(
                    "trusted harness scope root does not match AgentRunScope"
                )
            expected = upper.to_agent_run_scope()
            if str(getattr(expected, "repo_identity", "")) != str(
                getattr(scope, "repo_identity", "")
            ):
                raise WslBwrapError(
                    "trusted harness scope repository identity does not match AgentRunScope"
                )
            if str(getattr(expected, "workspace_access_level", "")) != str(
                getattr(scope, "workspace_access_level", "")
            ):
                raise WslBwrapError(
                    "trusted harness workspace access does not match AgentRunScope"
                )
            for name in (
                "read_roots",
                "write_roots",
                "delete_roots",
                "command_roots",
                "scratch_roots",
            ):
                expected_keys = {
                    _path_key(item) for item in getattr(expected, name, ()) or ()
                }
                actual_keys = {
                    _path_key(item) for item in getattr(scope, name, ()) or ()
                }
                if expected_keys != actual_keys:
                    raise WslBwrapError(
                        f"trusted harness {name} do not match AgentRunScope"
                    )
        except WslBwrapError:
            raise
        except Exception as exc:
            raise WslBwrapError(f"invalid trusted harness execution scope: {exc}") from exc
        return upper

    @classmethod
    def _trusted_resource_limits(cls, scope: Any) -> Any | None:
        upper = cls._trusted_upper_scope(scope)
        return getattr(upper, "resource_limits", None) if upper is not None else None

    def _network_flags(self, scope: Any) -> list[str]:
        """Resolve network capability from the trusted upper harness scope.

        AgentRunScope intentionally has no network authority.  Therefore the
        default (and every unbound/mismatched upper scope) remains fully
        isolated.  Only an explicitly-issued ``BROAD`` upper capability can
        remove ``--unshare-net``; organization/allowlist modes require a
        broker/firewall implementation that this backend does not provide.
        """

        upper = self._trusted_upper_scope(scope)
        if upper is None:
            return ["--unshare-net"]
        try:
            capability = getattr(getattr(upper, "network_capability", None), "value", None)
            capability = str(capability or getattr(upper, "network_capability", "none")).strip().lower()
        except WslBwrapError:
            raise
        except Exception as exc:
            raise WslBwrapError(f"invalid trusted harness network scope: {exc}") from exc
        if capability == "none":
            return ["--unshare-net"]
        if capability == "broad":
            return []
        raise WslBwrapError(
            f"network capability {capability!r} requires an approved network broker/allowlist"
        )

    @classmethod
    def _resource_limited_command(cls, scope: Any, command: str) -> str:
        """Prefix trusted finite POSIX resource limits, when supplied."""

        limits = cls._trusted_resource_limits(scope)
        if limits is None:
            return command
        clauses: list[str] = []
        for attr, option in (("max_cpu_seconds", "-t"), ("max_processes", "-u")):
            value = getattr(limits, attr, None)
            if value is None:
                continue
            try:
                integer = int(value)
            except (TypeError, ValueError) as exc:
                raise WslBwrapError(f"invalid trusted resource limit {attr}") from exc
            if integer <= 0:
                raise WslBwrapError(f"invalid trusted resource limit {attr}")
            clauses.append(f"ulimit {option} {integer}")
        memory = getattr(limits, "max_memory_bytes", None)
        if memory is not None:
            try:
                kb = max(int(memory) // 1024, 1)
            except (TypeError, ValueError) as exc:
                raise WslBwrapError("invalid trusted resource limit max_memory_bytes") from exc
            clauses.append(f"ulimit -v {kb}")
        disk = getattr(limits, "max_disk_bytes", None)
        if disk is not None:
            try:
                # bash's ``ulimit -f`` uses 1024-byte blocks on the supported
                # WSL distributions.  Aggregate staged growth is monitored on
                # the host as a second boundary.
                blocks = max((int(disk) + 1023) // 1024, 1)
            except (TypeError, ValueError) as exc:
                raise WslBwrapError("invalid trusted resource limit max_disk_bytes") from exc
            clauses.append(f"ulimit -f {blocks}")
        return "; ".join((*clauses, command)) if clauses else command

    def _external_read_roots(self, scope: Any) -> tuple[Path, ...]:
        """Select explicit read roots that are not represented by workspace mounts."""

        target = Path(scope.canonical_root)
        scratch = self._roots(scope, "scratch_roots")
        result: list[Path] = []
        for root in self._roots(scope, "read_roots"):
            # The target and explicit scratch roots get their own mount.  A
            # nested read root must not overlay a writable scratch/target tree
            # and accidentally widen the command view.
            if _is_within(root, target) or self._path_inside_any(root, scratch):
                continue
            if any(_is_within(root, prior) for prior in result):
                continue
            result.append(root)
        return tuple(result)

    def _external_aliases(
        self,
        scope: Any,
        roots: Sequence[Path],
    ) -> dict[Path, str]:
        """Use server-issued friendly aliases when an upper scope is bound."""

        issued: dict[str, str] = {}
        upper = self._trusted_upper_scope(scope)
        if upper is not None:
            for grant in getattr(upper, "external_read_grants", ()) or ():
                alias = str(getattr(grant, "mount_alias", "") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", alias):
                    raise WslBwrapError("trusted external read grant has an unsafe mount alias")
                issued[_path_key(Path(grant.root))] = alias

        result: dict[Path, str] = {}
        used: set[str] = set()
        for root in roots:
            alias = issued.get(_path_key(root), _safe_alias(root, "input"))
            if alias.casefold() in used:
                raise WslBwrapError("trusted external read grant aliases collide")
            used.add(alias.casefold())
            result[root] = alias
        return result

    @staticmethod
    def _external_sensitive_masks(
        roots: Sequence[Path],
        aliases: Mapping[Path, str],
    ) -> list[str]:
        """Mask credential-like descendants of otherwise approved RO roots."""

        argv: list[str] = []
        count = 0
        for root in roots:
            alias = aliases[root]
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                current_path = Path(current)
                for name in list(dirs):
                    child = current_path / name
                    if _is_link_or_reparse(child):
                        raise WslBwrapError(
                            f"external read grant contains a link/reparse entry: {child}"
                        )
                    relative = (current_path / name).relative_to(root).as_posix()
                    if _sensitive_relative_path(relative):
                        dirs.remove(name)
                        argv.extend(
                            [
                                "--tmpfs",
                                str(PurePosixPath("/inputs", alias, relative)),
                            ]
                        )
                        count += 1
                for name in files:
                    child = current_path / name
                    if _is_link_or_reparse(child):
                        raise WslBwrapError(
                            f"external read grant contains a link/reparse entry: {child}"
                        )
                    relative = (current_path / name).relative_to(root).as_posix()
                    if _sensitive_relative_path(relative):
                        argv.extend(
                            [
                                "--ro-bind",
                                "/dev/null",
                                str(PurePosixPath("/inputs", alias, relative)),
                            ]
                        )
                        count += 1
                if count > 4096:
                    raise WslBwrapError(
                        "external read grant contains too many sensitive paths to mask"
                    )
        return argv

    def _cwd_mount_path(
        self,
        scope: Any,
        cwd: str | os.PathLike[str] | None,
        *,
        scratch_aliases: Mapping[Path, str],
        external_aliases: Mapping[Path, str],
    ) -> str:
        """Map an approved host cwd into one of the deterministic mounts."""

        root = Path(scope.canonical_root)
        selected = root if cwd is None else Path(cwd)
        try:
            selected = Path(scope.assert_command_cwd_allowed(selected))
        except Exception as exc:
            raise WslBwrapError(f"command cwd is outside the run scope: {cwd}") from exc
        if _is_within(selected, root):
            relative = selected.relative_to(root)
            return self.workspace_mount if not relative.parts else str(PurePosixPath(self.workspace_mount, *relative.parts))
        for scratch, alias in scratch_aliases.items():
            if _is_within(selected, scratch):
                relative = selected.relative_to(scratch)
                base = PurePosixPath("/scratch", alias)
                return str(base if not relative.parts else PurePosixPath(base, *relative.parts))
        for external, alias in external_aliases.items():
            if _is_within(selected, external):
                relative = selected.relative_to(external)
                base = PurePosixPath("/inputs", alias)
                return str(base if not relative.parts else PurePosixPath(base, *relative.parts))
        raise WslBwrapError(f"command cwd is outside mounted roots: {selected}")

    def _workspace_cwd(self, scope: Any, cwd: str | os.PathLike[str] | None) -> str:
        """Backward-compatible cwd mapper used by callers/tests."""

        return self._cwd_mount_path(scope, cwd, scratch_aliases=self._scratch_aliases(scope), external_aliases={
            root: _safe_alias(root, "input") for root in self._external_read_roots(scope)
        })

    def translate_windows_path(self, path: str | os.PathLike[str]) -> str:
        """Translate one canonical Windows path to an absolute WSL path."""

        self._require_available()
        raw = str(path)
        if not raw or "\x00" in raw:
            raise WslBwrapError("cannot translate an empty/NUL-containing repository path")
        escaped = _escape_wsl_windows_argument(raw)
        try:
            env = self._default_env()
            completed = subprocess.run(
                [
                    self.wsl_executable,
                    "-d",
                    self.distribution,
                    "--",
                    "/usr/bin/wslpath",
                    "-a",
                    escaped,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.translation_timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WslBwrapError(f"wslpath translation failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise WslBwrapError(
                f"wslpath translation failed (exit={completed.returncode}): {detail}"
            )
        translated = (completed.stdout or "").strip()
        if not translated or "\n" in translated or "\r" in translated:
            raise WslBwrapError("wslpath returned an invalid multi-line path")
        if not translated.startswith("/"):
            raise WslBwrapError(f"wslpath returned a non-absolute path: {translated!r}")
        return translated

    @staticmethod
    def _scope_mount_option(scope: Any) -> str:
        """Choose a mount mode for the target source.

        Narrowed mutation scopes are mounted from a private copy and are
        therefore still ``--bind`` inside the namespace.  Read-only scopes
        can use a direct ``--ro-bind`` because no publication is possible.
        """

        if str(getattr(scope, "workspace_access_level", "write")) != "write":
            return "--ro-bind"
        return "--bind"

    def build_argv(
        self,
        scope: Any,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        shell: str | None = None,
    ) -> list[str]:
        """Build the complete WSL+bwrap argv for one scoped command.

        ``build_argv`` is primarily an inspection/testing API.  If a narrowed
        scope requires a temporary snapshot, it is created only long enough to
        construct argv and then removed; :meth:`spawn` owns a retained plan.
        """

        argv, state = self._build_argv_and_plan(scope, command, cwd=cwd, shell=shell)
        if state is not None:
            state.cleanup()
        return argv

    def _prepare_stage_trees(self, scope: Any) -> tuple[Path | None, tuple[_StageTree, ...]]:
        """Create private copies for roots that cannot be safely bound RW."""

        target = Path(scope.canonical_root)
        target_full = self._target_full_mutation(scope)
        scratch_roots = self._roots(scope, "scratch_roots")
        writable_scope = str(getattr(scope, "workspace_access_level", "write")) == "write"
        # A general Enterprise harness scope always uses copy-on-write staging,
        # even when it grants full mutation of the private user workspace.
        # This keeps disk limits, write/delete separation and all-or-nothing
        # publication effective for arbitrary shell behavior.  Legacy
        # repository-only AgentRunScope calls retain their established direct
        # bind behavior when no trusted upper scope is present.
        force_staging = self._trusted_upper_scope(scope) is not None

        # Read-only scopes never need a staging copy.  For writable scopes,
        # every target lacking *both* full write and full delete authority is
        # staged so shell-side arbitrary mutations cannot bypass policy.
        stage_target = writable_scope and (force_staging or not target_full)
        stage_scratch: dict[Path, bool] = {}
        for root in scratch_roots:
            full = self._scratch_full_mutation(scope, root)
            # A missing root cannot be bind-mounted safely; an authorized
            # writable scratch gets an empty staged tree and is created on
            # publication instead.
            stage_scratch[root] = writable_scope and (
                force_staging or not full or not root.exists()
            )

        if not stage_target and not any(stage_scratch.values()):
            return None, ()

        stage_parent = Path(tempfile.mkdtemp(prefix="aoitalk-wsl-stage-"))
        trees: list[_StageTree] = []
        nested_scratch: list[str] = []
        for scratch in scratch_roots:
            if _is_within(scratch, target):
                try:
                    nested_scratch.append(scratch.relative_to(target).as_posix())
                except ValueError:
                    pass

        if stage_target:
            excludes = (".git", *nested_scratch)
            baseline = _snapshot_tree(
                target,
                excluded_prefixes=excludes,
                exclude_sensitive=force_staging,
            )
            stage_root = stage_parent / "workspace"
            _copy_tree(
                target,
                stage_root,
                excluded_prefixes=excludes,
                exclude_sensitive=force_staging,
            )
            # Leave a mountpoint for .git, including worktree ``.git`` files.
            git_metadata = target / ".git"
            if git_metadata.exists():
                if git_metadata.is_dir() and not _is_link_or_reparse(git_metadata):
                    (stage_root / ".git").mkdir(parents=True, exist_ok=True)
                elif git_metadata.is_file():
                    (stage_root / ".git").touch()
            trees.append(
                _StageTree(
                    target,
                    stage_root,
                    baseline,
                    tuple(excludes),
                    exclude_sensitive=force_staging,
                )
            )

        for index, scratch in enumerate(scratch_roots):
            if not stage_scratch.get(scratch):
                continue
            stage_root = stage_parent / "scratch" / str(index)
            if scratch.exists():
                if not scratch.is_dir():
                    raise WslBwrapError(f"scratch root must be a directory: {scratch}")
                baseline = _snapshot_tree(
                    scratch,
                    exclude_sensitive=force_staging,
                )
                _copy_tree(
                    scratch,
                    stage_root,
                    exclude_sensitive=force_staging,
                )
            else:
                stage_root.mkdir(parents=True, exist_ok=True)
                baseline = {}
            trees.append(
                _StageTree(
                    scratch,
                    stage_root,
                    baseline,
                    exclude_sensitive=force_staging,
                )
            )
        return stage_parent, tuple(trees)

    def _build_argv_and_plan(
        self,
        scope: Any,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        shell: str | None = None,
    ) -> tuple[list[str], _FinalizationState | None]:
        if not getattr(scope, "file_scoped", True):
            raise WslBwrapError("an AgentRunScope is required for the file-scoped backend")
        if not isinstance(command, str) or "\x00" in command:
            raise WslBwrapError("command must be a NUL-free string")
        if self._GIT_PUBLICATION_OR_DESTRUCTIVE.search(command):
            raise WslBwrapError(
                "worker Git publication/destructive commands are parent-controller only"
            )
        shell_name = str(shell or "auto").strip().lower()
        if shell_name in {"cmd", "powershell"}:
            raise WslBwrapError(
                f"shell={shell_name!r} is unavailable inside the POSIX WSL sandbox; use bash/auto"
            )
        self._require_available()
        network_flags = self._network_flags(scope)
        trusted_limits = self._trusted_resource_limits(scope)
        command_for_shell = self._resource_limited_command(scope, command)

        stage_parent: Path | None = None
        state: _FinalizationState | None = None
        try:
            stage_parent, trees = self._prepare_stage_trees(scope)
            stage_by_canonical = {_path_key(tree.canonical_root): tree for tree in trees}
            target = Path(scope.canonical_root)
            target_tree = stage_by_canonical.get(_path_key(target))
            target_source = target_tree.stage_root if target_tree else target
            translated_root = self.translate_windows_path(target_source)

            scratch_roots = self._roots(scope, "scratch_roots")
            scratch_aliases = self._scratch_aliases(scope)
            external_roots = self._external_read_roots(scope)
            external_aliases = self._external_aliases(scope, external_roots)
            for root in external_roots:
                if not root.exists():
                    raise WslBwrapError(f"external read root does not exist: {root}")
            workspace_cwd = self._cwd_mount_path(
                scope,
                cwd,
                scratch_aliases=scratch_aliases,
                external_aliases=external_aliases,
            )
            ephemeral_mounts: dict[str, Path] = {}
            monitor_roots: tuple[Path, ...] = ()
            if trusted_limits is not None:
                if stage_parent is None:
                    raise WslBwrapError(
                        "trusted harness execution requires staged ephemeral storage"
                    )
                ephemeral_base = stage_parent / "ephemeral"
                for name in ("tmp", "root", "mnt", "inputs", "scratch", "dev-shm"):
                    mount_root = ephemeral_base / name
                    mount_root.mkdir(parents=True, exist_ok=True)
                    ephemeral_mounts[name] = mount_root
                for alias in external_aliases.values():
                    (ephemeral_mounts["inputs"] / alias).mkdir(parents=True)
                for alias in scratch_aliases.values():
                    (ephemeral_mounts["scratch"] / alias).mkdir(parents=True)
                monitor_roots = (ephemeral_base,)

            git_metadata = target / ".git"
            translated_git = None
            if git_metadata.exists():
                # Do not let the explicit read-only .git overlay become a path
                # escape.  A worktree's .git file is fine; a symlink/junction
                # to another repository or host directory is denied first.
                try:
                    safe_git = scope.assert_read_allowed(git_metadata)
                except Exception as exc:
                    raise WslBwrapError(
                        f"repository .git metadata is outside the run scope: {git_metadata}"
                    ) from exc
                if _is_link_or_reparse(git_metadata):
                    raise WslBwrapError("repository .git metadata cannot be a symlink/reparse point")
                translated_git = self.translate_windows_path(safe_git)

            mount_flag = "--bind" if target_tree else self._scope_mount_option(scope)
            argv = [
            self.wsl_executable,
            "-d",
            self.distribution,
            "--",
            # Strip WSL-injected interop/display/host PATH variables before
            # bwrap becomes namespace PID 1.  bwrap --clearenv only affects
            # the final child; without this prefix /proc/1/environ leaks the
            # outer WSL environment to sandbox code.
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            self.bwrap_executable,
            "--clearenv",
            "--die-with-parent",
            # WSL distributions may be configured to launch as uid 0.  A
            # root process retaining CAP_SYS_ADMIN can remount a ro-bind
            # writable from inside its mount namespace, so capability drop is
            # mandatory even when the normal distribution user is unprivileged.
            "--cap-drop",
            "ALL",
            "--unshare-user",
            "--disable-userns",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--hostname",
            "aoitalk-sandbox",
            *network_flags,
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            *(
                [
                    item
                    for name, destination in (
                        ("tmp", "/tmp"),
                        ("root", "/root"),
                        ("mnt", "/mnt"),
                        ("inputs", "/inputs"),
                        ("scratch", "/scratch"),
                        # ``--dev /dev`` creates a writable tmpfs including
                        # /dev/shm.  Overlay it with host-backed ephemeral
                        # storage so the same aggregate disk monitor accounts
                        # for every general-purpose writable filesystem.
                        ("dev-shm", "/dev/shm"),
                    )
                    for item in (
                        "--bind",
                        self.translate_windows_path(ephemeral_mounts[name]),
                        destination,
                    )
                ]
                if ephemeral_mounts
                else [
                    "--tmpfs", "/tmp",
                    "--tmpfs", "/mnt",
                    "--dir", "/root",
                    "--dir", "/inputs",
                    "--dir", "/scratch",
                ]
            ),
            mount_flag,
            translated_root,
            self.workspace_mount,
            ]

            # Mount every scratch root at a deterministic path.  A staged root
            # is RW only inside the temporary namespace; direct RW is reserved
            # for roots with both write and delete authority.
            for scratch in scratch_roots:
                tree = stage_by_canonical.get(_path_key(scratch))
                source = tree.stage_root if tree else scratch
                if not source.exists():
                    raise WslBwrapError(f"scratch root does not exist: {scratch}")
                mode = "--bind" if tree or self._scratch_full_mutation(scope, scratch) else "--ro-bind"
                argv.extend([mode, self.translate_windows_path(source), f"/scratch/{scratch_aliases[scratch]}"])

            # Explicit external read grants are always read-only and are never
            # exposed through their host /mnt path.
            for external in external_roots:
                argv.extend(["--ro-bind", self.translate_windows_path(external), f"/inputs/{external_aliases[external]}"])
            argv.extend(
                self._external_sensitive_masks(external_roots, external_aliases)
            )

            argv.extend([
            *(
                ["--ro-bind", translated_git, f"{self.workspace_mount}/.git"]
                if translated_git
                else []
            ),
            "--chdir",
            workspace_cwd,
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            # Do not allow a scoped worker to invoke a Windows executable via
            # WSL interop.  Such an executable would leave the Linux mount
            # namespace and could mutate arbitrary host paths.
            "--setenv",
            "WSL_INTEROP",
            "",
            "--setenv",
            "WSLENV",
            "",
            "--setenv",
            "PYTHONIOENCODING",
            "utf-8",
            "--setenv",
            "PYTHONUNBUFFERED",
            "1",
            "--setenv",
            "PYTHON_DOTENV_DISABLED",
            "1",
            "--setenv",
            "HOME",
            "/root",
            # bash supplies the trusted ``ulimit -u`` primitive used by the
            # process-count capability. Debian's /bin/sh (dash) rejects -u,
            # which would silently leave that limit unenforced.
            "/bin/bash",
            "-lc",
            command_for_shell,
            ])
            if trees:
                max_disk = (
                    getattr(trusted_limits, "max_disk_bytes", None)
                    if trusted_limits is not None
                    else None
                )
                state = _FinalizationState(
                    self,
                    scope,
                    trees,
                    stage_parent=stage_parent,
                    monitor_roots=monitor_roots,
                    max_disk_bytes=(int(max_disk) if max_disk is not None else None),
                )
            return argv, state
        except Exception:
            if stage_parent is not None:
                shutil.rmtree(stage_parent, ignore_errors=True)
            raise

    @staticmethod
    def _default_env(
        base: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the already-sanitized parent environment for ``wsl.exe``."""

        from ..utils.subprocess_env import build_aoitalk_subprocess_env

        return build_aoitalk_subprocess_env(
            base=base,
            extra_env=dict(extra_env or {}),
        )

    @staticmethod
    def _entry_equal(left: _SnapshotEntry | None, right: _SnapshotEntry | None) -> bool:
        return left == right

    @staticmethod
    def _remove_node(path: Path) -> None:
        """Remove a node without ever following a link/reparse point."""

        if not path.exists() and not path.is_symlink():
            return
        if _is_link_or_reparse(path):
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _copy_node(source: Path, destination: Path) -> None:
        """Copy one validated regular node for journal/rollback."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(source):
            destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)

    @staticmethod
    def _current_entry(path: Path) -> _SnapshotEntry | None:
        try:
            return _snapshot_signature(path)
        except FileNotFoundError:
            return None

    def _collect_tree_diffs(self, tree: _StageTree) -> list[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]]:
        current = _snapshot_tree(
            tree.stage_root,
            excluded_prefixes=tree.excluded_prefixes,
            exclude_sensitive=tree.exclude_sensitive,
        )
        keys = sorted(set(tree.baseline) | set(current), key=lambda item: (item.count("/"), item))
        result: list[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]] = []
        for rel in keys:
            before = tree.baseline.get(rel)
            after = current.get(rel)
            if self._entry_equal(before, after):
                continue
            relative = Path(*PurePosixPath(rel).parts)
            canonical = tree.canonical_root / relative
            staged = tree.stage_root / relative if after is not None else None
            result.append((canonical, staged, before, after))
        return result

    def _validate_tree_diffs(
        self,
        scope: Any,
        tree: _StageTree,
        diffs: Sequence[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]],
    ) -> None:
        """Validate every diff before applying any host mutation."""

        for canonical, staged, before, after in diffs:
            # A stale canonical tree means another process changed state while
            # the command was running; fail closed rather than overwriting it.
            if self._current_entry(canonical) != before:
                raise WslBwrapError(f"canonical path changed during sandbox run: {canonical}")

            # Symlink/reparse publication is never permitted.  Existing links
            # may remain untouched, but any changed/added link is rejected.
            if (before is not None and before.kind in {"link", "other"}) or (
                after is not None and after.kind in {"link", "other"}
            ):
                raise WslBwrapError(f"symlink/reparse publication is not allowed: {canonical}")

            type_replacement = before is not None and after is not None and before.kind != after.kind
            if after is None or type_replacement:
                try:
                    # ``assert_delete_allowed`` evaluates canonical containment
                    # and catches a host-side symlink/reparse parent.
                    scope.assert_delete_allowed(canonical)
                except Exception as exc:
                    raise WslBwrapError(f"delete scope denied for {canonical}: {exc}") from exc
            if after is not None:
                try:
                    scope.assert_mutation_allowed(canonical, "write")
                except Exception as exc:
                    raise WslBwrapError(f"write scope denied for {canonical}: {exc}") from exc
                if staged is None or not staged.exists():
                    raise WslBwrapError(f"staged publication source disappeared: {canonical}")
                # Check every existing component in the destination path.  The
                # scope catches escaping links, while this explicit lstat check
                # rejects even links that happen to resolve inside the root.
                current = canonical
                parents: list[Path] = []
                while current != current.parent and _is_within(current, tree.canonical_root):
                    parents.append(current)
                    current = current.parent
                if any(_is_link_or_reparse(parent) for parent in parents):
                    raise WslBwrapError(f"symlink/reparse destination is not publishable: {canonical}")

    def _apply_tree_diffs(
        self,
        diffs: Sequence[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]],
    ) -> int:
        """Apply prevalidated diffs with a rollback journal."""

        if not diffs:
            return 0
        journal_root = Path(tempfile.mkdtemp(prefix="aoitalk-wsl-journal-"))
        backups: dict[Path, Path | None] = {}
        try:
            for index, (canonical, _staged, before, _after) in enumerate(diffs):
                if before is None:
                    backups[canonical] = None
                    continue
                backup = journal_root / str(index)
                self._copy_node(canonical, backup)
                backups[canonical] = backup

            # Remove deepest nodes first, then create directories and files.
            ordered = sorted(
                diffs,
                key=lambda item: (len(item[0].parts), item[0].as_posix()),
                reverse=True,
            )
            for canonical, staged, before, after in ordered:
                if after is None or (before is not None and after is not None and before.kind != after.kind):
                    self._remove_node(canonical)

            for canonical, staged, before, after in sorted(
                diffs,
                key=lambda item: (len(item[0].parts), item[0].as_posix()),
            ):
                if after is None:
                    continue
                canonical.parent.mkdir(parents=True, exist_ok=True)
                if after.kind == "dir":
                    canonical.mkdir(parents=True, exist_ok=True)
                    try:
                        os.chmod(canonical, after.mode)
                    except OSError:
                        pass
                elif after.kind == "file":
                    assert staged is not None
                    temp = canonical.with_name(f".{canonical.name}.aoitalk-{os.getpid()}-{threading.get_ident()}")
                    self._remove_node(temp)
                    shutil.copy2(staged, temp, follow_symlinks=False)
                    os.replace(temp, canonical)
                    try:
                        os.chmod(canonical, after.mode)
                    except OSError:
                        pass
                else:
                    raise WslBwrapError(f"unsupported staged node type: {canonical}")
            return len(diffs)
        except Exception:
            # Restore every touched path.  Rollback itself is best effort but
            # never follows a symlink; report failure to the caller if a node
            # cannot be restored.
            rollback_error: Exception | None = None
            for canonical in sorted(backups, key=lambda item: len(item.parts), reverse=True):
                try:
                    self._remove_node(canonical)
                    backup = backups[canonical]
                    if backup is not None and backup.exists():
                        self._copy_node(backup, canonical)
                except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                    rollback_error = rollback_error or exc
            if rollback_error:
                raise WslBwrapError(f"publication failed and rollback failed: {rollback_error}")
            raise
        finally:
            shutil.rmtree(journal_root, ignore_errors=True)

    def _finalize_staging(self, scope: Any, trees: Sequence[_StageTree]) -> dict[str, Any]:
        """Validate and atomically publish every staged tree, or publish none."""

        if not trees:
            return {"success": True, "published": 0}
        all_diffs: list[
            tuple[
                _StageTree,
                list[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]],
            ]
        ] = []
        with self._publication_lock:
            try:
                for tree in trees:
                    diffs = self._collect_tree_diffs(tree)
                    self._validate_tree_diffs(scope, tree, diffs)
                    all_diffs.append((tree, diffs))
                flattened: list[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]] = []
                seen_paths: set[str] = set()
                for _tree, diffs in all_diffs:
                    for operation in diffs:
                        key = _path_key(operation[0])
                        if key in seen_paths:
                            raise WslBwrapError(f"overlapping staged publication paths: {operation[0]}")
                        seen_paths.add(key)
                        flattened.append(operation)
                published = self._apply_all_tree_diffs(flattened)
            except Exception as exc:
                return {"success": False, "published": 0, "error": str(exc)}
        return {"success": True, "published": published}

    def _apply_all_tree_diffs(
        self,
        diffs: Sequence[tuple[Path, Path | None, _SnapshotEntry | None, _SnapshotEntry | None]],
    ) -> int:
        """Apply all tree operations under one journal and rollback boundary."""

        if not diffs:
            return 0
        journal_root = Path(tempfile.mkdtemp(prefix="aoitalk-wsl-journal-"))
        backups: dict[Path, Path | None] = {}
        try:
            for index, (canonical, _staged, before, _after) in enumerate(diffs):
                if before is None:
                    backups[canonical] = None
                else:
                    backup = journal_root / str(index)
                    self._copy_node(canonical, backup)
                    backups[canonical] = backup

            removals = [
                item
                for item in diffs
                if item[3] is None or (item[2] is not None and item[3] is not None and item[2].kind != item[3].kind)
            ]
            for canonical, _staged, _before, _after in sorted(
                removals, key=lambda item: (len(item[0].parts), item[0].as_posix()), reverse=True
            ):
                self._remove_node(canonical)

            for canonical, staged, _before, after in sorted(
                (item for item in diffs if item[3] is not None),
                key=lambda item: (len(item[0].parts), item[0].as_posix()),
            ):
                assert after is not None
                canonical.parent.mkdir(parents=True, exist_ok=True)
                if after.kind == "dir":
                    canonical.mkdir(parents=True, exist_ok=True)
                    try:
                        os.chmod(canonical, after.mode)
                    except OSError:
                        pass
                elif after.kind == "file":
                    if staged is None:
                        raise WslBwrapError(f"staged file source missing: {canonical}")
                    temp = canonical.with_name(
                        f".{canonical.name}.aoitalk-{os.getpid()}-{threading.get_ident()}"
                    )
                    self._remove_node(temp)
                    shutil.copy2(staged, temp, follow_symlinks=False)
                    os.replace(temp, canonical)
                    try:
                        os.chmod(canonical, after.mode)
                    except OSError:
                        pass
                else:
                    raise WslBwrapError(f"unsupported staged node type: {canonical}")
            return len(diffs)
        except Exception:
            rollback_error: Exception | None = None
            for canonical in sorted(backups, key=lambda item: len(item.parts), reverse=True):
                try:
                    self._remove_node(canonical)
                    backup = backups[canonical]
                    if backup is not None and backup.exists():
                        self._copy_node(backup, canonical)
                except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                    rollback_error = rollback_error or exc
            if rollback_error:
                raise WslBwrapError(f"publication failed and rollback failed: {rollback_error}")
            raise
        finally:
            shutil.rmtree(journal_root, ignore_errors=True)

    def spawn(
        self,
        scope: Any,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        shell: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        popen_kwargs: Mapping[str, Any] | None = None,
    ) -> subprocess.Popen:
        """Spawn a scoped command for callers that need stream/process control."""

        if timeout is None:
            limits = self._trusted_resource_limits(scope)
            if limits is not None:
                background_limit = getattr(limits, "max_background_seconds", None)
                timeout = float(
                    background_limit
                    if background_limit is not None
                    else getattr(limits, "timeout_seconds")
                )
        argv, state = self._build_argv_and_plan(scope, command, cwd=cwd, shell=shell)
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._default_env(base=env),
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if popen_kwargs:
            kwargs.update(dict(popen_kwargs))
        # ``BackgroundJobRegistry`` consumes bytes from stdin/stdout so it
        # can provide an interactive stream.  Popen rejects ``encoding`` and
        # ``errors`` when text mode is explicitly disabled; remove those
        # defaults rather than forcing every caller to duplicate the backend's
        # low-level details.
        if kwargs.get("text") is False or kwargs.get("universal_newlines") is False:
            kwargs.pop("encoding", None)
            kwargs.pop("errors", None)
        try:
            process = subprocess.Popen(argv, **kwargs)
            # Windows cannot deliver POSIX signals into the WSL PID
            # namespace.  ``taskkill /T`` without ``/F`` may therefore leave
            # a shell descendant alive long enough to mutate the workspace.
            # Mark this host wrapper so the shared process-tree terminator can
            # use an immediate forceful tree kill for the file-scoped lane.
            try:
                setattr(process, "_aoitalk_wsl_bwrap", True)
            except Exception:
                pass
            if state is not None:
                # Consumers of ``spawn`` (streaming/background) retain the
                # ordinary Popen object.  A daemon watcher carries the staging
                # capability until the wrapper exits and finalizes exactly
                # once, including timeout/stop paths.
                try:
                    setattr(process, "_aoitalk_finalization_state", state)
                    setattr(process, "_aoitalk_finalization_result", None)
                except Exception:
                    pass

                def _watch_and_finalize() -> None:
                    exit_code = None
                    try:
                        exit_code = process.wait()
                    except Exception:
                        # Even if wait is interrupted, publication is only
                        # attempted after the host wrapper is no longer alive.
                        state.cancel("sandbox process wait failed")
                    outcome = state.finalize(exit_code)
                    try:
                        setattr(process, "_aoitalk_finalization_result", dict(outcome))
                    except Exception:
                        pass

                watcher = threading.Thread(
                    target=_watch_and_finalize,
                    name="aoitalk-wsl-finalizer",
                    daemon=True,
                )
                watcher.start()
                if state.max_disk_bytes is not None:
                    def _enforce_disk_budget() -> None:
                        allowed = state.baseline_bytes + int(state.max_disk_bytes or 0)
                        while process.poll() is None:
                            try:
                                current = sum(
                                    _regular_tree_bytes(tree.stage_root)
                                    for tree in state.trees
                                ) + sum(
                                    _regular_tree_bytes(root)
                                    for root in state.monitor_roots
                                )
                            except Exception as exc:
                                state.cancel(f"sandbox disk usage could not be measured: {exc}")
                                _terminate_host_process_tree(process)
                                return
                            if current > allowed:
                                state.cancel(
                                    "sandbox disk growth exceeded trusted max_disk_bytes "
                                    f"({state.max_disk_bytes})"
                                )
                                _terminate_host_process_tree(process)
                                return
                            time.sleep(0.05)

                    disk_monitor = threading.Thread(
                        target=_enforce_disk_budget,
                        name="aoitalk-wsl-disk-limit",
                        daemon=True,
                    )
                    disk_monitor.start()
                if timeout is not None:
                    try:
                        deadline_seconds = max(float(timeout), 0.01)
                    except (TypeError, ValueError):
                        deadline_seconds = 0.01

                    def _enforce_deadline() -> None:
                        try:
                            process.wait(timeout=deadline_seconds)
                            return
                        except subprocess.TimeoutExpired:
                            pass
                        except Exception:
                            return
                        if process.poll() is None:
                            state.cancel(f"Command timed out after {deadline_seconds:g} seconds")
                            _terminate_host_process_tree(process)

                    timer = threading.Thread(
                        target=_enforce_deadline,
                        name="aoitalk-wsl-deadline",
                        daemon=True,
                    )
                    timer.start()
            return process
        except OSError as exc:
            if state is not None:
                state.cleanup()
            raise WslBwrapError(f"failed to spawn WSL sandbox: {exc}") from exc
        except Exception:
            if state is not None:
                state.cleanup()
            raise

    def run(
        self,
        scope: Any,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        shell: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        """Run a bounded command and kill the WSL wrapper on timeout."""

        effective_timeout = 120.0 if timeout is None else max(float(timeout), 0.01)
        limits = self._trusted_resource_limits(scope)
        if limits is not None:
            effective_timeout = min(
                effective_timeout,
                max(float(getattr(limits, "timeout_seconds", effective_timeout)), 0.01),
            )
        max_output_bytes = max(
            int(getattr(limits, "max_output_bytes", 32_768)) if limits is not None else 32_768,
            1,
        )
        started = time.monotonic()
        try:
            process = self.spawn(
                scope,
                command,
                cwd=cwd,
                shell=shell,
                timeout=effective_timeout,
                env=env,
                popen_kwargs={"text": False},
            )
        except WslBwrapError as exc:
            return SandboxResult(False, error_message=str(exc), duration_seconds=time.monotonic() - started)

        stdout_capture = _BoundedPipeCapture(max_output_bytes)
        stderr_capture = _BoundedPipeCapture(max_output_bytes)
        pump_threads: list[threading.Thread] = []
        for stream, capture in (
            (process.stdout, stdout_capture),
            (process.stderr, stderr_capture),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_drain_pipe_bounded,
                args=(stream, capture),
                name="aoitalk-wsl-output",
                daemon=True,
            )
            thread.start()
            pump_threads.append(thread)

        try:
            process.wait(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            # bwrap's --die-with-parent tears down the namespace descendants
            # when the wrapper exits.  Kill the Windows wsl.exe process and
            # drain its pipes before returning to avoid leaked handles.
            state = getattr(process, "_aoitalk_finalization_state", None)
            if state is not None:
                state.cancel(f"Command timed out after {effective_timeout:g} seconds")
            _terminate_host_process_tree(process)
            for thread in pump_threads:
                thread.join(timeout=5)
            stdout, stderr = stdout_capture.text(), stderr_capture.text()
            finalization = self._await_process_finalization(process)
            finalization_error = ""
            if finalization and not finalization.get("success", False):
                finalization_error = str(finalization.get("error", "staged publication denied"))
            return SandboxResult(
                False,
                stdout or "",
                stderr or "",
                return_code=process.returncode if process.returncode is not None else -1,
                timed_out=True,
                error_message=(
                    f"Command timed out after {effective_timeout:g} seconds"
                    + (f"; {finalization_error}" if finalization_error else "")
                ),
                duration_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            state = getattr(process, "_aoitalk_finalization_state", None)
            if state is not None:
                state.cancel(f"sandbox output capture failed: {exc}")
            _terminate_host_process_tree(process)
            return SandboxResult(
                False,
                error_message=f"WSL sandbox execution failed: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        for thread in pump_threads:
            thread.join(timeout=5)
        stdout, stderr = stdout_capture.text(), stderr_capture.text()

        finalization = self._await_process_finalization(process)
        finalization_error = ""
        if finalization and not finalization.get("success", False):
            finalization_error = str(finalization.get("error", "staged publication denied"))
        success = process.returncode == 0 and not finalization_error
        return SandboxResult(
            success,
            stdout or "",
            stderr or "",
            return_code=process.returncode,
            duration_seconds=time.monotonic() - started,
            error_message=(
                finalization_error
                or ((stderr or "").strip() if process.returncode else "")
            ),
        )

    @staticmethod
    def _await_process_finalization(process: subprocess.Popen) -> dict[str, Any] | None:
        state = getattr(process, "_aoitalk_finalization_state", None)
        if state is None:
            return None
        try:
            outcome = state.wait(timeout=10)
        except Exception:
            outcome = None
        if outcome is None:
            return {"success": False, "error": "staged publication finalization timed out"}
        return outcome


def get_wsl_bwrap_backend() -> WslBwrapBackend:
    """Return a fresh backend (configuration is immutable per invocation)."""

    return WslBwrapBackend()


__all__ = [
    "SandboxResult",
    "WslBwrapBackend",
    "WslBwrapError",
    "get_wsl_bwrap_backend",
]
