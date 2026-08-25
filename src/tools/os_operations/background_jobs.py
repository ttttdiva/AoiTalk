"""バックグラウンドコマンド実行のジョブ管理

サーバ起動・ビルド・長時間バッチのように前景実行では扱えないコマンドを
別プロセスで走らせ続け、あとから出力を読む / stdin を書く / 停止する、を提供する。

OpenAI Codex CLI の unified_exec（セッション付きコマンド実行）に相当する機能で、
以下を満たすように実装している:

- stdout / stderr はデーモンスレッドがリングバッファへ読み続ける（パイプ詰まり回避）
- 出力は `since_offset` 指定で差分読み出しできる
- プロセスは stdin=PIPE で起動し、対話的コマンドへ入力を送れる
- 同時実行数の上限を持つ（終了済みジョブは一覧に残るが上限には数えない）
- atexit で全ジョブを terminate → kill し、プロセスをリークさせない
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from .command_executor import (
    build_process_group_kwargs,
    build_shell_command,
    resolve_shell_name,
    terminate_process_tree,
)
from ...utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)

# 設定が読めなかった場合のフォールバック既定値
DEFAULT_MAX_BACKGROUND_JOBS = 8
DEFAULT_BACKGROUND_BUFFER_BYTES = 1024 * 1024


def _scope_path_fingerprint(path: Any) -> str:
    """Return a stable path value for a run-scope fingerprint.

    ``AgentRunScope`` already canonicalises all of its roots.  We still
    normalise here because this helper is intentionally usable with a small
    test double (and because a fingerprint must not depend on ``Path``'s
    repr implementation).
    """

    try:
        raw = os.fspath(path)
    except TypeError:
        raw = str(path)
    try:
        return os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(raw))))
    except (OSError, TypeError, ValueError):
        return os.path.normcase(os.path.normpath(str(raw)))


def compute_scope_fingerprint(scope: Any) -> str:
    """Compute the immutable identity of an :class:`AgentRunScope`.

    The run id is deliberately *not* part of this digest: it is stored as a
    separate owner capability.  The digest therefore catches a scope object
    that was accidentally reused with a different repository, identity, or
    path/access boundary even when the caller supplied the same run id.
    """

    if scope is None:
        raise ValueError("scope is required")

    def _paths(name: str) -> list[str]:
        values = getattr(scope, name, ()) or ()
        if isinstance(values, (str, os.PathLike)):
            values = (values,)
        return sorted(_scope_path_fingerprint(value) for value in values)

    root = getattr(scope, "canonical_root", getattr(scope, "target_root", None))
    repository_identity = getattr(scope, "repo_identity", None)
    if repository_identity is None:
        repository_identity = getattr(scope, "repository_identity", "")
    payload = {
        "version": 1,
        "canonical_root": _scope_path_fingerprint(root),
        "repository_identity": str(repository_identity or ""),
        "workspace_access_level": str(
            getattr(scope, "workspace_access_level", "write") or "write"
        ),
        "read_roots": _paths("read_roots"),
        "write_roots": _paths("write_roots"),
        "delete_roots": _paths("delete_roots"),
        "command_roots": _paths("command_roots"),
        "scratch_roots": _paths("scratch_roots"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


# Descriptive aliases are kept public so parent lifecycle services do not need
# to duplicate this identity calculation.
scope_fingerprint = compute_scope_fingerprint
run_scope_fingerprint = compute_scope_fingerprint


class BackgroundJobError(Exception):
    """バックグラウンドジョブ操作の失敗"""


class _RingBuffer:
    """上限付きの出力バッファ

    上限を超えた分は先頭から捨てる。捨てた分も含めた累計バイト数を保持し、
    呼び出し側が `since_offset` で差分だけを読めるようにする。
    """

    def __init__(self, max_bytes: int):
        self._max_bytes = max(int(max_bytes), 1024)
        self._data = bytearray()
        self._total_written = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._data.extend(chunk)
            self._total_written += len(chunk)
            overflow = len(self._data) - self._max_bytes
            if overflow > 0:
                del self._data[:overflow]

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written

    def read(self, since_offset: int = 0) -> Tuple[str, int, bool]:
        """指定オフセット以降のテキストを返す。

        Returns:
            Tuple[str, int, bool]: (テキスト, 次回に渡すオフセット, 取りこぼしがあったか)
        """
        with self._lock:
            total = self._total_written
            buffered = bytes(self._data)

        earliest = total - len(buffered)
        dropped = False
        start = since_offset
        if start < earliest:
            start = earliest
            dropped = since_offset > 0 or earliest > 0
        if start > total:
            start = total

        chunk = buffered[start - earliest:]
        return chunk.decode("utf-8", errors="replace"), total, dropped


@dataclass
class BackgroundJob:
    """バックグラウンド実行中の 1 ジョブ"""

    _OWNER_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"owner_run_id", "repository_identity", "scope_fingerprint"}
    )

    job_id: str
    command: str
    shell: str
    cwd: Optional[str]
    process: subprocess.Popen
    started_at: datetime
    stdout_buffer: _RingBuffer
    stderr_buffer: _RingBuffer
    exit_code: Optional[int] = None
    status: str = "running"  # running | exited | killed
    threads: List[threading.Thread] = field(default_factory=list)
    # These fields are a capability captured at process creation.  They are
    # intentionally separate from mutable lifecycle state (status/exit_code)
    # and assignments after construction are rejected by ``__setattr__``.
    owner_run_id: Optional[str] = None
    repository_identity: Optional[str] = None
    scope_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        owner_run_id = str(self.owner_run_id or "").strip() or None
        repository_identity = str(self.repository_identity or "").strip() or None
        scope_fingerprint = str(self.scope_fingerprint or "").strip() or None
        # A legacy/unscoped job has no owner metadata at all.  A scoped job
        # must carry the complete tuple so a caller cannot accidentally turn a
        # partially populated record into an access bypass.
        if owner_run_id is None:
            repository_identity = None
            scope_fingerprint = None
        elif not repository_identity or not scope_fingerprint:
            raise ValueError(
                "scoped background jobs require owner_run_id, repository_identity, "
                "and scope_fingerprint"
            )
        object.__setattr__(self, "owner_run_id", owner_run_id)
        object.__setattr__(self, "repository_identity", repository_identity)
        object.__setattr__(self, "scope_fingerprint", scope_fingerprint)

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep owner identity immutable while allowing lifecycle updates."""

        if name in self._OWNER_FIELDS and name in self.__dict__:
            current = self.__dict__[name]
            if current != value:
                raise AttributeError(f"{name} is immutable for a BackgroundJob")
        object.__setattr__(self, name, value)

    def to_summary(self) -> Dict[str, Any]:
        """一覧表示用の要約辞書を返す"""
        return {
            "job_id": self.job_id,
            "command": self.command,
            "shell": self.shell,
            "cwd": self.cwd,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "exit_code": self.exit_code,
            "owner_run_id": self.owner_run_id,
            "repository_identity": self.repository_identity,
            "scope_fingerprint": self.scope_fingerprint,
        }


class BackgroundJobRegistry:
    """バックグラウンドジョブの登録簿"""

    def __init__(
        self,
        max_jobs: int = DEFAULT_MAX_BACKGROUND_JOBS,
        buffer_bytes: int = DEFAULT_BACKGROUND_BUFFER_BYTES,
    ):
        self.max_jobs = max(int(max_jobs), 1)
        self.buffer_bytes = max(int(buffer_bytes), 4096)
        self._jobs: Dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()

    # --- 内部ユーティリティ ---

    @staticmethod
    def _current_scope() -> Any | None:
        """Return the run scope bound to this call, if any."""

        try:
            from ...security.agent_run_scope import get_current_run_scope

            return get_current_run_scope()
        except ImportError:  # pragma: no cover - defensive for stripped builds
            return None

    @classmethod
    def _resolve_scope(cls, scope: Any | None = None) -> Any | None:
        """Resolve an optional explicit scope without allowing a mismatch.

        Parent lifecycle code may pass the immutable scope after its request
        context has been reset.  When a scope is already bound, however, an
        explicit different scope is rejected rather than becoming a way to
        operate on another run's process.
        """

        current = cls._current_scope()
        if scope is not None and current is not None:
            try:
                same = (
                    str(getattr(scope, "run_id", ""))
                    == str(getattr(current, "run_id", ""))
                    and str(getattr(scope, "repo_identity", ""))
                    == str(getattr(current, "repo_identity", ""))
                    and compute_scope_fingerprint(scope)
                    == compute_scope_fingerprint(current)
                )
            except Exception:
                same = False
            if not same:
                raise BackgroundJobError(
                    "explicit AgentRunScope does not match the current run scope"
                )
        return scope if scope is not None else current

    @staticmethod
    def _scope_metadata(scope: Any) -> tuple[str, str, str]:
        """Return the owner tuple captured on every scoped job."""

        owner_run_id = str(getattr(scope, "run_id", "") or "").strip()
        repository_identity = str(
            getattr(scope, "repo_identity", None)
            or getattr(scope, "repository_identity", "")
            or ""
        ).strip()
        if not owner_run_id or not repository_identity:
            raise BackgroundJobError(
                "AgentRunScope must have a run_id and repository identity"
            )
        return owner_run_id, repository_identity, compute_scope_fingerprint(scope)

    @classmethod
    def _job_matches_scope(cls, job: BackgroundJob, scope: Any | None) -> bool:
        """Return whether *scope* owns *job*.

        Legacy unscoped jobs remain visible only to unbound legacy callers.
        Once an AgentRunScope is active, unscoped host jobs are intentionally
        hidden as well: otherwise a scoped worker could attach to a process
        created by another run before the boundary was enabled.  Scoped jobs
        are fail-closed on any owner tuple mismatch.
        """

        if job.owner_run_id is None:
            return scope is None
        if scope is None:
            return False
        try:
            owner_run_id, repository_identity, fingerprint = cls._scope_metadata(scope)
        except BackgroundJobError:
            return False
        return (
            job.owner_run_id == owner_run_id
            and job.repository_identity == repository_identity
            and job.scope_fingerprint == fingerprint
        )

    @classmethod
    def _assert_job_access(cls, job: BackgroundJob, scope: Any | None) -> None:
        if cls._job_matches_scope(job, scope):
            return
        raise BackgroundJobError(
            f"ジョブ {job.job_id} は現在のAgentRunScopeから利用できません。"
        )

    def _refresh(self, job: BackgroundJob) -> None:
        """プロセスの終了状態をジョブへ反映する"""
        if job.status != "running":
            return
        code = job.process.poll()
        if code is not None:
            job.exit_code = code
            job.status = "exited"

    def _get(self, job_id: str, *, scope: Any | None = None) -> BackgroundJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BackgroundJobError(f"ジョブが見つかりません: {job_id}")
        resolved_scope = self._resolve_scope(scope)
        self._assert_job_access(job, resolved_scope)
        self._refresh(job)
        return job

    def _running_count(self) -> int:
        count = 0
        for job in list(self._jobs.values()):
            self._refresh(job)
            if job.status in {"running", "termination_failed"}:
                count += 1
        return count

    def _pump(self, stream, buffer: _RingBuffer) -> None:
        """子プロセスの出力をリングバッファへ読み続ける（デーモンスレッド）

        read() はバッファが埋まるまでブロックしてしまい、実行中のジョブの
        出力をリアルタイムに読めない。read1() で「今読める分」を取り出す。
        """
        read_some = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                chunk = read_some(4096)
                if not chunk:
                    break
                buffer.append(chunk)
        except Exception:  # ストリームが閉じられた場合など
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    # --- 公開 API ---

    def start(
        self,
        command: str,
        cwd: Optional[str] = None,
        shell: Optional[str] = None,
    ) -> str:
        """バックグラウンドでコマンドを起動し job_id を返す。

        Raises:
            BackgroundJobError: 同時実行数の上限超過や起動失敗
        """
        try:
            from ...security.agent_run_scope import RunScopeViolation, get_current_run_scope
        except ImportError:  # pragma: no cover - defensive for stripped builds
            RunScopeViolation = None  # type: ignore[assignment]
            get_current_run_scope = lambda: None  # type: ignore[assignment]

        scope = get_current_run_scope()
        scoped_owner_metadata: tuple[str, str, str] | None = None
        if scope is not None:
            # Validate and capture the complete owner tuple before spawning.
            # In particular, an empty/malformed run_id must fail closed rather
            # than being normalised into the legacy ``owner_run_id=None`` path.
            scoped_owner_metadata = self._scope_metadata(scope)
        scoped_backend = None
        if scope is not None:
            try:
                cwd = str(scope.assert_command_cwd_allowed(cwd))
            except RunScopeViolation as exc:
                raise BackgroundJobError(str(exc)) from exc
            try:
                from ...security.wsl_bwrap_backend import (
                    WslBwrapError,
                    get_wsl_bwrap_backend,
                )
            except ImportError as exc:  # pragma: no cover - stripped builds
                raise BackgroundJobError(
                    f"file-scoped WSL2/bubblewrap backend is unavailable: {exc}"
                ) from exc
            backend = get_wsl_bwrap_backend()
            try:
                if not getattr(backend, "file_scoped", False):
                    raise BackgroundJobError(
                        "configured command backend is not file-scoped"
                    )
                if not backend.is_available():
                    raise BackgroundJobError(
                        "file-scoped WSL2/bubblewrap backend is unavailable; "
                        "run-scoped background shell execution was denied"
                    )
            except WslBwrapError as exc:
                raise BackgroundJobError(str(exc)) from exc
            scoped_backend = (scope, backend)
            # ``WslBwrapBackend`` always launches POSIX ``/bin/sh``.  Keep a
            # stable label in summaries without resolving a host shell that
            # is intentionally unavailable inside the namespace.
            shell_name = (
                "bash" if shell is None or str(shell).strip().lower() == "auto"
                else str(shell).strip().lower()
            )
            argv = None
        else:
            try:
                shell_name = resolve_shell_name(shell)
                argv = build_shell_command(command, shell)
            except ValueError as e:
                raise BackgroundJobError(str(e)) from e

            if cwd is not None and not os.path.isdir(cwd):
                raise BackgroundJobError(f"作業ディレクトリが存在しません: {cwd}")

        with self._lock:
            running = self._running_count()
            if running >= self.max_jobs:
                raise BackgroundJobError(
                    f"バックグラウンドジョブの同時実行数が上限 ({self.max_jobs}) に達しています。"
                    "stop_command で不要なジョブを停止してください。"
                )

        env = build_aoitalk_subprocess_env()
        env["PYTHONUNBUFFERED"] = "1"

        # 停止時にプロセスツリーごと確実に殺せるよう、独立したグループで起動する
        popen_kwargs: Dict[str, Any] = build_process_group_kwargs()

        try:
            if scoped_backend is not None:
                scoped_scope, backend = scoped_backend
                process = backend.spawn(
                    scoped_scope,
                    command,
                    cwd=cwd,
                    shell=shell,
                    env=env,
                    popen_kwargs={
                        **popen_kwargs,
                        "stdin": subprocess.PIPE,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": False,
                    },
                )
            else:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    **popen_kwargs,
                )
        except FileNotFoundError as e:
            executable = argv[0] if argv else "wsl.exe"
            raise BackgroundJobError(f"シェルが見つかりません: {executable}") from e
        except Exception as e:
            raise BackgroundJobError(f"バックグラウンド起動に失敗しました: {e}") from e

        job = BackgroundJob(
            job_id=uuid.uuid4().hex[:8],
            command=command,
            shell=shell_name,
            cwd=cwd,
            process=process,
            started_at=datetime.now(),
            stdout_buffer=_RingBuffer(self.buffer_bytes),
            stderr_buffer=_RingBuffer(self.buffer_bytes),
            **(
                {
                    "owner_run_id": scoped_owner_metadata[0],
                    "repository_identity": scoped_owner_metadata[1],
                    "scope_fingerprint": scoped_owner_metadata[2],
                }
                if scoped_backend is not None and scoped_owner_metadata is not None
                else {}
            ),
        )

        for stream, buffer in ((process.stdout, job.stdout_buffer), (process.stderr, job.stderr_buffer)):
            thread = threading.Thread(
                target=self._pump, args=(stream, buffer), daemon=True
            )
            thread.start()
            job.threads.append(thread)

        with self._lock:
            self._jobs[job.job_id] = job

        logger.info(f"バックグラウンドジョブ開始: {job.job_id} ({command[:80]})")
        return job.job_id

    def read(
        self,
        job_id: str,
        max_output_bytes: int = 8192,
        since_offset: int = 0,
        *,
        scope: Any | None = None,
    ) -> Dict[str, Any]:
        """ジョブの現在までの出力と状態を返す。"""
        from ..output_truncation import truncate_middle

        job = self._get(job_id, scope=scope)
        stdout_text, stdout_offset, stdout_dropped = job.stdout_buffer.read(since_offset)
        stderr_text, stderr_offset, stderr_dropped = job.stderr_buffer.read(since_offset)

        stdout_trimmed = truncate_middle(stdout_text, max_output_bytes)
        stderr_trimmed = truncate_middle(stderr_text, max_output_bytes)

        # execute_command と同じ `output` キーでも読めるようにする。
        # 前景とバックグラウンドでキー名が違うとモデルが取りこぼす。
        header = [f"Status: {job.status}"]
        if job.exit_code is not None:
            header.append(f"Exit code: {job.exit_code}")
        if stdout_trimmed.truncated or stderr_trimmed.truncated:
            header.append(
                "Total output lines: "
                f"{stdout_trimmed.original_lines + stderr_trimmed.original_lines}"
            )
        header.append("Output:")
        body = stdout_trimmed.text
        if stderr_trimmed.text.strip():
            if body and not body.endswith("\n"):
                body += "\n"
            body += f"Stderr:\n{stderr_trimmed.text}"

        return {
            "job_id": job.job_id,
            "command": job.command,
            "status": job.status,
            "exit_code": job.exit_code,
            "output": "\n".join(header) + "\n" + body,
            "stdout": stdout_trimmed.text,
            "stderr": stderr_trimmed.text,
            "truncated": stdout_trimmed.truncated or stderr_trimmed.truncated,
            "next_offset": max(stdout_offset, stderr_offset),
            "buffer_overflowed": stdout_dropped or stderr_dropped,
            "started_at": job.started_at.isoformat(),
        }

    def write_stdin(
        self,
        job_id: str,
        text: str,
        *,
        scope: Any | None = None,
    ) -> Dict[str, Any]:
        """実行中プロセスの stdin へ書き込む。"""
        job = self._get(job_id, scope=scope)
        if job.status != "running":
            raise BackgroundJobError(
                f"ジョブ {job_id} は既に終了しています (status={job.status})。"
            )
        if job.process.stdin is None or job.process.stdin.closed:
            raise BackgroundJobError(f"ジョブ {job_id} の標準入力は利用できません。")

        payload = text if text.endswith("\n") else text + "\n"
        try:
            job.process.stdin.write(payload.encode("utf-8"))
            job.process.stdin.flush()
        except Exception as e:
            raise BackgroundJobError(f"標準入力への書き込みに失敗しました: {e}") from e

        return {"job_id": job.job_id, "written_chars": len(payload)}

    def _stop_job(self, job: BackgroundJob) -> Dict[str, Any]:
        """Stop one job without doing an ownership lookup."""

        self._refresh(job)
        if job.status != "running":
            return {
                "job_id": job.job_id,
                "status": job.status,
                "exit_code": job.exit_code,
                "message": "ジョブは既に終了しています。",
            }

        termination_error: Exception | None = None
        try:
            terminate_process_tree(job.process)
        except Exception as exc:
            termination_error = exc

        # A best-effort tree terminator is not a security proof.  Verify that
        # the host wrapper has actually exited before marking the job drained;
        # otherwise a surviving WSL child could retain the repository mount.
        try:
            exit_code = job.process.poll()
            if exit_code is None:
                kill = getattr(job.process, "kill", None)
                if callable(kill):
                    kill()
                wait = getattr(job.process, "wait", None)
                if callable(wait):
                    try:
                        wait(timeout=5)
                    except TypeError:
                        wait()
                    except Exception:
                        pass
                exit_code = job.process.poll()
        except Exception as exc:
            termination_error = termination_error or exc
            exit_code = None

        if exit_code is None:
            job.status = "termination_failed"
            raise BackgroundJobError(
                f"ジョブ {job.job_id} のプロセス終了を確認できませんでした"
                + (f": {termination_error}" if termination_error else "")
            )
        job.exit_code = exit_code
        job.status = "killed"
        try:
            if job.process.stdin and not job.process.stdin.closed:
                job.process.stdin.close()
        except Exception:
            pass
        # Pump threads own stdout/stderr and close each stream in their
        # ``finally`` block.  Give them a short chance to release handles,
        # without allowing a broken child pipe to block run shutdown.
        for thread in job.threads:
            if thread.is_alive():
                thread.join(timeout=0.2)

        logger.info(f"バックグラウンドジョブ停止: {job.job_id}")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "exit_code": job.exit_code,
            "message": "ジョブを停止しました。",
        }

    def stop(
        self,
        job_id: str,
        *,
        scope: Any | None = None,
    ) -> Dict[str, Any]:
        """ジョブを terminate → kill で停止する。"""
        job = self._get(job_id, scope=scope)
        return self._stop_job(job)

    def list_jobs(self, scope: Any | None = None) -> List[Dict[str, Any]]:
        """全ジョブ（終了済み含む）の要約一覧を返す。"""
        resolved_scope = self._resolve_scope(scope)
        with self._lock:
            jobs = list(self._jobs.values())
        summaries = []
        for job in jobs:
            # A run must not even learn that another run's job exists.  The
            # owner filter therefore happens before building a summary.
            if not self._job_matches_scope(job, resolved_scope):
                continue
            self._refresh(job)
            summaries.append(job.to_summary())
        summaries.sort(key=lambda item: item["started_at"])
        return summaries

    def active_scoped_jobs(self, scope: Any | None = None) -> int:
        """Return the number of running jobs owned by one run scope."""

        resolved_scope = self._resolve_scope(scope)
        if resolved_scope is None:
            # Do not turn an unbound call into a global process inventory.
            return 0
        with self._lock:
            jobs = list(self._jobs.values())
        count = 0
        for job in jobs:
            if job.owner_run_id is None or not self._job_matches_scope(job, resolved_scope):
                continue
            self._refresh(job)
            if job.status in {"running", "termination_failed"}:
                count += 1
        return count

    active_scoped_job_count = active_scoped_jobs

    def preflight_scoped_jobs(self, scope: Any | None = None) -> Dict[str, Any]:
        """Return a read-only publication preflight for one run's jobs."""

        resolved_scope = self._resolve_scope(scope)
        active_count = self.active_scoped_jobs(resolved_scope)
        owner_run_id = None
        repository_identity = None
        fingerprint = None
        if resolved_scope is not None:
            owner_run_id, repository_identity, fingerprint = self._scope_metadata(
                resolved_scope
            )
        return {
            "allowed": active_count == 0,
            "ready": active_count == 0,
            "active_count": active_count,
            "owner_run_id": owner_run_id,
            "repository_identity": repository_identity,
            "scope_fingerprint": fingerprint,
            "reason": (
                "no active scoped background jobs"
                if active_count == 0
                else f"{active_count} scoped background job(s) are still running"
            ),
        }

    def assert_no_active_scoped_jobs(self, scope: Any | None = None) -> Dict[str, Any]:
        """Fail closed when publication is attempted with a running job."""

        result = self.preflight_scoped_jobs(scope)
        if not result["allowed"]:
            raise BackgroundJobError(
                f"Git publication is blocked until scoped background jobs stop "
                f"(active_count={result['active_count']}, "
                f"owner_run_id={result['owner_run_id']})"
            )
        return result

    assert_scoped_jobs_drained = assert_no_active_scoped_jobs

    def close_scoped_jobs(
        self,
        scope: Any | str | None = None,
        *,
        owner_run_id: str | None = None,
        repository_identity: str | None = None,
        scope_fingerprint: str | None = None,
        remove: bool = False,
    ) -> List[Dict[str, Any]]:
        """Terminate and optionally remove all jobs owned by one run.

        A parent may call this with the immutable scope after the child
        context has been reset, or with the complete owner tuple.  Supplying
        only a run id is supported for the trusted parent lifecycle hook; when
        the full tuple is available it is also checked to avoid closing a
        reused/mismatched scope accidentally.
        """

        # ``close_scoped_jobs(owner_run_id)`` is the compact parent lifecycle
        # spelling.  Keep the first positional parameter useful for both that
        # form and the richer ``close_scoped_jobs(scope)`` form.
        if scope is not None and not hasattr(scope, "run_id"):
            if owner_run_id is not None:
                raise BackgroundJobError(
                    "close_scoped_jobs received both positional and keyword owner_run_id"
                )
            owner_run_id = str(scope)
            scope = None

        resolved_scope = self._resolve_scope(scope)
        if resolved_scope is not None:
            expected_owner, expected_identity, expected_fingerprint = self._scope_metadata(
                resolved_scope
            )
            supplied_owner = str(owner_run_id or "").strip() or None
            supplied_identity = str(repository_identity or "").strip() or None
            supplied_fingerprint = str(scope_fingerprint or "").strip() or None
            if (
                supplied_owner is not None
                and supplied_owner != expected_owner
            ) or (
                supplied_identity is not None
                and supplied_identity != expected_identity
            ) or (
                supplied_fingerprint is not None
                and supplied_fingerprint != expected_fingerprint
            ):
                raise BackgroundJobError("close_scoped_jobs owner metadata does not match scope")
            owner_run_id, repository_identity, scope_fingerprint = (
                expected_owner,
                expected_identity,
                expected_fingerprint,
            )
        else:
            owner_run_id = str(owner_run_id or "").strip() or None
            repository_identity = str(repository_identity or "").strip() or None
            scope_fingerprint = str(scope_fingerprint or "").strip() or None
            if not owner_run_id:
                raise BackgroundJobError(
                    "close_scoped_jobs requires an AgentRunScope or owner_run_id"
                )

        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if (
                    job.owner_run_id == owner_run_id
                    and (
                        repository_identity is None
                        or job.repository_identity == repository_identity
                    )
                    and (
                        scope_fingerprint is None
                        or job.scope_fingerprint == scope_fingerprint
                    )
                )
            ]

        results: List[Dict[str, Any]] = []
        for job in jobs:
            result = self._stop_job(job)
            result.update(
                {
                    "owner_run_id": job.owner_run_id,
                    "repository_identity": job.repository_identity,
                    "scope_fingerprint": job.scope_fingerprint,
                }
            )
            results.append(result)

        if remove and jobs:
            with self._lock:
                for job in jobs:
                    self._jobs.pop(job.job_id, None)
        return results

    close_owner_jobs = close_scoped_jobs
    cleanup_scoped_jobs = close_scoped_jobs

    def shutdown(self) -> None:
        """全ジョブを停止する（atexit から呼ばれる）。"""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            try:
                self._stop_job(job)
            except Exception:
                pass


# --- グローバルインスタンス ---

_registry: Optional[BackgroundJobRegistry] = None
_registry_lock = threading.Lock()


def _load_background_config() -> Dict[str, Any]:
    """config から os_operations.command 設定を読む（失敗時は既定値）。"""
    try:
        from ...config import Config

        config = Config()
        command_config = config.get("os_operations", {}).get("command", {}) or {}
    except Exception as e:  # 設定が読めなくても動作させる
        logger.warning(f"バックグラウンド設定の読み込みに失敗しました: {e}")
        command_config = {}

    return {
        "max_background_jobs": int(
            command_config.get("max_background_jobs", DEFAULT_MAX_BACKGROUND_JOBS)
        ),
        "background_buffer_bytes": int(
            command_config.get("background_buffer_bytes", DEFAULT_BACKGROUND_BUFFER_BYTES)
        ),
    }


def get_background_job_registry() -> BackgroundJobRegistry:
    """グローバルな BackgroundJobRegistry を取得する（遅延生成）。"""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            settings = _load_background_config()
            _registry = BackgroundJobRegistry(
                max_jobs=settings["max_background_jobs"],
                buffer_bytes=settings["background_buffer_bytes"],
            )
    return _registry


@atexit.register
def _shutdown_background_jobs() -> None:
    """プロセス終了時に残ったバックグラウンドジョブを後始末する。"""
    if _registry is not None:
        _registry.shutdown()
