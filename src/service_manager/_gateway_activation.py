"""公開Gatewayのactivation secretをNext.js子プロセスへ引き継ぐ。

公開経路（Cloudflare → Caddy → Next）のgateway marker付き要求は、Next側が
`GATEWAY_SUPERVISOR_ACTIVATION_SECRET` で署名proofを検証する。この値は
85_CaddyGatewayがWindows DPAPI（CurrentUser）で保管し、Gatewayの起動時に
プロセス環境へ復元する。`run.bat` のような通常起動はその環境を持たないため、
Next側にだけ値が無く、全てのgateway経由要求が403になる。

ここでは85と同じ規則（DPAPI blobがあればそれを優先し、無い場合のみ有効な
環境値を残す）で復元し、Next.jsの子プロセス環境にだけ設定する。値はログや
例外に出さない。
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
from ctypes import wintypes
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger(__name__)

ACTIVATION_ENV = "GATEWAY_SUPERVISOR_ACTIVATION_SECRET"
BLOB_ENV = "GATEWAY_SUPERVISOR_ACTIVATION_BLOB"
GATEWAY_ROOT_ENV = "GATEWAY_ROOT"
DEFAULT_GATEWAY_DIRNAME = "85_CaddyGateway"
BLOB_REL_PATH = Path("config") / "private" / "supervisor-activation.dpapi"

_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,}$")
_MISSING_SENTINELS = frozenset(
    {
        "__AZV_SUPERVISOR_ACTIVATION_MISSING__",
        "__GATEWAY_SUPERVISOR_ACTIVATION_MISSING__",
        "__AZV_WORKER_GUARD_MISSING__",
    }
)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _is_valid_value(value: str) -> bool:
    return bool(value) and value not in _MISSING_SENTINELS and bool(_VALUE_PATTERN.match(value))


def _dpapi_unprotect(data: bytes) -> bytes:
    """CurrentUserスコープでDPAPI blobを復号する（Windows専用）。"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def resolve_blob_path(project_root: Path, env: MutableMapping[str, str]) -> Path:
    explicit = str(env.get(BLOB_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    gateway_root = str(env.get(GATEWAY_ROOT_ENV) or "").strip()
    root = Path(gateway_root) if gateway_root else project_root.parent / DEFAULT_GATEWAY_DIRNAME
    return root / BLOB_REL_PATH


def restore_activation_secret(project_root: Path, env: MutableMapping[str, str]) -> str:
    """`env` にactivation secretを復元し、結果の分類名を返す。

    分類: ``restored`` / ``inherited`` / ``unavailable`` / ``unsupported``。
    DPAPI blobが読めた場合は環境値より優先する（85と同じ規則）。
    """
    if os.name != "nt":
        return "unsupported"
    existing = str(env.get(ACTIVATION_ENV) or "").strip()
    blob_path = resolve_blob_path(project_root, env)
    if not blob_path.is_file():
        if _is_valid_value(existing):
            return "inherited"
        env.pop(ACTIVATION_ENV, None)
        return "unavailable"
    try:
        cipher = blob_path.read_bytes()
        if not cipher:
            raise ValueError("empty activation blob")
        plain = _dpapi_unprotect(cipher)
        value = plain.decode("utf-8").strip()
        if not _is_valid_value(value):
            raise ValueError("invalid activation value")
    except Exception as exc:  # noqa: BLE001 - 値を含み得る内容は記録しない
        env.pop(ACTIVATION_ENV, None)
        logger.warning("Gateway activation secretを復元できませんでした (%s)", type(exc).__name__)
        return "unavailable"
    env[ACTIVATION_ENV] = value
    return "restored"
