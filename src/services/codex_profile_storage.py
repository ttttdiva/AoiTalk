"""Private Windows directories and offline startup policy for app profiles."""
from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes as w
from pathlib import Path

from .codex_app_profiles import CodexProfileError, _directory, _within


class WindowsPrivateStorage:
    """Native owner/SYSTEM-only protected DACL; never changes an existing ACL."""
    def __init__(self):
        if os.name != "nt":
            raise CodexProfileError("codex_profile_windows_storage_required")
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        self.adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        self.adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        self.adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
        self.adv.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        self.sid = self._current_sid()

    def _sid_text(self, pointer) -> str:
        value = w.LPWSTR()
        if not self.adv.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
            raise CodexProfileError("codex_profile_windows_owner_unverified")
        try:
            return value.value
        finally:
            self.kernel.LocalFree(value)

    def _current_sid(self) -> str:
        token, size = w.HANDLE(), w.DWORD()
        if not self.adv.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise CodexProfileError("codex_profile_windows_owner_unverified")
        try:
            self.adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise CodexProfileError("codex_profile_windows_owner_unverified")
            return self._sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            self.kernel.CloseHandle(token)

    def verify_directory(self, path: Path) -> tuple[int, int]:
        identity = _directory(path)
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.adv.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                             ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error:
            raise CodexProfileError("codex_profile_windows_acl_unverified")
        try:
            control, revision = w.WORD(), w.DWORD()
            if (self._sid_text(owner) != self.sid or not dacl.value
                    or not self.adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                    or not control.value & 0x1000):
                raise CodexProfileError("codex_profile_windows_acl_unverified")
            # ACL header: BYTE revision, BYTE pad, WORD size, WORD AceCount.
            count = ctypes.cast(dacl, ctypes.POINTER(w.WORD))[2]
            observed = set()
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.adv.GetAce(dacl, index, ctypes.byref(ace)):
                    raise CodexProfileError("codex_profile_windows_acl_unverified")
                header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
                mask = ctypes.cast(ace.value + 4, ctypes.POINTER(w.DWORD))[0]
                if header[0] != 0 or header[1] != 3 or mask != 0x1F01FF:
                    raise CodexProfileError("codex_profile_windows_acl_unverified")
                observed.add(self._sid_text(ace.value + 8))
            if count != 2 or observed != {self.sid, "S-1-5-18"}:
                raise CodexProfileError("codex_profile_windows_acl_unverified")
        finally:
            self.kernel.LocalFree(descriptor)
        if _directory(path) != identity:
            raise CodexProfileError("codex_profile_storage_changed")
        return identity

    def create_directory(self, path: Path) -> None:
        _directory(path.parent)
        # Atomic creation with protected DACL, not mkdir followed by chmod.
        descriptor = ctypes.c_void_p()
        sddl = f"O:{self.sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.sid})"
        if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise CodexProfileError("codex_profile_windows_acl_unverified")
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        self.kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        try:
            if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                raise CodexProfileError("codex_profile_storage_create_failed")
        finally:
            self.kernel.LocalFree(descriptor)
        self.verify_directory(path)

    def prepare(self, root: Path, profile_id: str, allowed_roots) -> tuple[Path, tuple[int, int], tuple[int, int]]:
        if not root.is_absolute() or uuid.UUID(profile_id).hex != profile_id:
            raise CodexProfileError("codex_profile_storage_unverified")
        for allowed in allowed_roots:
            candidate = Path(allowed).resolve(strict=True)
            if _within(root, candidate) or _within(candidate, root):
                raise CodexProfileError("codex_profile_secret_scope_overlap", 403)
        try:
            root.lstat()
        except FileNotFoundError:
            self.create_directory(root)
        root_identity = self.verify_directory(root)
        home = root / profile_id
        self.create_directory(home)  # Existing/reused home is always refused.
        return home, root_identity, self.verify_directory(home)

    def lock_files(self, paths, *, directory: bool = False) -> list[int]:
        handles = []
        try:
            for path in paths:
                handle = self.kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x200000 | (0x2000000 if directory else 0), None)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise CodexProfileError("codex_profile_artifact_lock_failed")
                handles.append(handle)
            return handles
        except Exception:
            self.unlock_files(handles)
            raise

    def lock_directories(self, paths) -> list[int]:
        return self.lock_files(paths, directory=True)

    def unlock_files(self, handles) -> None:
        for handle in handles:
            self.kernel.CloseHandle(handle)


def windows_program_data() -> Path:
    # Match pinned Codex's known-folder source, not an inherited env string.
    if os.name != "nt":
        raise CodexProfileError("codex_profile_managed_policy_unverified")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97").bytes_le)
    value = w.LPWSTR()
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, w.DWORD, w.HANDLE, ctypes.POINTER(w.LPWSTR)]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    if shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(value)) != 0:
        # Fail closed rather than claiming the CLI's fallback is verified.
        raise CodexProfileError("codex_profile_managed_policy_unverified")
    try:
        path = Path(value.value)
        _directory(path)
        return path
    finally:
        ole.CoTaskMemFree(value)


def require_absent(path: Path) -> None:
    # Verify existing ancestors even when the leaf is missing; never read
    # system/project config, host instructions or credential contents.
    try:
        for parent in reversed(path.parents):
            try:
                parent.lstat()
            except FileNotFoundError:
                continue
            _directory(parent)
        path.lstat()
    except FileNotFoundError:
        return
    except (OSError, ValueError, RuntimeError):
        raise CodexProfileError("codex_profile_managed_policy_unverified") from None
    raise CodexProfileError("codex_profile_managed_policy_present")


def verify_startup_policy(home: Path) -> None:
    system = windows_program_data() / "OpenAI" / "Codex"
    for name in ("config.toml", "requirements.toml"):
        require_absent(system / name)
    for name in ("auth.json", "config.toml", "environments.toml", "managed_config.toml", "AGENTS.md", "AGENTS.override.md"):
        require_absent(home / name)
    for ancestor in (home, *home.parents):
        require_absent(ancestor / ".codex" / "config.toml")
