"""OS protection and read-only orphan checks for Bridge-owned profiles."""
from __future__ import annotations

import os


class ProfileSecurityError(RuntimeError):
    pass


def _windows_acl():
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    return win32security, owner, system


def protect_directory(path):
    try:
        if os.name != "nt":
            path.chmod(0o700)
        else:
            import ntsecuritycon
            security, owner, system = _windows_acl()
            acl = security.ACL()
            flags = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
            for sid in (owner, system):
                acl.AddAccessAllowedAceEx(security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
            security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, acl, None)
        verify_directory(path)
    except Exception:
        raise ProfileSecurityError("browser_profile_permissions_unverified") from None


def verify_directory(path):
    try:
        if os.name != "nt":
            if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
                raise ValueError()
        else:
            import ntsecuritycon
            security, owner, system = _windows_acl()
            descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                                                     security.DACL_SECURITY_INFORMATION)
            control, _ = descriptor.GetSecurityDescriptorControl()
            acl = descriptor.GetSecurityDescriptorDacl()
            if not control & security.SE_DACL_PROTECTED or acl is None or acl.GetAceCount() != 2:
                raise ValueError()
            accepted = set()
            for index in range(acl.GetAceCount()):
                header, access, sid = acl.GetAce(index)
                if (header[0] != security.ACCESS_ALLOWED_ACE_TYPE or access != ntsecuritycon.FILE_ALL_ACCESS
                        or header[1] & (security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE) != 3
                        or sid not in (owner, system)):
                    raise ValueError()
                accepted.add(security.ConvertSidToStringSid(sid))
            if accepted != {security.ConvertSidToStringSid(sid) for sid in (owner, system)}:
                raise ValueError()
    except Exception:
        raise ProfileSecurityError("browser_profile_permissions_unverified") from None


class WindowsRuntimeObserver:
    """Checks recorded birth identities/job membership; never terminates a PID."""
    def __call__(self, record):
        from .windows_helper_job import ProcessIdentity, _WindowsKernel
        try:
            kernel = _WindowsKernel()
            if kernel.named_job_has_processes(record["job_name"]):
                return True
            return any(kernel.recorded_process_alive(ProcessIdentity(item["pid"], item["creation_ticks"]))
                       for item in record["processes"])
        except Exception:
            raise ProfileSecurityError("browser_profile_runtime_unverified") from None
