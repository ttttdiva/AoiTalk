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
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


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
        bwrap_executable: str = "bwrap",
        workspace_mount: str = "/workspace",
        # A cold WSL distribution can take several seconds to start on
        # Windows; keep this bounded but above the observed cold-start cost.
        translation_timeout_seconds: float = 30.0,
    ) -> None:
        self.distribution = str(distribution or "Debian")
        self.wsl_executable = str(wsl_executable or "wsl.exe")
        self.bwrap_executable = str(bwrap_executable or "bwrap")
        self.workspace_mount = str(workspace_mount or "/workspace").rstrip("/") or "/workspace"
        self.translation_timeout_seconds = max(float(translation_timeout_seconds), 0.1)

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
                    "wslpath",
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

    def _workspace_cwd(self, scope: Any, cwd: str | os.PathLike[str] | None) -> str:
        """Map a scope-approved Windows cwd to the mounted WSL workspace."""

        root = Path(scope.canonical_root)
        selected = root if cwd is None else Path(cwd)
        try:
            selected = Path(scope.assert_command_cwd_allowed(selected))
            relative = selected.relative_to(root)
        except Exception as exc:
            raise WslBwrapError(f"command cwd is outside the run scope: {cwd}") from exc
        if not relative.parts:
            return self.workspace_mount
        # Path.parts on Windows can contain drive/UNC anchors only when the
        # relative_to call above failed; PurePosixPath keeps the argv portable.
        return str(PurePosixPath(self.workspace_mount, *relative.parts))

    @staticmethod
    def _scope_mount_option(scope: Any) -> str:
        """Choose a safe mount mode for the requested mutation roots."""

        if str(getattr(scope, "workspace_access_level", "write")) != "write":
            return "--ro-bind"

        # A single RW bind cannot represent narrowed write/delete roots.  Do
        # not widen such a scope accidentally through the shell; callers can
        # use the path-aware file APIs for restricted roots instead.
        root = Path(scope.canonical_root)
        write_roots = tuple(
            Path(item) for item in getattr(scope, "write_roots", ()) or ()
        )
        delete_roots = tuple(
            Path(item) for item in getattr(scope, "delete_roots", ()) or ()
        )
        if not any(item == root for item in write_roots) or not any(
            item == root for item in delete_roots
        ):
            raise WslBwrapError(
                "file-scoped shell requires full target-root write/delete roots; "
                "use a narrower file API for restricted scopes"
            )
        return "--bind"

    def build_argv(
        self,
        scope: Any,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        shell: str | None = None,
    ) -> list[str]:
        """Build the complete WSL+bwrap argv for one scoped command."""

        if not getattr(scope, "file_scoped", True):
            raise WslBwrapError("an AgentRunScope is required for the file-scoped backend")
        if not isinstance(command, str) or "\x00" in command:
            raise WslBwrapError("command must be a NUL-free string")
        if self._GIT_PUBLICATION_OR_DESTRUCTIVE.search(command):
            raise WslBwrapError(
                "worker Git publication/destructive commands are parent-controller only"
            )
        if getattr(scope, "scratch_roots", ()):
            raise WslBwrapError(
                "file-scoped WSL backend requires scratch roots to be explicitly mounted"
            )
        shell_name = str(shell or "auto").strip().lower()
        if shell_name in {"cmd", "powershell"}:
            raise WslBwrapError(
                f"shell={shell_name!r} is unavailable inside the POSIX WSL sandbox; use bash/auto"
            )
        self._require_available()
        translated_root = self.translate_windows_path(scope.canonical_root)
        workspace_cwd = self._workspace_cwd(scope, cwd)
        git_metadata = scope.canonical_root / ".git"
        translated_git = None
        if git_metadata.exists():
            # Do not let the explicit read-only .git overlay become a path
            # escape.  A worktree's .git file is fine; a symlink/junction to
            # another repository or host directory must be denied before it
            # is translated and mounted.
            try:
                safe_git = scope.assert_read_allowed(git_metadata)
            except Exception as exc:
                raise WslBwrapError(
                    f"repository .git metadata is outside the run scope: {git_metadata}"
                ) from exc
            translated_git = self.translate_windows_path(safe_git)

        mount_flag = self._scope_mount_option(scope)
        return [
            self.wsl_executable,
            "-d",
            self.distribution,
            "--",
            self.bwrap_executable,
            "--clearenv",
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-net",
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
            "--tmpfs",
            "/tmp",
            "--dir",
            "/root",
            mount_flag,
            translated_root,
            self.workspace_mount,
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
            "/bin/sh",
            "-lc",
            command,
        ]

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

        argv = self.build_argv(scope, command, cwd=cwd, shell=shell)
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
        # ``timeout`` is accepted for adapter compatibility/documentation; the
        # process owner performs the actual bounded wait and termination.
        del timeout
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
            return process
        except OSError as exc:
            raise WslBwrapError(f"failed to spawn WSL sandbox: {exc}") from exc

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
        started = time.monotonic()
        try:
            process = self.spawn(
                scope,
                command,
                cwd=cwd,
                shell=shell,
                env=env,
            )
        except WslBwrapError as exc:
            return SandboxResult(False, error_message=str(exc), duration_seconds=time.monotonic() - started)

        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            # bwrap's --die-with-parent tears down the namespace descendants
            # when the wrapper exits.  Kill the Windows wsl.exe process and
            # drain its pipes before returning to avoid leaked handles.
            _terminate_host_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            return SandboxResult(
                False,
                stdout or "",
                stderr or "",
                return_code=process.returncode if process.returncode is not None else -1,
                timed_out=True,
                error_message=f"Command timed out after {effective_timeout:g} seconds",
                duration_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            return SandboxResult(
                False,
                error_message=f"WSL sandbox execution failed: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        return SandboxResult(
            process.returncode == 0,
            stdout or "",
            stderr or "",
            return_code=process.returncode,
            duration_seconds=time.monotonic() - started,
            error_message=(stderr or "").strip() if process.returncode else "",
        )


def get_wsl_bwrap_backend() -> WslBwrapBackend:
    """Return a fresh backend (configuration is immutable per invocation)."""

    return WslBwrapBackend()


__all__ = [
    "SandboxResult",
    "WslBwrapBackend",
    "WslBwrapError",
    "get_wsl_bwrap_backend",
]
