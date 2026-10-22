"""Local Win32/UIA identity helpers; no server or Gateway runtime dependency."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W


def window_identity(user, hwnd):
    if not hwnd or not user.IsWindow(hwnd):
        raise RuntimeError("computer_stale_window")
    pid, rect = W.DWORD(), W.RECT()
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user.GetWindowThreadProcessId(hwnd, C.byref(pid))
    if not user.GetWindowRect(hwnd, C.byref(rect)):
        raise RuntimeError("computer_window_geometry_unavailable")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.GetProcessTimes.argtypes = [W.HANDLE, *([C.POINTER(W.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        raise RuntimeError("computer_window_identity_unavailable")
    try:
        created, exited, system, cpu = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(system), C.byref(cpu)):
            raise RuntimeError("computer_window_identity_unavailable")
        started = created.dwLowDateTime | (created.dwHighDateTime << 32)
    finally:
        kernel.CloseHandle(handle)
    return {"window_id": int(hwnd), "pid": pid.value, "process_started": started,
            "rect": [rect.left, rect.top, rect.right, rect.bottom]}


def control_identity(item):
    info, rect = item.element_info, item.rectangle()
    return {"runtime_id": list(info.element.GetRuntimeId() or ()),
            "role": info.control_type, "name": (getattr(info.element, "CurrentName", item.window_text()) or ""),
            "automation_id": info.automation_id, "process_id": info.process_id,
            "native_hwnd": info.handle,
            "rect": [rect.left, rect.top, rect.right, rect.bottom]}


def validate_identity(previous, current, *, geometry=True):
    keys = ("window_id", "pid", "process_started")
    if any(previous.get(k) != current.get(k) for k in keys):
        raise RuntimeError("computer_stale_window")
    if geometry and previous["rect"] != current["rect"]:
        raise RuntimeError("computer_window_moved")


def walk_controls(root, walker, *, max_nodes=4000, max_depth=32, cancelled=lambda: None):
    """Lazy traversal; no unbounded FindAll/children list. Includes coverage."""
    coverage = {"scanned_nodes": 0, "truncated": False, "errors": 0}
    def walk():
        stack = [(root, 0)]
        while stack and coverage["scanned_nodes"] < max_nodes:
            cancelled()
            element, depth = stack.pop()
            coverage["scanned_nodes"] += 1
            yield element, depth
            try:
                if depth:
                    sibling = walker.GetNextSiblingElement(element)
                    if sibling:
                        stack.append((sibling, depth))
                child = walker.GetFirstChildElement(element)
                if child:
                    if depth < max_depth:
                        stack.append((child, depth + 1))
                    else:
                        coverage["truncated"] = True
            except Exception:
                coverage["errors"] += 1
                coverage["truncated"] = True
        coverage["truncated"] |= bool(stack)
    return walk(), coverage


def monitor_layout(user):
    rectangles = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, C.POINTER(W.RECT), W.LPARAM)
    @callback_type
    def collect(_monitor, _dc, pointer, _data):
        rect = pointer.contents
        rectangles.append([rect.left, rect.top, rect.right, rect.bottom])
        return True
    user.EnumDisplayMonitors.argtypes = [W.HDC, C.POINTER(W.RECT), callback_type, W.LPARAM]
    if not user.EnumDisplayMonitors(None, None, collect, 0):
        raise RuntimeError('computer_monitor_layout_unavailable')
    return sorted(rectangles)


def value_read_only(item):
    """None means no Value pattern, as with some editable Text/Document providers."""
    try:
        return bool(item.iface_value.CurrentIsReadOnly)
    except (AttributeError, NotImplementedError):
        return None
    except Exception as exc:
        # pywinauto is imported only by the Windows worker, not the server.
        # Preserve Text-pattern-only editors; real provider failures propagate.
        if type(exc).__name__ != 'NoPatternInterfaceError':
            raise
        return None
