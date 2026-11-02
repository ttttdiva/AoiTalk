"""In-process Windows DPAPI; no helper executable or interactive prompt."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Protocol

from ..codes import AgentLinkFailure, LinkErrorCode


class DPAPIUnprotector(Protocol):
    def unprotect(self, wrapped: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDPAPIUnprotector:
    """Unprotect a 32-byte storage key as the current Windows user."""

    def __init__(self, *, platform: str | None = None) -> None:
        self._platform = sys.platform if platform is None else platform

    def unprotect(self, wrapped: bytes) -> bytes:
        if self._platform != "win32":
            raise AgentLinkFailure(LinkErrorCode.GROK_PROFILE_UNAVAILABLE)
        if not isinstance(wrapped, bytes) or not 0 < len(wrapped) <= 64 * 1024:
            raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED)
        output = _DataBlob()
        local_free = None
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            unprotect = crypt32.CryptUnprotectData
            unprotect.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            unprotect.restype = wintypes.BOOL
            local_free = kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            backing = (ctypes.c_ubyte * len(wrapped)).from_buffer_copy(wrapped)
            source = _DataBlob(len(wrapped), backing)
            # CRYPTPROTECT_UI_FORBIDDEN keeps credential access non-interactive.
            if not unprotect(
                ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)
            ):
                raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED)
            if output.cbData != 32 or not output.pbData:
                raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED)
            return ctypes.string_at(output.pbData, 32)
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.GROK_DPAPI_FAILED) from None
        finally:
            if output.pbData and local_free is not None:
                ctypes.memset(output.pbData, 0, output.cbData)
                local_free(ctypes.cast(output.pbData, ctypes.c_void_p))
