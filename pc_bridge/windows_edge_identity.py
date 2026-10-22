"""Read-only verification of an external Edge debugging listener.

No process here is job-owned, launched, assigned, terminated, or configured.
PID plus creation time and executable path are rechecked around listener reads.
This is a local process identity gate, not a claim of cryptographic CDP auth.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import ntpath
import os
import socket
import struct
from typing import Mapping
from urllib.parse import urlsplit

from .edge_cdp import EdgeCdpEndpoint, EdgeCdpError

_MAX_TABLE_BYTES = 16 * 1024 * 1024
_CHANNEL_FOLDERS = {
    "stable": "Edge",
    "beta": "Edge Beta",
    "dev": "Edge Dev",
    "canary": "Edge SxS",
}
_DWORD = ctypes.c_uint32


@dataclass(frozen=True)
class EdgeProcessIdentity:
    pid: int
    creation_ticks: int
    executable: str = field(repr=False)


def _windows_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise EdgeCdpError("edge_process_path_invalid")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    drive, tail = ntpath.splitdrive(value)
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or not tail.startswith(("\\", "/"))
    ):
        raise EdgeCdpError("edge_process_path_invalid")
    return ntpath.normcase(ntpath.normpath(value))


def known_edge_paths(
    channel: str, environ: Mapping[str, str] | None = None
) -> frozenset[str]:
    """Canonical channel paths from local installation roots; no registry writes."""
    if channel not in _CHANNEL_FOLDERS:
        raise EdgeCdpError("edge_channel_invalid")
    env = os.environ if environ is None else environ
    folded = {key.upper(): value for key, value in env.items()}
    paths = set()
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = folded.get(key)
        if root:
            try:
                root = _windows_path(root)
            except EdgeCdpError:
                continue
            paths.add(
                ntpath.join(
                    root,
                    "microsoft",
                    _CHANNEL_FOLDERS[channel].lower(),
                    "application",
                    "msedge.exe",
                )
            )
    return frozenset(paths)


class EdgeEndpointIdentityVerifier:
    """Pin one exact external Edge process; a new owner requires a new run."""

    def __init__(self, *, inspector=None, environ: Mapping[str, str] | None = None):
        self._inspector = inspector
        self._environ = environ

    def _inspect(self, endpoint: EdgeCdpEndpoint) -> EdgeProcessIdentity:
        if not isinstance(endpoint, EdgeCdpEndpoint):
            raise EdgeCdpError("edge_cdp_endpoint_invalid")
        if self._inspector is None:
            self._inspector = WindowsEdgeInspector()
        parsed = urlsplit(endpoint.url)
        try:
            owners = self._inspector.listener_owners(parsed.hostname, parsed.port)
            if (
                not isinstance(owners, tuple)
                or len(owners) != 1
                or type(owners[0]) is not int
                or owners[0] <= 0
            ):
                raise EdgeCdpError("edge_listener_owner_unverified")
            identity = self._inspector.process_identity(owners[0])
            if (
                not isinstance(identity, EdgeProcessIdentity)
                or identity.pid != owners[0]
                or type(identity.creation_ticks) is not int
                or identity.creation_ticks <= 0
            ):
                raise EdgeCdpError("edge_process_identity_unverified")
            path = _windows_path(identity.executable)
            if ntpath.basename(path) != "msedge.exe":
                raise EdgeCdpError("edge_listener_not_edge")
            known = known_edge_paths(endpoint.channel, self._environ)
            if not known:
                raise EdgeCdpError("edge_installation_path_unavailable")
            if path not in known:
                raise EdgeCdpError("edge_channel_path_mismatch")
            canonical = EdgeProcessIdentity(identity.pid, identity.creation_ticks, path)
            # Query again rather than trusting a PID read before OpenProcess.
            if self._inspector.listener_owners(parsed.hostname, parsed.port) != owners:
                raise EdgeCdpError("edge_listener_owner_changed")
            again = self._inspector.process_identity(identity.pid)
            if (
                not isinstance(again, EdgeProcessIdentity)
                or again.pid != identity.pid
                or again.creation_ticks != identity.creation_ticks
                or _windows_path(again.executable) != path
            ):
                raise EdgeCdpError("edge_process_identity_changed")
            return canonical
        except EdgeCdpError:
            raise
        except Exception:
            raise EdgeCdpError("edge_process_identity_unverified") from None

    def capture(self, endpoint: EdgeCdpEndpoint) -> EdgeProcessIdentity:
        return self._inspect(endpoint)

    def verify(self, endpoint: EdgeCdpEndpoint, expected: EdgeProcessIdentity) -> None:
        if self._inspect(endpoint) != expected:
            raise EdgeCdpError("edge_listener_owner_changed")


def _listener_owners(raw: bytes, family: int, host: str, port: int) -> tuple[int, ...]:
    """Decode SDK OWNER_PID_LISTENER tables, including malformed-table checks."""
    row_size = {2: 24, 23: 56}.get(family)
    if row_size is None or len(raw) < 4 or len(raw) > _MAX_TABLE_BYTES:
        raise EdgeCdpError("edge_listener_table_invalid")
    count = struct.unpack_from("<I", raw)[0]
    if count > (len(raw) - 4) // row_size:
        raise EdgeCdpError("edge_listener_table_invalid")
    owners = []
    for index in range(count):
        offset = 4 + index * row_size
        if family == 2:
            state, _, encoded_port, _, _, pid = struct.unpack_from("<6I", raw, offset)
            address = socket.inet_ntop(socket.AF_INET, raw[offset + 4 : offset + 8])
            scope = 0
        else:
            address = socket.inet_ntop(socket.AF_INET6, raw[offset : offset + 16])
            scope, encoded_port = struct.unpack_from("<2I", raw, offset + 16)
            state, pid = struct.unpack_from("<2I", raw, offset + 48)
        actual_port = socket.ntohs(encoded_port & 0xFFFF)
        if state != 2 or actual_port != port:
            continue
        if address in {"0.0.0.0", "::"}:
            # Do not silently bless remotely exposed wildcard debugging ports.
            raise EdgeCdpError("edge_listener_not_loopback")
        if address == host:
            if scope != 0 or pid <= 0:
                raise EdgeCdpError("edge_listener_owner_unverified")
            owners.append(pid)
    return tuple(owners)


class WindowsEdgeInspector:
    """Only read-only Win32 APIs; injectable APIs permit Linux unit tests."""

    def __init__(self, *, api=None, ip_api=None):
        if api is None or ip_api is None:
            if os.name != "nt":
                raise EdgeCdpError("edge_process_verification_requires_windows")
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            ip_api = ctypes.WinDLL("iphlpapi", use_last_error=True)
            handle = ctypes.c_void_p
            api.OpenProcess.argtypes = [_DWORD, ctypes.c_int, _DWORD]
            api.OpenProcess.restype = handle
            api.CloseHandle.argtypes = [handle]
            api.CloseHandle.restype = ctypes.c_int
            api.GetProcessTimes.argtypes = [handle, *([ctypes.c_void_p] * 4)]
            api.GetProcessTimes.restype = ctypes.c_int
            api.QueryFullProcessImageNameW.argtypes = [
                handle,
                _DWORD,
                ctypes.c_wchar_p,
                ctypes.POINTER(_DWORD),
            ]
            api.QueryFullProcessImageNameW.restype = ctypes.c_int
            api.WaitForSingleObject.argtypes = [handle, _DWORD]
            api.WaitForSingleObject.restype = _DWORD
            ip_api.GetExtendedTcpTable.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_DWORD),
                ctypes.c_int,
                _DWORD,
                ctypes.c_int,
                _DWORD,
            ]
            ip_api.GetExtendedTcpTable.restype = _DWORD
        self._api, self._ip_api = api, ip_api

    def listener_owners(self, host: str, port: int) -> tuple[int, ...]:
        if (
            host not in {"127.0.0.1", "::1"}
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise EdgeCdpError("edge_cdp_endpoint_invalid")
        family = 2 if host == "127.0.0.1" else 23
        size = _DWORD()
        # TableClass=TCP_TABLE_OWNER_PID_LISTENER. Resize only a bounded number
        # of times if the system table changes between sizing and retrieval.
        code = self._ip_api.GetExtendedTcpTable(
            None, ctypes.byref(size), False, family, 3, 0
        )
        for _ in range(3):
            if code not in (0, 122) or not 4 <= size.value <= _MAX_TABLE_BYTES:
                raise EdgeCdpError("edge_listener_query_failed")
            capacity = size.value
            data = ctypes.create_string_buffer(capacity)
            code = self._ip_api.GetExtendedTcpTable(
                data, ctypes.byref(size), False, family, 3, 0
            )
            if code == 0:
                if not 4 <= size.value <= capacity:
                    raise EdgeCdpError("edge_listener_table_invalid")
                return _listener_owners(data.raw[: size.value], family, host, port)
        raise EdgeCdpError("edge_listener_query_failed")

    def process_identity(self, pid: int) -> EdgeProcessIdentity | None:
        if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
            return None
        # QUERY_LIMITED_INFORMATION | SYNCHRONIZE; neither permits mutation.
        handle = self._api.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return None
        try:
            times = [ctypes.c_uint64() for _ in range(4)]
            if not self._api.GetProcessTimes(
                handle, *(ctypes.byref(value) for value in times)
            ):
                return None
            size = _DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not self._api.QueryFullProcessImageNameW(
                handle, 0, image, ctypes.byref(size)
            ):
                return None
            if (
                not 0 < size.value < len(image)
                or self._api.WaitForSingleObject(handle, 0) != 258
            ):
                return None
            return EdgeProcessIdentity(pid, times[0].value, image.value)
        finally:
            self._api.CloseHandle(handle)
