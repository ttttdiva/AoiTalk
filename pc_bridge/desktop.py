"""Windows desktop observation/UIA and physical-coordinate SendInput actions."""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
from io import BytesIO
import math
import json
import os
import time
import threading
from uuid import uuid4
from .desktop_identity import control_identity, validate_identity, walk_controls, window_identity, monitor_layout, value_read_only


MAX_CONTROL_BYTES = 512_000


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD),
                ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD),
                ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [('uMsg', W.DWORD), ('wParamL', W.WORD), ('wParamH', W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('type', W.DWORD), ('value', INPUTUNION)]


KEYS = {'CTRL': 0x11, 'CONTROL': 0x11, 'SHIFT': 0x10, 'ALT': 0x12,
        'WIN': 0x5B, 'ENTER': 0x0D, 'RETURN': 0x0D, 'TAB': 9,
        'ESC': 0x1B, 'ESCAPE': 0x1B, 'SPACE': 0x20, 'BACKSPACE': 8,
        'DELETE': 0x2E, 'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28,
        'HOME': 0x24, 'END': 0x23, 'PAGEUP': 0x21, 'PAGEDOWN': 0x22,
        **{f'F{i}': 0x6F + i for i in range(1, 25)}}


def keyboard_input(vk=0, scan=0, flags=0):
    return INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def enable_dpi():
    if os.name == 'nt':
        try:
            C.windll.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        except (AttributeError, OSError):
            C.windll.user32.SetProcessDPIAware()


class DesktopController:
    def __init__(self, stop_event=None):
        self.stop_event = stop_event or threading.Event()
        if os.name != 'nt':
            raise RuntimeError('computer_use_requires_windows')
        self.u = C.WinDLL('user32', use_last_error=True)
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.IsWindow.argtypes = [W.HWND]
        self.u.IsWindowVisible.argtypes = [W.HWND]
        self.u.GetWindowTextLengthW.argtypes = [W.HWND]
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.u.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
        self.u.SendInput.restype = W.UINT
        self.targets = {}
        self.target_identities = {}
        self.observation = None
        self._input_guard = None
        import pythoncom
        pythoncom.CoInitialize()

    def check_cancelled(self):
        if getattr(self, "stop_event", None) is not None and self.stop_event.is_set():
            raise RuntimeError("computer_action_cancelled")

    def bounds(self):
        return {'x': self.u.GetSystemMetrics(76), 'y': self.u.GetSystemMetrics(77),
                'width': self.u.GetSystemMetrics(78), 'height': self.u.GetSystemMetrics(79)}

    def screenshot(self):
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        original = self.bounds()
        image.thumbnail((1600, 1200))
        output = BytesIO()
        image.convert('RGB').save(output, format='JPEG', quality=78)
        return {'desktop': original, 'screenshot': {
            'mime_type': 'image/jpeg', 'width': image.width, 'height': image.height,
            'data': base64.b64encode(output.getvalue()).decode('ascii'),
        }}

    def windows(self):
        result = []
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def collect(hwnd, _):
            if self.u.IsWindowVisible(hwnd):
                length = self.u.GetWindowTextLengthW(hwnd)
                if length:
                    title = C.create_unicode_buffer(length + 1)
                    self.u.GetWindowTextW(hwnd, title, len(title))
                    rect = W.RECT()
                    self.u.GetWindowRect(hwnd, C.byref(rect))
                    try:
                        identity = self.window_identity(hwnd)
                    except RuntimeError:
                        identity = {'window_id': int(hwnd), 'rect': [rect.left, rect.top, rect.right, rect.bottom]}
                    result.append({**identity, 'title': title.value[:300]})
            return True
        self.u.EnumWindows(callback_type(collect), 0)
        return result[:4096]

    def window_identity(self, hwnd):
        return window_identity(self.u, hwnd)

    @staticmethod
    def automation():
        from pywinauto.uia_defines import IUIA
        return IUIA().iuia

    @staticmethod
    def wrap(element):
        from pywinauto.uia_element_info import UIAElementInfo
        from pywinauto.controls.uiawrapper import UIAWrapper
        return UIAWrapper(UIAElementInfo(element))

    def observe(self, screenshot=True):
        windows = self.windows()
        foreground = int(self.u.GetForegroundWindow() or 0)
        owner = self.window_identity(foreground) if foreground else None
        self.targets, self.target_identities = {}, {}
        controls, error = {}, ''
        control_bytes = 0
        nonce = uuid4().hex
        coverage = {"scanned_nodes": 0, "truncated": False, "errors": 0}
        try:
            automation = self.automation()
            root = automation.ElementFromHandle(foreground)
            traversal, coverage = walk_controls(root, automation.ControlViewWalker, cancelled=self.check_cancelled)
            for element, depth in traversal:
                try:
                    item = self.wrap(element)
                    identity = control_identity(item)
                    rect = identity['rect']
                    if not item.is_visible() or rect[2] <= rect[0] or rect[3] <= rect[1]:
                        continue
                    key = f'{nonce}:{len(controls)}'
                    rid = identity['runtime_id']
                    entry = {'label': identity['name'][:300], 'type': identity['role'],
                             'rect': rect, 'enabled': item.is_enabled(), 'depth': depth,
                             'runtime_id': rid, 'automation_id': identity['automation_id'],
                             'actionable': bool(rid)}
                    if identity['role'] in ('Edit', 'Document') and not element.CurrentIsPassword:
                        try:
                            entry['value'] = item.iface_value.CurrentValue[:2000]
                            entry['readonly'] = bool(item.iface_value.CurrentIsReadOnly)
                        except Exception:
                            try:
                                entry['value'] = item.iface_text.DocumentRange.GetText(2000)
                            except Exception:
                                pass
                    entry_bytes = len(json.dumps(entry, ensure_ascii=False).encode('utf-8')) + len(key) + 8
                    if control_bytes + entry_bytes > MAX_CONTROL_BYTES:
                        coverage['truncated'] = True
                        coverage['budget'] = 'control_bytes'
                        break
                    control_bytes += entry_bytes
                    coverage['control_bytes'] = control_bytes
                    controls[key] = entry
                    self.targets[key], self.target_identities[key] = item, identity
                except Exception as exc:
                    self.check_cancelled()
                    if len(coverage.setdefault('error_types', [])) < 20:
                        coverage['error_types'].append(type(exc).__name__)
                    coverage['errors'] += 1
                    coverage['truncated'] = True
            counts = {}
            for identity in self.target_identities.values():
                rid = tuple(identity['runtime_id'])
                counts[rid] = counts.get(rid, 0) + 1
            for key, entry in controls.items():
                if counts[tuple(entry['runtime_id'])] > 1:
                    entry['actionable'] = False
                    self.targets.pop(key, None)
                    self.target_identities.pop(key, None)
        except Exception as exc:
            self.check_cancelled()
            error = type(exc).__name__
            coverage['truncated'] = True
        if owner:
            validate_identity(owner, self.window_identity(foreground))
            if int(self.u.GetForegroundWindow() or 0) != foreground:
                raise RuntimeError('computer_foreground_changed')
        self.observation = {'id': nonce, 'owner': owner, 'windows': windows, 'desktop': self.bounds(), 'monitors': monitor_layout(self.u)}
        result = {'windows': windows, 'foreground_window_id': foreground, 'controls': controls,
                  'desktop': self.bounds(), 'accessibility_error': error,
                  'observation_id': nonce, 'foreground_identity': owner, 'coverage': coverage}
        if screenshot:
            result.update(self.screenshot())
            if owner:
                validate_identity(owner, self.window_identity(foreground))
        return result

    def guard_observation(self, action, params):
        oid = params.get('observation_id')
        if not oid and not params.get('target'):
            return None  # Explicit, unbound legacy single-input calls remain available.
        snapshot = self.observation
        if not snapshot or (oid and oid != snapshot['id']):
            raise RuntimeError('computer_stale_observation')
        if snapshot.get('monitors') is not None and snapshot['monitors'] != monitor_layout(self.u):
            raise RuntimeError('computer_monitor_layout_changed')
        if snapshot['desktop'] != self.bounds():
            raise RuntimeError('computer_monitor_layout_changed')
        if action == 'focus':
            previous = next((w for w in snapshot['windows'] if w['window_id'] == params.get('window_id')), None)
            if not previous or 'pid' not in previous:
                raise RuntimeError('computer_stale_window')
            validate_identity(previous, self.window_identity(previous['window_id']), geometry=False)
            return previous
        owner = snapshot['owner']
        if not owner or int(self.u.GetForegroundWindow() or 0) != owner['window_id']:
            raise RuntimeError('computer_foreground_changed')
        validate_identity(owner, self.window_identity(owner['window_id']))
        return owner

    @staticmethod
    def uia_point(x, y):
        from pywinauto.uia_defines import IUIA
        return IUIA().UIA_dll.tagPOINT(x, y)

    def guard_target(self, target, action):
        item = self.targets.get(target)
        old = self.target_identities.get(target)
        if item is None or not old or not old['runtime_id']:
            raise RuntimeError('computer_stale_target')
        if old != control_identity(item) or not item.is_visible() or not item.is_enabled():
            raise RuntimeError('computer_stale_target')
        if action == 'type':
            if old['role'] not in ('Edit', 'Document'):
                raise RuntimeError('computer_target_not_editable')
            if value_read_only(item) is True:
                raise RuntimeError('computer_target_readonly')
        else:
            automation = self.automation()
            r = old['rect']
            point = self.uia_point((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
            hit = automation.ElementFromPoint(point)
            for _ in range(64):
                if not hit:
                    break
                if automation.CompareElements(item.element_info.element, hit):
                    return item
                hit = automation.RawViewWalker.GetParentElement(hit)
            raise RuntimeError('computer_target_covered')
        return item

    def send(self, events):
        guard = getattr(self, '_input_guard', None)
        if guard:
            guard()
        for start in range(0, len(events), 128):
            batch = events[start:start + 128]
            array = (INPUT * len(batch))(*batch)
            count = self.u.SendInput(len(batch), array, C.sizeof(INPUT))
            if count != len(batch):
                raise RuntimeError('windows_input_failed_or_desktop_unavailable')

    def press(self, keys):
        if not isinstance(keys, list) or not keys or len(keys) > 8:
            raise ValueError('keys_requires_a_nonempty_array')
        codes = []
        for key in keys:
            key = str(key).upper()
            code = KEYS.get(key)
            if code is None and len(key) == 1 and key.isascii() and key.isalnum():
                code = ord(key)
            if code is None:
                raise ValueError('unsupported_key:' + key)
            codes.append(code)
        self.u.GetKeyboardLayout.argtypes = [W.DWORD]
        self.u.GetKeyboardLayout.restype = C.c_void_p
        self.u.MapVirtualKeyExW.argtypes = [W.UINT, W.UINT, C.c_void_p]
        self.u.MapVirtualKeyExW.restype = W.UINT
        thread = self.u.GetWindowThreadProcessId(self.u.GetForegroundWindow(), None)
        layout = self.u.GetKeyboardLayout(thread)
        extended = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C}
        downs = [keyboard_input(vk=code, scan=self.u.MapVirtualKeyExW(code, 0, layout),
                                flags=1 if code in extended else 0) for code in codes]
        releases = [keyboard_input(vk=event.ki.wVk, scan=event.ki.wScan,
                                   flags=event.ki.dwFlags | 2) for event in reversed(downs)]
        try:
            # Apps may use the native scan code to translate accelerator keys;
            # a VK-only packet can reach KeyDown but fail to invoke Ctrl+A.
            # One OS input batch prevents another producer splitting the chord.
            self.send(downs + releases)
        except Exception:
            self.send(releases)
            raise


    def type_text(self, text):
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError('invalid_text')
        encoded = text.replace('\r\n', '\n').encode('utf-16-le')
        events = []
        for offset in range(0, len(encoded), 2):
            code = int.from_bytes(encoded[offset:offset + 2], 'little')
            if code in (9, 10, 13):
                key = 9 if code == 9 else 13
                events.extend([keyboard_input(vk=key), keyboard_input(vk=key, flags=2)])
            else:
                events.extend([keyboard_input(scan=code, flags=4), keyboard_input(scan=code, flags=6)])
        # WinUI text controls may resolve VK_PACKET asynchronously. Sending
        # an entire string in one SendInput batch can repeat its final code
        # unit. Give each key-down/up pair a message-pump interval.
        for offset in range(0, len(events), 2):
            self.check_cancelled()
            self.send(events[offset:offset + 2])
            time.sleep(0.015)

    def move(self, x, y):
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('coordinates_required')
        bounds = self.bounds()
        if not (bounds['x'] <= x < bounds['x'] + bounds['width'] and bounds['y'] <= y < bounds['y'] + bounds['height']):
            raise ValueError('coordinates_outside_desktop')
        if not self.u.SetCursorPos(int(x), int(y)):
            raise RuntimeError('windows_cursor_failed')

    def mouse(self, flag, data=0):
        self.send([INPUT(type=0, mi=MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flag, 0, 0))])

    def execute(self, action, params):
        self.check_cancelled()
        if action == 'screenshot':
            return self.screenshot()
        if action == 'observe':
            return self.observe(bool(params.get('screenshot', True)))
        if action == 'windows':
            return {'windows': self.windows()}
        self.guard_observation(action, params)
        target = params.get('target')
        if target:
            item = self.guard_target(target, action)
            rect = item.rectangle()
            params = {**params, 'x': (rect.left + rect.right) // 2, 'y': (rect.top + rect.bottom) // 2}
        if action == 'focus':
            hwnd = params.get('window_id')
            if not isinstance(hwnd, int) or not self.u.IsWindow(hwnd):
                raise ValueError('window_not_found')
            self.u.ShowWindow(hwnd, 9)
            if int(self.u.GetForegroundWindow() or 0) != hwnd:
                self.press(['ALT'])
                time.sleep(0.1)
                self.u.SetForegroundWindow(hwnd)
                time.sleep(0.15)
            if int(self.u.GetForegroundWindow() or 0) != hwnd:
                # A background worker has no GUI input queue initially. Create
                # one, attach only during the explicit focus request, and
                # always detach before returning. Never send text until the
                # requested HWND is actually foreground.
                self.u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
                self.u.GetWindowThreadProcessId.restype = W.DWORD
                self.u.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
                self.u.BringWindowToTop.argtypes = [W.HWND]
                msg = W.MSG()
                self.u.PeekMessageW(C.byref(msg), None, 0, 0, 0)
                current = C.windll.kernel32.GetCurrentThreadId()
                foreground = self.u.GetWindowThreadProcessId(self.u.GetForegroundWindow(), None)
                attached = bool(foreground and foreground != current and self.u.AttachThreadInput(current, foreground, True))
                try:
                    self.u.BringWindowToTop(hwnd)
                    self.u.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        self.u.AttachThreadInput(current, foreground, False)
                time.sleep(0.15)
            if int(self.u.GetForegroundWindow() or 0) != hwnd:
                from pywinauto import Desktop
                Desktop(backend='uia').window(handle=hwnd).set_focus()
                time.sleep(0.15)
            if int(self.u.GetForegroundWindow() or 0) != hwnd:
                raise RuntimeError('window_focus_failed')
        elif action in ('click', 'double_click', 'right_click', 'move', 'drag'):
            self.guard_observation(action, params)
            if target:
                self.guard_target(target, action)
            self.move(params.get('x'), params.get('y'))
            if action == 'drag':
                x, y = params['x'], params['y']
                x2, y2 = params.get('x2'), params.get('y2')
                if not isinstance(x2, (int, float)) or not isinstance(y2, (int, float)):
                    raise ValueError('drag_destination_required')
                self.mouse(2)
                try:
                    for step in range(1, 11):
                        self.check_cancelled()
                        self.move(x + (x2 - x) * step / 10, y + (y2 - y) * step / 10)
                        time.sleep(0.025)
                finally:
                    self.mouse(4)
            elif action != 'move':
                down, up = (8, 16) if action == 'right_click' else (2, 4)
                for _ in range(2 if action == 'double_click' else 1):
                    self.mouse(down)
                    self.mouse(up)
                    time.sleep(0.08)
        elif action == 'type':
            if target:
                # Focus without a click: a preceding CTRL+A selection must
                # remain selected when the next step types replacement text.
                if not item.element_info.element.CurrentHasKeyboardFocus:
                    item.set_focus()
                self.guard_target(target, action)
                if not item.element_info.element.CurrentHasKeyboardFocus:
                    raise RuntimeError('computer_target_focus_failed')
            self.guard_observation(action, params)
            self._input_guard = lambda: self.guard_observation(action, params)
            try:
                self.type_text(params.get('text'))
            finally:
                self._input_guard = None
        elif action == 'press':
            self.press(params.get('keys'))
        elif action == 'scroll':
            amount = params.get('amount', -3)
            if not isinstance(amount, int) or not -100 <= amount <= 100:
                raise ValueError('scroll_amount_invalid')
            self.mouse(0x0800, 120 * amount)
        else:
            raise ValueError('computer_action_unknown')
        if action == 'focus' and params.get('observation_id'):
            previous = next(w for w in self.observation['windows'] if w['window_id'] == params['window_id'])
            validate_identity(previous, self.window_identity(params['window_id']), geometry=False)
        time.sleep(0.1)
        return {'action': action, 'performed': True, 'execution_status': 'accepted',
                'verification': {'status': 'unknown'}, 'observation_id': params.get('observation_id')}
