"""Windows-only process ownership; an attached browser is never job-owned.

The CLI starts suspended and cannot execute until assignment to a fresh,
non-breakaway, kill-on-close Job succeeds. Keep the Job alive across short CLI
invocations: their daemon descendants remain owned. Kernel injection exists for
unit tests, not as an alternate production process launcher.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import socket
import subprocess
import threading
import time
from typing import BinaryIO, Mapping, Protocol


class HelperJobError(RuntimeError):
    """Fixed codes only; never include argv, output, or OS exception text."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_ticks: int


class OwnedProcess(Protocol):
    identity: ProcessIdentity
    stdin: BinaryIO
    stdout: BinaryIO
    stderr: BinaryIO

    def poll(self) -> int | None: ...
    def cancel_io(self, thread_ids: tuple[int, ...]) -> None: ...
    def close(self) -> None: ...


class WindowsHelperJob:
    def __init__(self, *, kernel=None, name=None):
        self._kernel = _WindowsKernel() if kernel is None else kernel
        self._lock = threading.RLock()
        self._job = self._kernel.create_job() if name is None else self._kernel.create_job(name)
        self._closed = False
        self._records: set[ProcessIdentity] = set()

    @property
    def records(self) -> tuple[ProcessIdentity, ...]:
        with self._lock:
            return tuple(
                sorted(self._records, key=lambda item: (item.pid, item.creation_ticks))
            )

    def spawn(
        self, argv: tuple[str, ...], *, cwd, environment: Mapping[str, str]
    ) -> OwnedProcess:
        with self._lock:
            if self._closed:
                raise HelperJobError("agent_browser_owner_closed")
            process = None
            try:
                process = self._kernel.spawn_suspended(argv, cwd, environment)
                self._kernel.assign(self._job, process)
                identity = self._kernel.process_identity(process)
                if not self._kernel.is_member(self._job, identity):
                    raise HelperJobError("agent_browser_ownership_unverified")
                process.identity = identity
                self._records.add(identity)
                self._kernel.resume(process)
                return process
            except Exception:
                # Only this newly created process handle is eligible here. Never
                # resume after failed assignment, and never kill by PID/name.
                if process is not None:
                    try:
                        self._kernel.terminate_process(process)
                    except Exception:
                        pass
                    finally:
                        process.close()
                raise HelperJobError("agent_browser_owned_spawn_failed") from None

    def capture_identity(self, pid: int) -> ProcessIdentity | None:
        with self._lock:
            if self._closed or type(pid) is not int or pid <= 0:
                return None
            identity = self._kernel.identity_for_pid(pid)
            if identity is None or not self._kernel.is_member(self._job, identity):
                return None
            self._records.add(identity)
            return identity

    def snapshot_identities(self):
        with self._lock:
            if self._closed:
                return ()
            return tuple(identity for pid in self._kernel.job_pids(self._job)
                         if (identity := self.capture_identity(pid)) is not None)

    def verify_identity(self, identity: ProcessIdentity) -> bool:
        with self._lock:
            return (
                not self._closed
                and identity in self._records
                and self._kernel.is_member(self._job, identity)
            )

    def verify_port(self, identity: ProcessIdentity, port: int) -> bool:
        """Verify an owned helper listener; this does NOT claim the Edge port."""
        with self._lock:
            if type(port) is not int or not 1 <= port <= 65535:
                return False
            if not self.verify_identity(identity):
                return False
            try:
                observed = self._kernel.has_loopback_listener(identity.pid, port)
            except Exception:
                return False
            return observed and self.verify_identity(identity)

    def close(self, *, timeout_seconds: float = 2.0) -> bool:
        """Terminate only this Job; return whether zero descendants was observed."""
        with self._lock:
            if self._closed:
                return self._cleanup_verified
            self._closed = True
            self._cleanup_verified = False
            try:
                self._kernel.terminate_job(self._job)
                deadline = time.monotonic() + max(0.0, timeout_seconds)
                while True:
                    if not self._kernel.job_pids(self._job):
                        self._cleanup_verified = True
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            except Exception:
                self._cleanup_verified = False
            finally:
                self._kernel.close_job(self._job)
            return self._cleanup_verified


class _WindowsProcess:
    def __init__(self, kernel, handle, thread, pid, stdin, stdout, stderr):
        self._kernel, self.handle, self.thread = kernel, handle, thread
        self.pid = pid
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.identity = ProcessIdentity(pid, 0)
        self._io_handles = []
        self._io_lock = threading.Lock()
        self._io_cancelled = threading.Event()

    def register_io_thread(self):
        # Capture a HANDLE while this exact thread is alive. Never reopen a
        # numeric thread ID later: an exited thread's ID could have been reused.
        handle = self._kernel.api.OpenThread(0x0001, False, threading.get_native_id())
        if not handle:
            raise HelperJobError("agent_browser_io_thread_unavailable")
        with self._io_lock:
            self._io_handles.append(handle)

    def poll(self):
        # GetExitCodeProcess's STILL_ACTIVE also can be a process exit code.
        if self._kernel.winapi.WaitForSingleObject(self.handle, 0) == 258:
            return None
        return self._kernel.winapi.GetExitCodeProcess(self.handle)

    def read_output(self, stream, size):
        """Drain CLI bytes without waiting for an inherited daemon writer's EOF.

        Each pipe has exactly one reader. A signaled CLI handle is the output
        boundary, but it is checked *before* the final peek so final writes are
        not missed between a zero-byte peek and process exit. This does not
        imply success: the caller still validates complete JSON and exit code.
        """
        while not self._io_cancelled.is_set():
            available = self._kernel.pipe_available(stream)
            if available is None:
                return b""
            if available:
                return stream.read(min(size, available))
            if self.poll() is not None:
                # The CLI can no longer write. Do not require daemon-owned
                # duplicates of its stdio handles to close before returning.
                available = self._kernel.pipe_available(stream)
                return stream.read(min(size, available)) if available else b""
            self._io_cancelled.wait(0.01)
        raise HelperJobError("agent_browser_io_cancelled")

    def cancel_io(self, thread_ids):
        self._io_cancelled.set()
        with self._io_lock:
            for handle in self._io_handles:
                self._kernel.api.CancelSynchronousIo(handle)

    def close(self):
        with self._io_lock:
            for handle in self._io_handles:
                self._kernel.api.CloseHandle(handle)
            self._io_handles.clear()
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if self.thread is not None:
            self._kernel.winapi.CloseHandle(self.thread)
            self.thread = None
        if self.handle is not None:
            self._kernel.winapi.CloseHandle(self.handle)
            self.handle = None


# Fixed-width fields also make the structure definitions inspectable off Windows.
DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKernel:
    def __init__(self):
        if os.name != "nt":
            raise HelperJobError("agent_browser_windows_required")
        import _winapi
        import msvcrt

        self.winapi, self.msvcrt = _winapi, msvcrt
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
            "OpenJobObjectW": ([DWORD, DWORD, ctypes.c_wchar_p], HANDLE),
            "SetInformationJobObject": (
                [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD],
                DWORD,
            ),
            "AssignProcessToJobObject": ([HANDLE, HANDLE], DWORD),
            "IsProcessInJob": ([HANDLE, HANDLE, ctypes.POINTER(DWORD)], DWORD),
            "QueryInformationJobObject": (
                [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)],
                DWORD,
            ),
            "TerminateJobObject": ([HANDLE, DWORD], DWORD),
            "CloseHandle": ([HANDLE], DWORD),
            "ResumeThread": ([HANDLE], DWORD),
            "OpenProcess": ([DWORD, DWORD, DWORD], HANDLE),
            "GetProcessTimes": ([HANDLE] + [ctypes.c_void_p] * 4, DWORD),
            "OpenThread": ([DWORD, DWORD, DWORD], HANDLE),
            "CancelSynchronousIo": ([HANDLE], DWORD),
            "PeekNamedPipe": (
                [
                    HANDLE,
                    ctypes.c_void_p,
                    DWORD,
                    ctypes.c_void_p,
                    ctypes.POINTER(DWORD),
                    ctypes.c_void_p,
                ],
                DWORD,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.ip.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(DWORD),
            DWORD,
            DWORD,
            ctypes.c_int,
            DWORD,
        ]
        self.ip.GetExtendedTcpTable.restype = DWORD

    def create_job(self, name=None):
        handle = self.api.CreateJobObjectW(None, name)
        if not handle:
            raise HelperJobError("agent_browser_job_create_failed")
        if name is not None and ctypes.get_last_error() == 183:
            self.api.CloseHandle(handle)
            raise HelperJobError("agent_browser_job_already_exists")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        )
        if not self.api.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.api.CloseHandle(handle)
            raise HelperJobError("agent_browser_job_configure_failed")
        return handle

    def spawn_suspended(self, argv, cwd, environment):
        # _winapi constructs STARTUPINFOEX from this explicit handle-list.
        # The Job handle and parent pipe ends are never inherited.
        descriptors = []
        streams = []
        hp = ht = None
        try:
            in_read, in_write = os.pipe()
            descriptors.extend((in_read, in_write))
            out_read, out_write = os.pipe()
            descriptors.extend((out_read, out_write))
            err_read, err_write = os.pipe()
            descriptors.extend((err_read, err_write))
            child_fds = (in_read, out_write, err_write)
            handles = [self.msvcrt.get_osfhandle(fd) for fd in child_fds]
            for fd in child_fds:
                os.set_inheritable(fd, True)
            info = subprocess.STARTUPINFO()
            info.dwFlags = subprocess.STARTF_USESTDHANDLES
            info.hStdInput, info.hStdOutput, info.hStdError = handles
            info.lpAttributeList = {"handle_list": handles}
            hp, ht, pid, _ = self.winapi.CreateProcess(
                argv[0],
                subprocess.list2cmdline(argv),
                None,
                None,
                True,
                0x00000004 | 0x08000000,
                dict(environment),
                str(cwd),
                info,
            )
            for fd in child_fds:
                os.close(fd)
                descriptors.remove(fd)
            for fd, mode in ((in_write, "wb"), (out_read, "rb"), (err_read, "rb")):
                streams.append(os.fdopen(fd, mode, buffering=0))
                descriptors.remove(fd)
            return _WindowsProcess(self, hp, ht, pid, *streams)
        except Exception:
            for stream in streams:
                stream.close()
            if hp is not None:
                self.winapi.TerminateProcess(hp, 1)
                self.winapi.CloseHandle(hp)
            if ht is not None:
                self.winapi.CloseHandle(ht)
            raise HelperJobError("agent_browser_process_create_failed") from None
        finally:
            for fd in descriptors:
                os.close(fd)

    def pipe_available(self, stream):
        available = DWORD()
        handle = self.msvcrt.get_osfhandle(stream.fileno())
        if self.api.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            return available.value
        if ctypes.get_last_error() in (109, 232):  # BROKEN_PIPE / NO_DATA
            return None
        raise HelperJobError("agent_browser_pipe_query_failed")

    def assign(self, job, process):
        if not self.api.AssignProcessToJobObject(job, int(process.handle)):
            raise HelperJobError("agent_browser_job_assignment_failed")

    def resume(self, process):
        if self.api.ResumeThread(int(process.thread)) == 0xFFFFFFFF:
            raise HelperJobError("agent_browser_resume_failed")
        self.winapi.CloseHandle(process.thread)
        process.thread = None

    def terminate_process(self, process):
        self.winapi.TerminateProcess(process.handle, 1)
        self.winapi.WaitForSingleObject(process.handle, 2000)

    def _creation_ticks(self, handle):
        values = [ctypes.c_uint64() for _ in range(4)]
        if not self.api.GetProcessTimes(
            handle, *(ctypes.byref(item) for item in values)
        ):
            return None
        return values[0].value

    def process_identity(self, process):
        ticks = self._creation_ticks(int(process.handle))
        if ticks is None:
            raise HelperJobError("agent_browser_process_identity_failed")
        return ProcessIdentity(process.pid, ticks)

    def identity_for_pid(self, pid):
        handle = self.api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            ticks = self._creation_ticks(handle)
            return ProcessIdentity(pid, ticks) if ticks is not None else None
        finally:
            self.api.CloseHandle(handle)

    def recorded_process_alive(self, identity):
        handle = self.api.OpenProcess(0x1000 | 0x00100000, False, identity.pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # process no longer exists
                return False
            raise HelperJobError("agent_browser_process_state_unverified")
        try:
            ticks = self._creation_ticks(handle)
            if ticks is None:
                raise HelperJobError("agent_browser_process_state_unverified")
            return ticks == identity.creation_ticks and self.winapi.WaitForSingleObject(handle, 0) == 258
        finally:
            self.api.CloseHandle(handle)

    def named_job_has_processes(self, name):
        handle = self.api.OpenJobObjectW(0x0004, False, name)  # QUERY, never terminate
        if not handle:
            if ctypes.get_last_error() == 2:
                return False
            raise HelperJobError("agent_browser_job_state_unverified")
        try:
            return bool(self.job_pids(handle))
        finally:
            self.api.CloseHandle(handle)

    def is_member(self, job, identity):
        handle = self.api.OpenProcess(0x1000 | 0x00100000, False, identity.pid)
        if not handle:
            return False
        try:
            member = DWORD()
            return (
                self._creation_ticks(handle) == identity.creation_ticks
                and self.winapi.WaitForSingleObject(handle, 0) == 258
                and bool(self.api.IsProcessInJob(handle, job, ctypes.byref(member)))
                and bool(member.value)
            )
        finally:
            self.api.CloseHandle(handle)

    def terminate_job(self, job):
        if not self.api.TerminateJobObject(job, 1):
            raise HelperJobError("agent_browser_job_terminate_failed")

    def job_pids(self, job):
        size = 4096
        while size <= 1024 * 1024:
            data = ctypes.create_string_buffer(size)
            if self.api.QueryInformationJobObject(job, 3, data, size, None):
                count = DWORD.from_buffer(data, 4).value
                if count > (size - 8) // ctypes.sizeof(ctypes.c_size_t):
                    break
                return tuple((ctypes.c_size_t * count).from_buffer(data, 8))
            if ctypes.get_last_error() != 234:  # MORE_DATA
                break
            size *= 2
        raise HelperJobError("agent_browser_job_query_failed")

    def close_job(self, job):
        self.api.CloseHandle(job)

    def has_loopback_listener(self, pid, port):
        # TCP_TABLE_OWNER_PID_LISTENER=3. Query both families, fail closed.
        for family, row_size in ((2, 24), (23, 56)):
            size = DWORD()
            code = self.ip.GetExtendedTcpTable(
                None, ctypes.byref(size), False, family, 3, 0
            )
            if code not in (0, 122) or size.value > 16 * 1024 * 1024:
                raise HelperJobError("agent_browser_port_query_failed")
            if size.value < 4:
                continue
            data = ctypes.create_string_buffer(size.value)
            code = self.ip.GetExtendedTcpTable(
                data, ctypes.byref(size), False, family, 3, 0
            )
            if code:
                raise HelperJobError("agent_browser_port_query_failed")
            raw = data.raw
            count = int.from_bytes(raw[:4], "little")
            if 4 + count * row_size > len(raw):
                raise HelperJobError("agent_browser_port_query_failed")
            for offset in range(4, 4 + count * row_size, row_size):
                row = raw[offset : offset + row_size]
                if family == 2:
                    address, port_bytes, owner = row[4:8], row[8:10], row[20:24]
                    loopback = address == b"\x7f\x00\x00\x01"
                else:
                    address, port_bytes, owner = row[:16], row[20:22], row[52:56]
                    loopback = address == socket.inet_pton(socket.AF_INET6, "::1")
                if (
                    loopback
                    and int.from_bytes(port_bytes, "big") == port
                    and int.from_bytes(owner, "little") == pid
                ):
                    return True
        return False
