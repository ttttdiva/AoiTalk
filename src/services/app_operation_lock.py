"""Cross-process serialization for destructive App/Project operations.

The API and background runner normally share one event loop, but deployments
may start multiple workers.  An asyncio lock alone would leave the shared App
workspace and Git repository open to cross-worker races, so each logical lock
also holds an OS file lock under ``workspaces/.locks``.

設計上の要点:

* 再試行するのは「他者がロックを保持している」種類のエラーだけ。ENOENT /
  ENOTDIR / ENAMETOOLONG などの恒久的エラーは即座に送出して無限リトライを避ける。
* 獲得待ちには必ず上限（``DEFAULT_ACQUIRE_TIMEOUT``）があり、超過時は
  ``AppOperationLockTimeout`` で失敗する。
* 獲得途中にキャンセルされても、ワーカースレッドが後から掴んだ file handle /
  OS ロックを必ず解放する（二重キャンセルでも取りこぼさない）。
* ``release()`` は獲得したタスク自身からの呼び出しだけを受け付け、他タスクが
  保持中のロックを誤って解放しない。
* lock path は ``resolve()`` + ``normcase()`` で正規化してからレジストリの
  キーにするため、相対パス・末尾スラッシュ・大文字小文字の違いで別ロックに
  分裂しない。
"""

from __future__ import annotations

import asyncio
import errno
import functools
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from .app_storage import get_workspaces_root

logger = logging.getLogger(__name__)

#: ロック獲得を待つ既定の上限秒数。無限待ちを防ぐための安全弁。
DEFAULT_ACQUIRE_TIMEOUT = 300.0

#: 既定 timeout を上書きする環境変数キー（``inf`` を指定すると無限待ち）。
ACQUIRE_TIMEOUT_ENV_KEY = "AOITALK_APP_LOCK_TIMEOUT"

#: OS ロック再試行の間隔（秒）。
_POLL_INTERVAL = 0.05

#: ``asyncio.wait_for(..., 0)`` は待機せず必ず TimeoutError になるため、
#: ``timeout=0`` でも最低 1 回は獲得を試みられるよう下限を設ける。
_MIN_LOCAL_WAIT = 0.001


class AppOperationLockError(RuntimeError):
    """App/Project operation lock の基底例外。

    ``TimeoutError`` は ``OSError`` の派生であり、呼び出し元の
    ``except OSError`` に巻き込まれると workspace I/O 由来のエラーと
    区別できなくなるため、意図的に ``RuntimeError`` 系にしている。
    """


class AppOperationLockTimeout(AppOperationLockError):
    """制限時間内にロックを獲得できなかった場合に送出する。"""


class _LockAcquireCancelled(Exception):
    """Internal signal from the file-lock worker when its waiter is cancelled."""


class _LockAcquireTimedOut(Exception):
    """Internal signal from the file-lock worker when the deadline expired."""


def _errno_set(*names: str) -> frozenset[int]:
    return frozenset(
        value for value in (getattr(errno, name, None) for name in names) if value is not None
    )


# ERROR_SHARING_VIOLATION(32) / ERROR_LOCK_VIOLATION(33) / ERROR_LOCK_FAILED(167)
_WINDOWS_CONTENTION_WINERRORS = frozenset({32, 33, 167})

# msvcrt.locking() は CRT の errno しか設定しない（winerror は None）。
# LK_NBLCK が拒否されたときは EACCES、LK_LOCK が再試行上限に達すると EDEADLOCK。
_WINDOWS_CONTENTION_ERRNOS = _errno_set(
    "EACCES", "EDEADLK", "EDEADLOCK", "EAGAIN", "EWOULDBLOCK"
)

_POSIX_CONTENTION_ERRNOS = _errno_set("EAGAIN", "EWOULDBLOCK")


def _is_contention_error(exc: OSError) -> bool:
    """ロック要求が「他者が保持中」で拒否されたときだけ True を返す。

    ここで判定するのはロック要求（``msvcrt.locking`` / ``fcntl.flock``）が
    投げた例外だけで、``open()`` や ``mkdir()`` のエラーは対象外。つまり本当の
    権限不足・パス不正はロック要求に到達する前に送出されるため、Windows の
    EACCES を競合として扱っても恒久的エラーを握り潰すことはない。
    """

    if os.name == "nt":
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            return winerror in _WINDOWS_CONTENTION_WINERRORS
        return exc.errno in _WINDOWS_CONTENTION_ERRNOS
    if isinstance(exc, BlockingIOError):
        return True
    return exc.errno in _POSIX_CONTENTION_ERRNOS


def _resolve_timeout(timeout: float | None) -> float:
    """呼び出し引数 → 環境変数 → 既定値 の順で待ち時間上限を決める。"""

    if timeout is not None:
        if timeout < 0:
            raise ValueError("timeout は 0 以上で指定してください")
        return float(timeout)

    raw = os.environ.get(ACQUIRE_TIMEOUT_ENV_KEY)
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning(
                "%s の値 %r を解釈できないため既定 timeout を使用します",
                ACQUIRE_TIMEOUT_ENV_KEY,
                raw,
            )
        else:
            if parsed >= 0:
                return parsed
            logger.warning(
                "%s に負値 %r が設定されているため既定 timeout を使用します",
                ACQUIRE_TIMEOUT_ENV_KEY,
                raw,
            )
    return DEFAULT_ACQUIRE_TIMEOUT


class _AcquireRequest:
    """ワーカースレッドと獲得側タスクが共有する 1 回分の獲得要求。"""

    __slots__ = ("cancel_event", "deadline", "abandoned", "disposed")

    def __init__(self, deadline: float | None) -> None:
        self.cancel_event = threading.Event()
        self.deadline = deadline
        self.abandoned = False
        self.disposed = False

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()


class AppOperationLock:
    """1 論理ロック = asyncio ロック + OS file ロック。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local: asyncio.Lock | None = None
        self._local_loop: asyncio.AbstractEventLoop | None = None
        self._handle: BinaryIO | None = None
        self._owner: tuple[asyncio.AbstractEventLoop, "asyncio.Task | None"] | None = None

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def locked(self) -> bool:
        """OS ロックを保持中かどうか。"""

        return self._handle is not None

    def _get_local(self) -> asyncio.Lock:
        """実行中の event loop に紐づく asyncio ロックを返す。

        ``asyncio.Lock`` は最初の待機時に event loop へ束縛される。テストのように
        プロセス内で loop が作り直される場合、旧 loop の Lock を使い回すと
        RuntimeError になるため、保持されていない場合に限り作り直す。
        """

        loop = asyncio.get_running_loop()
        local = self._local
        if local is not None and self._local_loop is loop:
            return local
        if local is not None:
            previous_loop = self._local_loop
            if previous_loop is not None and not previous_loop.is_closed() and local.locked():
                # 別の生存 loop がまだ保持している異常系。OS ロックが最終防衛線。
                return local
        local = asyncio.Lock()
        self._local = local
        self._local_loop = loop
        return local

    @staticmethod
    def _owner_key() -> tuple[asyncio.AbstractEventLoop, "asyncio.Task | None"]:
        return (asyncio.get_running_loop(), asyncio.current_task())

    def _lock_handle(self, handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_handle(self, handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _close_handle(self, handle: BinaryIO) -> None:
        """OS ロックを解除して handle を閉じる。失敗しても close は必ず行う。"""

        try:
            self._unlock_handle(handle)
        except (OSError, ValueError):
            logger.warning("lock file %s の unlock に失敗しました", self._path, exc_info=True)
        finally:
            try:
                handle.close()
            except OSError:
                logger.warning("lock file %s の close に失敗しました", self._path, exc_info=True)

    # ------------------------------------------------------------------
    # ワーカースレッド側
    # ------------------------------------------------------------------
    def _acquire_file(self, request: _AcquireRequest) -> BinaryIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
            while True:
                if request.cancel_event.is_set():
                    raise _LockAcquireCancelled()
                try:
                    self._lock_handle(handle)
                    break
                except OSError as exc:
                    if not _is_contention_error(exc):
                        # 恒久的エラーはリトライしても回復しないので即時失敗。
                        raise
                remaining = request.remaining()
                if remaining is not None and remaining <= 0:
                    raise _LockAcquireTimedOut()
                wait_for = _POLL_INTERVAL if remaining is None else min(_POLL_INTERVAL, remaining)
                if request.cancel_event.wait(wait_for):
                    raise _LockAcquireCancelled()
            return handle
        except BaseException:
            handle.close()
            raise

    def _dispose_worker(self, request: _AcquireRequest, task: "asyncio.Task") -> None:
        """獲得側が離脱した後にワーカーが掴んだ handle を後始末する。

        ``add_done_callback`` からも、キャンセル処理からも呼ばれる。
        ``request.disposed`` により二重解放と二重キャンセルを吸収する。
        """

        if not request.abandoned or request.disposed:
            return
        request.disposed = True
        if task.cancelled():
            return
        if task.exception() is not None:
            return
        handle = task.result()
        if handle is not None:
            self._close_handle(handle)

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------
    async def acquire(self, timeout: float | None = None) -> bool:
        """ロックを獲得する。

        Args:
            timeout: 待ち時間上限（秒）。``None`` なら既定値、``math.inf`` で無限待ち。

        Raises:
            AppOperationLockTimeout: 制限時間内に獲得できなかった場合。
            OSError: lock file を開けない等の恒久的エラー。
        """

        limit = _resolve_timeout(timeout)
        deadline = None if math.isinf(limit) else time.monotonic() + limit
        local = self._get_local()

        local_timeout = (
            None if deadline is None else max(deadline - time.monotonic(), _MIN_LOCAL_WAIT)
        )
        try:
            await asyncio.wait_for(local.acquire(), local_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise AppOperationLockTimeout(
                f"App operation lock を {limit:g} 秒以内に獲得できませんでした: {self._path}"
            ) from exc

        request = _AcquireRequest(deadline)
        try:
            worker = asyncio.create_task(asyncio.to_thread(self._acquire_file, request))
        except BaseException:
            self._release_local(local)
            raise
        worker.add_done_callback(functools.partial(self._dispose_worker, request))

        try:
            handle = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # ワーカーは shield 越しなので生きている。獲得済み／獲得直後どちらでも
            # handle を確実に閉じるため、abandoned を立ててから後始末を試みる。
            request.abandoned = True
            request.cancel_event.set()
            if worker.done():
                self._dispose_worker(request, worker)
            self._release_local(local)
            raise
        except _LockAcquireTimedOut as exc:
            self._release_local(local)
            raise AppOperationLockTimeout(
                f"App operation lock を {limit:g} 秒以内に獲得できませんでした: {self._path}"
            ) from exc
        except _LockAcquireCancelled:
            self._release_local(local)
            raise asyncio.CancelledError()
        except BaseException:
            self._release_local(local)
            raise

        self._handle = handle
        self._owner = self._owner_key()
        return True

    def _release_local(self, local: "asyncio.Lock | None" = None) -> None:
        """獲得した asyncio ロックを解放する。

        ``local`` を明示すると、``_get_local()`` が別 loop 用に作り直した後でも
        「自分が獲得した方」を確実に解放できる。
        """

        target = local if local is not None else self._local
        if target is not None and target.locked():
            target.release()

    def release(self) -> None:
        """ロックを解放する。獲得したタスク以外からの呼び出しは無視する。"""

        owner = self._owner
        if owner is None:
            # acquire に失敗した呼び出し元が finally で release() する経路。
            return
        try:
            current = self._owner_key()
        except RuntimeError:
            current = None
        if current is None or current != owner:
            logger.warning(
                "App operation lock %s を保持していないタスクからの release() を無視しました",
                self._path,
            )
            return

        self._owner = None
        handle = self._handle
        self._handle = None
        try:
            if handle is not None:
                self._close_handle(handle)
        finally:
            self._release_local()

    async def __aenter__(self) -> "AppOperationLock":
        await self.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


#: 旧名の後方互換エイリアス。
_CrossProcessLock = AppOperationLock


_LOCKS: dict[str, AppOperationLock] = {}
_LOCKS_GUARD = threading.Lock()


def _normalize_lock_key(path: Path) -> str:
    """lock path をレジストリキーへ正規化する。

    ``get_workspaces_root()`` は ``resolve()`` 済みだが、Windows では対象
    ディレクトリが未作成のとき最終要素の大文字小文字が確定しない
    （``resolve()`` は存在する接頭辞までしか正規化しない）。同じ workspace が
    別ロックへ分裂しないよう ``normcase`` を重ねる。
    """

    return os.path.normcase(os.path.normpath(str(path)))


def _get_lock(
    kind: str,
    value: UUID | str,
    workspace_root: str | os.PathLike[str] | None,
) -> AppOperationLock:
    root = get_workspaces_root(workspace_root)
    path = (root / ".locks" / f"{kind}_{value}.lock").resolve()
    key = _normalize_lock_key(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = AppOperationLock(path)
            _LOCKS[key] = lock
        return lock


def app_operation_lock(
    app_id: UUID | str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> AppOperationLock:
    """Return the shared operation lock for one App."""

    return _get_lock("app", app_id, workspace_root)


def project_operation_lock(
    project_id: UUID | str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> AppOperationLock:
    """Return the shared lifecycle lock for one Project's App instances."""

    return _get_lock("project", project_id, workspace_root)


__all__ = [
    "AppOperationLock",
    "AppOperationLockError",
    "AppOperationLockTimeout",
    "ACQUIRE_TIMEOUT_ENV_KEY",
    "DEFAULT_ACQUIRE_TIMEOUT",
    "app_operation_lock",
    "project_operation_lock",
]
