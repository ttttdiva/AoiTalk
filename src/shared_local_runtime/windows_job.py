"""Windows ownership guard for every process spawned by the coordinator.

An anonymous non-inheritable job handle is owned only by the broker. Windows
closes it even after TerminateProcess/crash, killing the broker's descendants.
External listeners are never assigned to this job.
"""
from __future__ import annotations

import ctypes
import logging
import os

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
KILL_ON_JOB_CLOSE = 0x00002000
EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", DWORD),
                ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", DWORD), ("Affinity", SIZE_T),
                ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class OwnershipUnavailable(RuntimeError):
    pass


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], HANDLE),
        "IsProcessInJob": ([HANDLE, HANDLE, ctypes.POINTER(BOOL)], BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
        "SetInformationJobObject": ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], BOOL),
        "QueryInformationJobObject": ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)], BOOL),
        "AssignProcessToJobObject": ([HANDLE, HANDLE], BOOL),
        "CloseHandle": ([HANDLE], BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def current_job_limits(kernel=None, last_error=None):
    """Read the immediate inherited job without changing it or its restrictions.

    Microsoft documents that QueryInformationJobObject(NULL, ...) selects the
    calling process's immediate job. Windows may attach a benign system job even
    after a permitted CREATE_BREAKAWAY_FROM_JOB launch.
    """
    kernel = kernel or _kernel32()
    last_error = last_error or ctypes.get_last_error
    member = BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(member)):
        raise OwnershipUnavailable(
            f"Shared runtime ownership initialization failed: IsProcessInJob, Windows error {last_error()}. No model worker was started.")
    flags = 0
    if member.value:
        limits = _ExtendedLimits()
        returned = DWORD()
        if not kernel.QueryInformationJobObject(None, EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits), ctypes.byref(returned)):
            raise OwnershipUnavailable(
                f"Shared runtime ownership initialization failed: QueryInformationJobObject(inherited), Windows error {last_error()}. No model worker was started.")
        flags = int(limits.BasicLimitInformation.LimitFlags)
    return {"in_job": bool(member.value), "limit_flags": flags,
            "kill_on_close": bool(flags & KILL_ON_JOB_CLOSE)}


class KillOnCloseJob:
    def __init__(self, kernel=None, last_error=None):
        kernel = kernel or _kernel32()
        last_error = last_error or ctypes.get_last_error
        self.handle = None
        self.kernel = kernel

        def fail(operation):
            raise OwnershipUnavailable(
                f"Shared runtime ownership initialization failed: {operation}, Windows error {last_error()}. "
                "No model worker was started.")

        current = kernel.GetCurrentProcess()
        self.inherited_job = current_job_limits(kernel=kernel, last_error=last_error)
        if self.inherited_job["in_job"]:
            flags = self.inherited_job["limit_flags"]
            logging.getLogger(__name__).info("Inherited Windows job flags: 0x%08x", flags)
            if self.inherited_job["kill_on_close"]:
                raise OwnershipUnavailable(
                    f"Shared runtime coordinator inherited a kill-on-close Windows job (flags=0x{flags:08x}). "
                    "Independent lifetime cannot be guaranteed. No job restrictions were changed and no model worker was started.")
            # A harmless residual system job may remain after breakaway. Windows
            # 8+ permits nested ownership; AssignProcessToJobObject below remains
            # authoritative and fails closed if limits/UI restrictions disallow it.
        handle = kernel.CreateJobObjectW(None, None)  # Anonymous, non-inheritable handle.
        if not handle:
            fail("CreateJobObjectW")
        assigned = False
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, EXTENDED_LIMIT_INFORMATION,
                                                 ctypes.byref(limits), ctypes.sizeof(limits)):
                fail("SetInformationJobObject")
            verified = _ExtendedLimits()
            size = DWORD()
            if not kernel.QueryInformationJobObject(handle, EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(verified), ctypes.sizeof(verified), ctypes.byref(size)):
                fail("QueryInformationJobObject")
            if not verified.BasicLimitInformation.LimitFlags & KILL_ON_JOB_CLOSE:
                fail("verify KILL_ON_JOB_CLOSE")
            if not kernel.AssignProcessToJobObject(handle, current):
                fail("AssignProcessToJobObject")
            assigned = True
            self.handle = handle
        finally:
            if not assigned:
                kernel.CloseHandle(handle)
        # Never explicitly CloseHandle here: the broker itself is a member. The
        # OS closes the last handle on process exit, including forced termination.


def install_process_ownership():
    """Install before importing/launching the backend; retain the returned guard."""
    if os.name != "nt":
        return None
    return KillOnCloseJob()
