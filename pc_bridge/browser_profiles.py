"""Bridge-owned login profiles, separate from helper/QA session directories.

Acquiring a lease never starts a browser, reads cookies, or imports credentials.
The OS lock stays held until the owner has confirmed its browser is closed.
Profile data survives release; an unconfirmed close retains the lock.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .paths import home
from .profile_security import protect_directory, verify_directory, ProfileSecurityError, WindowsRuntimeObserver


class BrowserProfileError(RuntimeError):
    pass


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value or not UUID(value).int:
            raise ValueError()
    except (ValueError, AttributeError):
        raise BrowserProfileError("browser_profile_scope_invalid") from None
    return value


@dataclass(frozen=True)
class BrowserProfileScope:
    service: str
    owner_user_id: str
    platform_account_id: str

    def __post_init__(self):
        if self.service not in {"x", "youtube", "browser"}:
            raise BrowserProfileError("browser_profile_service_unsupported")
        _uuid(self.owner_user_id)
        _uuid(self.platform_account_id)

    def document(self):
        return {"service": self.service, "owner_user_id": self.owner_user_id,
                "platform_account_id": self.platform_account_id}


def _local_path(path):
    """Never follow a junction/symlink into an ordinary browser profile."""
    path = Path(path).absolute()
    if str(path).startswith(("\\\\", "//")):
        raise BrowserProfileError("browser_profile_path_unsafe")
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise BrowserProfileError("browser_profile_path_unsafe")
    return path


def _identity(info):
    return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", None)


class _ProfileLock:
    def __init__(self, path):
        self.path = _local_path(path)
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                # msvcrt locks one byte even for an initially empty file.
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.identity = _identity(os.fstat(self.file.fileno()))
            self.verify()
        except (OSError, BrowserProfileError):
            self.file.close()
            raise BrowserProfileError("browser_profile_busy_or_changed") from None

    def verify(self):
        if self.file.closed or _identity(_local_path(self.path).stat()) != self.identity:
            raise BrowserProfileError("browser_profile_lease_changed")

    def release(self):
        # Closing an OS handle releases the lock; never unlink a lock file.
        self.file.close()


class BrowserProfileLease:
    def __init__(self, manager, scope, directory, document, lock, connection_epoch):
        self.manager, self.scope, self.directory = manager, scope, directory
        self.profile_id, self._document, self._lock = document["profile_id"], document, lock
        self.connection_epoch = connection_epoch
        self.context = {"context_id": self.profile_id, "context_epoch": str(uuid4())}
        self.closed = False

    @property
    def user_data_dir(self):
        return self.directory / "browser"

    def verify(self):
        if self.closed:
            raise BrowserProfileError("browser_profile_lease_closed")
        self._lock.verify()
        if self.manager._read(self.directory, self.scope) != self._document:
            raise BrowserProfileError("browser_profile_binding_changed")
        _local_path(self.user_data_dir)
        try:
            verify_directory(self.directory)
            verify_directory(self.user_data_dir)
        except ProfileSecurityError as exc:
            raise BrowserProfileError(str(exc)) from None

    def start_runtime(self, job_name, processes=()):
        self.verify()
        if not isinstance(job_name, str) or not re.fullmatch(
                r"Local\\AoiTalk-Profile-" + re.escape(self.profile_id) + r"-[a-f0-9]{32}", job_name):
            raise BrowserProfileError("browser_profile_runtime_invalid")
        record = {"version": 1, "profile_id": self.profile_id, "job_name": job_name,
            "processes": [{"pid": item.pid, "creation_ticks": item.creation_ticks} for item in processes]}
        temporary = _local_path(self.directory / "runtime.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.directory / "runtime.json")

    def finish_runtime(self):
        self.verify()
        self.manager._check_runtime(self.directory, self.profile_id)

    def binding(self):
        self.verify()
        return {"version": 1, **self.scope.document(), "profile_id": self.profile_id,
                "connection_epoch": self.connection_epoch}

    def release(self, *, browser_closed):
        if browser_closed is not True:
            raise BrowserProfileError("browser_profile_close_unconfirmed")
        if not self.closed:
            self.closed = True
            self._lock.release()


class BrowserProfileManager:
    def __init__(self, root=None, *, runtime_observer=None):
        # root injection is for isolated tests/local trusted host construction,
        # never a WebSocket/Operations parameter or an existing Edge directory.
        self.root = _local_path(root if root is not None else home() / "data" / "browser-profiles")
        self._runtime_observer = runtime_observer or WindowsRuntimeObserver()

    def _check_runtime(self, directory, profile_id):
        path = _local_path(directory / "runtime.json")
        if not path.exists():
            return
        try:
            if path.stat().st_size > 32768:
                raise ValueError()
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
            if (not isinstance(record, dict) or set(record) != {"version", "profile_id", "job_name", "processes"}
                    or type(record["version"]) is not int or record["version"] != 1 or record["profile_id"] != profile_id
                    or not isinstance(record["job_name"], str) or not re.fullmatch(
                        r"Local\\AoiTalk-Profile-" + re.escape(profile_id) + r"-[a-f0-9]{32}", record["job_name"])
                    or not isinstance(record["processes"], list) or len(record["processes"]) > 256
                    or any(not isinstance(item, dict) or set(item) != {"pid", "creation_ticks"}
                        or any(type(item[key]) is not int or item[key] <= 0 for key in item) for item in record["processes"])):
                raise ValueError()
            alive = self._runtime_observer(record)
            if type(alive) is not bool:
                raise ProfileSecurityError("browser_profile_runtime_unverified")
            if alive:
                raise BrowserProfileError("browser_profile_runtime_still_alive")
            path.unlink()
        except BrowserProfileError:
            raise
        except (ValueError, OSError, ProfileSecurityError):
            raise BrowserProfileError("browser_profile_runtime_unverified") from None

    def _directory(self, scope):
        if not isinstance(scope, BrowserProfileScope):
            raise BrowserProfileError("browser_profile_scope_invalid")
        return _local_path(self.root / scope.service / scope.owner_user_id / scope.platform_account_id)

    @staticmethod
    def _read(directory, scope):
        path = _local_path(directory / "profile.json")
        try:
            if path.stat().st_size > 2048:
                raise ValueError()
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
            if (not isinstance(document, dict) or set(document) != {"version", "profile_id", *scope.document()}
                    or type(document["version"]) is not int or document["version"] != 1
                    or any(document[key] != value for key, value in scope.document().items())):
                raise ValueError()
            _uuid(document["profile_id"])
            return document
        except (OSError, ValueError, BrowserProfileError):
            raise BrowserProfileError("browser_profile_binding_invalid") from None

    def acquire(self, scope, *, connection_epoch):
        if not isinstance(connection_epoch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", connection_epoch):
            raise BrowserProfileError("browser_profile_connection_epoch_invalid")
        directory = self._directory(scope)
        _local_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            protect_directory(self.root)
        except ProfileSecurityError as exc:
            raise BrowserProfileError(str(exc)) from None
        directory.mkdir(parents=True, exist_ok=True)
        _local_path(directory)
        if (directory / "profile.json").exists():
            self._read(directory, scope)
        elif any(path.name != "lease.lock" for path in directory.iterdir()):
            raise BrowserProfileError("browser_profile_unregistered_data")
        try:
            protect_directory(directory)
        except ProfileSecurityError as exc:
            raise BrowserProfileError(str(exc)) from None
        lock = _ProfileLock(directory / "lease.lock")
        try:
            manifest = _local_path(directory / "profile.json")
            if not manifest.exists():
                # An unregistered existing browser directory is never adopted.
                if any(path.name != "lease.lock" for path in directory.iterdir()):
                    raise BrowserProfileError("browser_profile_unregistered_data")
                document = {"version": 1, "profile_id": str(uuid4()), **scope.document()}
                with manifest.open("x", encoding="utf-8") as output:
                    json.dump(document, output, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
            document = self._read(directory, scope)
            self._check_runtime(directory, document["profile_id"])
            user_data_dir = _local_path(directory / "browser")
            user_data_dir.mkdir(exist_ok=True)
            protect_directory(user_data_dir)
            return BrowserProfileLease(self, scope, directory, document, lock, connection_epoch)
        except BaseException:
            lock.release()
            raise
