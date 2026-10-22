"""Portable GUI/CLI. Closing the window stops remote control."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import sys

from . import VERSION
from .client import BridgeClient
from .action_identity import stable_machine_id
from .paths import data_dir
from .browser_setup import (BACKENDS, CHANNELS, BrowserSetupController, connection_config,
                            launch_background, load_config, safe_error, save_config,
                            validate_config)


class ConnectionController:
    """Testable Bridge lifecycle; late worker events never touch a newer GUI run."""
    def __init__(self, config_path, *, client_factory=BridgeClient, launch=launch_background):
        self.config_path, self._factory, self._launch = config_path, client_factory, launch
        self.browser = BrowserSetupController(launch=launch)
        self.media_browser = BrowserSetupController(launch=launch, purpose='media')
        self.browser_purpose = 'general'
        self._view_epoch, self._view_scope = 0, None
        self._view_targets, self._view_profiles = [], []
        self.client = self.run_thread = self.stop_thread = None
        self._events, self._generation = queue.Queue(), 0
        self.status = '未接続'
        self.stopping = self.closed = False

    @staticmethod
    def _alive(thread):
        return thread is not None and thread.is_alive()

    @property
    def active(self):
        return self.client is not None or self._alive(self.run_thread) or self._alive(self.stop_thread)

    def select_browser_purpose(self, purpose):
        if purpose not in {'general', 'media'}:
            raise ValueError('bridge_browser_purpose_invalid')
        if purpose != self.browser_purpose:
            self.browser_purpose = purpose
            self._view_epoch += 1
            self._view_scope = None
            self._view_targets, self._view_profiles = [], []

    def current_browser_setup(self):
        return self.media_browser if self.browser_purpose == 'media' else self.browser

    def select_browser_target(self, scope, tab_id):
        if (scope is None or scope != self._view_scope
                or not any(row['tab_id'] == tab_id for row in self._view_targets)):
            return False
        return self.current_browser_setup().select(tab_id)

    def open_browser_profile(self, scope, account_id):
        if (scope is None or scope != self._view_scope or self.browser_purpose != 'media'
                or not any(row['platform_account_id'] == account_id for row in self._view_profiles)):
            return False
        return self.media_browser.open_profile(account_id)

    def start(self, value):
        if self.closed or self.active:
            self.status = '接続中または切断中です。停止してから再接続してください。'
            return False
        value = validate_config(value, require_connection=True)
        if value.get('browser_backend') == 'agent_browser':
            # A prior browser target can disappear between GUI lifetimes.
            # Headless callers may still supply an explicit known target.
            value.setdefault('agent_browser', {}).pop('target_id', None)
        save_config(self.config_path, value)
        self._generation += 1
        generation = self._generation
        emit = lambda message: self._events.put((generation, 'status', message))
        client = self._factory(value, emit)
        self.client, self.stopping = client, False
        self.browser.bind(client)
        self.media_browser.bind(client)
        self.status = '接続中…'

        def run():
            try:
                client.run()
            except Exception:
                emit('接続を確認できませんでした')
            finally:
                try:
                    client.stop()
                finally:
                    self._events.put((generation, 'finished', None))

        self.run_thread = self._launch(run)
        return True

    def stop(self):
        self._generation += 1
        self.browser.bind(None)
        self.media_browser.bind(None)
        if self.stopping:
            return
        client = self.client
        self.stopping = True
        self.status = '切断中…'
        if client is not None:
            self.stop_thread = self._launch(client.stop)

    def close(self):
        self.closed = True
        self.stop()

    @property
    def can_close(self):
        return self.closed and not self._alive(self.run_thread) and not self._alive(self.stop_thread)

    def poll(self):
        while True:
            try:
                generation, kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation:
                continue
            if kind == 'status':
                self.status = value
            elif kind == 'finished':
                self.client = None
                self.browser.bind(None)
                self.media_browser.bind(None)
                self.status = '停止しました'
        if self.stopping and not self._alive(self.run_thread) and not self._alive(self.stop_thread):
            self.client, self.stopping = None, False
            self.status = '停止しました'
        general, media = self.browser.poll(), self.media_browser.poll()
        view = media if self.browser_purpose == 'media' else general
        self._view_scope = (self.browser_purpose, self._view_epoch)
        self._view_targets, self._view_profiles = view['targets'], view['profiles']
        return {**view, 'scope': self._view_scope}


def gui(config_path):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from .license_bundle import LicenseBundleError, export_notices, notices_text
    config_error = ''
    try:
        config = load_config(config_path) if config_path.exists() else {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        config = {}
        config_error = '保存済み設定を読み込めません。設定を再入力または読込してください。'
    root = tk.Tk()
    root.title('AoiTalk PC Bridge')
    root.geometry('730x700')
    root.minsize(640, 510)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    # Only settings scroll; connect/target/status controls remain visible even
    # on a 768px display. Configure events change layout, never browser state.
    settings_area = ttk.Frame(root)
    settings_area.grid(row=0, column=0, sticky='nsew')
    canvas = tk.Canvas(settings_area, highlightthickness=0)
    scrollbar = ttk.Scrollbar(settings_area, orient='vertical', command=canvas.yview)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    frame = ttk.Frame(canvas, padding=20)
    window = canvas.create_window((0, 0), window=frame, anchor='nw')
    frame.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
    frame.columnconfigure(0, weight=1)
    controls = ttk.Frame(root, padding=(20, 0, 20, 15))
    controls.grid(row=1, column=0, sticky='ew')
    controls.columnconfigure(0, weight=1)
    ttk.Label(frame, text='AoiTalk PC Bridge', font=('Yu Gothic UI', 18, 'bold')).grid(row=0, column=0, sticky='w')
    ttk.Label(frame, text='このPCのEdge・Windows操作をAoiTalkに接続します。').grid(row=1, column=0, sticky='w', pady=(2, 12))
    server = tk.StringVar(value=config.get('server_url', ''))
    token = tk.StringVar(value=config.get('token', ''))
    ca = tk.StringVar(value=config.get('ca_file', ''))
    settings = config.get('agent_browser', {})
    backend = tk.StringVar(value='agent_browser')
    channel = tk.StringVar(value=settings.get('channel', 'stable'))
    endpoint = tk.StringVar(value=settings.get('endpoint', ''))
    profile = tk.StringVar(value=settings.get('user_data_dir', ''))
    editable = []
    for row, title, variable, mask in [(2, 'AoiTalkサーバーURL（インターネット経由はHTTPS）', server, ''),
                                      (4, '接続コード（AoiTalk設定 → PC接続で発行）', token, '•'),
                                      (6, '独自CA証明書ファイル（通常は空欄）', ca, '')]:
        ttk.Label(frame, text=title).grid(row=row, column=0, sticky='w')
        entry = ttk.Entry(frame, textvariable=variable, show=mask)
        entry.grid(row=row + 1, column=0, sticky='ew', pady=(2, 7))
        editable.append((entry, 'normal'))
    browser_frame = ttk.LabelFrame(frame, text='ブラウザ接続（Operationsは専用profile）', padding=10)
    browser_frame.grid(row=8, column=0, sticky='ew', pady=5)
    browser_frame.columnconfigure(1, weight=1)
    ttk.Label(browser_frame, text='接続方式').grid(row=0, column=0, sticky='w')
    backend_box = ttk.Combobox(browser_frame, textvariable=backend, values=BACKENDS, state='readonly')
    backend_box.grid(row=0, column=1, sticky='ew')
    editable.append((backend_box, 'readonly'))
    ttk.Label(browser_frame, text='Edgeチャネル').grid(row=1, column=0, sticky='w')
    channel_box = ttk.Combobox(browser_frame, textvariable=channel, values=CHANNELS, state='readonly')
    channel_box.grid(row=1, column=1, sticky='ew', pady=4)
    editable.append((channel_box, 'readonly'))
    for row, title, variable in [(2, '接続先WS（通常は空欄）', endpoint),
                                 (3, 'User Dataフォルダ（任意）', profile)]:
        ttk.Label(browser_frame, text=title).grid(row=row, column=0, sticky='w')
        entry = ttk.Entry(browser_frame, textvariable=variable, show='•' if variable is endpoint else '')
        entry.grid(row=row, column=1, sticky='ew', pady=4)
        editable.append((entry, 'normal'))
    ttk.Label(browser_frame, text='agent_browserは専用helperが必要です。読込済みのsnapshot_scopeは保持します。\nEdgeの許可・セキュリティ設定は自動変更しません。', wraplength=620).grid(row=4, column=0, columnspan=2, sticky='w')
    saved_actions = config.get('saved_actions', {})
    if isinstance(saved_actions, dict) and isinstance(saved_actions.get('actions', saved_actions), dict):
        saved_action_count = len(saved_actions.get('actions', saved_actions))
    elif isinstance(saved_actions, list):
        saved_action_count = len(saved_actions)
    else:
        saved_action_count = 0
    saved_action_status = tk.StringVar(value=(
        f'保存Action catalog: {saved_action_count}件（operator承認済みの絶対パス・SHA-256・mode・probeのみ）'
        if saved_action_count else
        '保存Action catalog: 未設定（設定ファイルへ明示登録したActionだけ実行できます）'))
    ttk.Label(frame, textvariable=saved_action_status, wraplength=620).grid(
        row=13, column=0, sticky='w', pady=(4, 8))
    ttk.Label(frame, text='stable machine_id（PC登録時にサーバーへ明示登録）: ' +
              str(config.get('machine_id') or stable_machine_id()), wraplength=620).grid(
        row=14, column=0, sticky='w', pady=(0, 8))
    controller = ConnectionController(config_path)
    controller.status = config_error or '未接続'
    status = tk.StringVar(value=controller.status)
    browser_status = tk.StringVar(value='Edge未接続・許可状態は未確認')
    purpose = tk.StringVar(value='通常操作')
    target = tk.StringVar()
    target_rows = []
    account_choice = tk.StringVar()
    profile_rows = []
    displayed_scope = None

    def stop():
        controller.stop()

    def start():
        nonlocal config
        try:
            value = connection_config(config, server_url=server.get(), token=token.get(), ca_file=ca.get(),
                                      backend=backend.get(), channel=channel.get(), endpoint=endpoint.get(),
                                      user_data_dir=profile.get())
            if controller.start(value):
                config = value
        except Exception as exc:
            controller.status = '接続設定を確認してください (' + safe_error(exc) + ')'

    def import_config():
        nonlocal config
        if controller.active:
            controller.status = '切断してから設定ファイルを読み込んでください'
            return
        path = filedialog.askopenfilename(filetypes=[('Bridge設定', '*.json')])
        if path:
            try:
                value = load_config(Path(path))
                # Complete validation precedes every visible mutation.
                validate_config(value, require_connection=True)
                config = value
                server.set(value['server_url'])
                token.set(value['token'])
                ca.set(value.get('ca_file', ''))
                backend.set('agent_browser')
                local = value.get('agent_browser', {})
                channel.set(local.get('channel', 'stable'))
                endpoint.set(local.get('endpoint', ''))
                profile.set(local.get('user_data_dir', ''))
                imported_actions = value.get('saved_actions', {})
                if isinstance(imported_actions, dict) and isinstance(imported_actions.get('actions', imported_actions), dict):
                    imported_count = len(imported_actions.get('actions', imported_actions))
                elif isinstance(imported_actions, list):
                    imported_count = len(imported_actions)
                else:
                    imported_count = 0
                saved_action_status.set(
                    f'保存Action catalog: {imported_count}件（operator承認済みの絶対パス・SHA-256・mode・probeのみ）'
                    if imported_count else
                    '保存Action catalog: 未設定（設定ファイルへ明示登録したActionだけ実行できます）')
                controller.status = '設定を読み込みました。接続時に保存します。'
            except Exception:
                controller.status = '設定ファイルを読み込めませんでした'

    def select_target():
        index = target_box.current()
        if 0 <= index < len(target_rows):
            controller.select_browser_target(displayed_scope, target_rows[index]['tab_id'])

    def open_profile():
        index = account_box.current()
        if 0 <= index < len(profile_rows):
            controller.open_browser_profile(displayed_scope, profile_rows[index]['platform_account_id'])

    def show_licenses():
        try:
            text = notices_text()
        except (LicenseBundleError, OSError):
            messagebox.showerror('ライセンス', 'このビルドのライセンス情報を検証できません。', parent=root)
            return
        window = tk.Toplevel(root)
        window.title('ライセンス・ソース入手先')
        window.geometry('800x600')
        body = ttk.Frame(window)
        body.pack(fill='both', expand=True)
        view = tk.Text(body, wrap='word')
        scroll = ttk.Scrollbar(body, orient='vertical', command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        view.pack(side='left', fill='both', expand=True)
        view.insert('1.0', text)
        view.configure(state='disabled')

        def export():
            chosen = filedialog.asksaveasfilename(parent=window, title='ライセンスを新規ZIPに書出し',
                                                 initialfile='AoiTalk-PC-Bridge-licenses.zip',
                                                 defaultextension='.zip', filetypes=[('ZIP', '*.zip')])
            if not chosen:
                return
            try:
                export_notices(Path(chosen))
            except (LicenseBundleError, OSError):
                messagebox.showerror('ライセンス', '書出せませんでした。既存ファイルを上書きしない新しい保存先を選んでください。', parent=window)
            else:
                messagebox.showinfo('ライセンス', 'ライセンス原文とソース情報をZIPに書出しました。', parent=window)

        ttk.Button(window, text='原文・ソース情報を書出し', command=export).pack(pady=8)

    buttons = ttk.Frame(controls)
    buttons.grid(row=0, column=0, sticky='w', pady=7)
    start_button = ttk.Button(buttons, text='接続', command=start)
    start_button.pack(side='left', padx=(0, 8))
    ttk.Button(buttons, text='切断', command=stop).pack(side='left', padx=(0, 8))
    import_button = ttk.Button(buttons, text='設定ファイル読込', command=import_config)
    import_button.pack(side='left')
    ttk.Button(buttons, text='ライセンス', command=show_licenses).pack(side='right')
    purpose_box = ttk.Combobox(controls, textvariable=purpose,
                              values=('通常操作', 'X／YouTube素材準備'), state='readonly')
    purpose_box.grid(row=1, column=0, sticky='ew', pady=4)
    account_frame = ttk.Frame(controls)
    account_frame.grid(row=2, column=0, sticky='ew', pady=4)
    account_frame.columnconfigure(0, weight=1)
    account_box = ttk.Combobox(account_frame, textvariable=account_choice, state='disabled')
    account_box.grid(row=0, column=0, sticky='ew')
    open_button = ttk.Button(account_frame, text='専用ブラウザを開く', command=open_profile)
    open_button.grid(row=0, column=1, padx=(8, 0))
    discover_button = ttk.Button(controls, text='対象タブを確認', command=lambda: controller.current_browser_setup().discover())
    discover_button.grid(row=3, column=0, sticky='w', pady=4)
    target_frame = ttk.Frame(controls)
    target_frame.grid(row=4, column=0, sticky='ew', pady=4)
    target_frame.columnconfigure(0, weight=1)
    target_box = ttk.Combobox(target_frame, textvariable=target, state='readonly')
    target_box.grid(row=0, column=0, sticky='ew')
    select_button = ttk.Button(target_frame, text='このタブを選択', command=select_target)
    select_button.grid(row=0, column=1, padx=(8, 0))
    ttk.Label(controls, textvariable=browser_status, wraplength=590).grid(row=5, column=0, sticky='w', pady=6)
    ttk.Label(controls, textvariable=status, wraplength=590).grid(row=6, column=0, sticky='w', pady=6)
    ttk.Label(controls, text='接続中は、このPCをAoiTalkから操作できます。閉じると切断します。\n専用profileはこのPCに保存されます。本人がログインし、公開の最終確認を行います。', wraplength=590).grid(row=7, column=0, sticky='w')

    def change_purpose(*_):
        nonlocal target_rows, profile_rows, displayed_scope
        controller.select_browser_purpose('media' if purpose.get() == 'X／YouTube素材準備' else 'general')
        target_rows, profile_rows, displayed_scope = [], [], None
        target.set('')
        account_choice.set('')
        target_box.configure(values=(), state='disabled')
        account_box.configure(values=(), state='disabled')
        select_button.configure(state='disabled')
        open_button.configure(state='disabled')
        browser_status.set(purpose.get() + ': 対象タブを確認してください')

    # A variable trace runs synchronously with the value change, before the
    # next click or 150ms poll can use rows rendered for the previous purpose.
    purpose.trace_add('write', change_purpose)

    def poll():
        nonlocal target_rows, profile_rows, displayed_scope
        view = controller.poll()
        displayed_scope = view['scope']
        status.set(controller.status)
        browser_status.set(purpose.get() + ': ' + view['text'])
        account_box.configure(state='readonly' if view['open_enabled'] else 'disabled')
        open_button.configure(state='normal' if view['open_enabled'] else 'disabled')
        if profile_rows != view['profiles']:
            profile_rows = view['profiles']
            account_box.configure(values=[f"{row['project_name']} / {row['persona_name']} / {row['service']} / {row['display_name']} / {row['platform_account_id']}" for row in profile_rows])
            account_choice.set('人格・アカウントを明示選択' if profile_rows else '')
        discover_button.configure(state='normal' if view['discover_enabled'] else 'disabled')
        select_button.configure(state='normal' if view['select_enabled'] else 'disabled')
        target_box.configure(state='readonly' if view['select_enabled'] else 'disabled')
        if target_rows != view['targets']:
            previous = target_box.current()
            previous_id = target_rows[previous]['tab_id'] if 0 <= previous < len(target_rows) else None
            target_rows = view['targets']
            target_box.configure(values=[f"タブID {row['tab_id']}: {row['title'] or 'タイトルなし'} | {row['origin']}" + ('（選択済み）' if row['selected'] else '')
                                         for index, row in enumerate(target_rows)])
            chosen = next((i for i, row in enumerate(target_rows) if row['selected']), None)
            if chosen is None:
                chosen = next((i for i, row in enumerate(target_rows) if row['tab_id'] == previous_id), None)
            if chosen is None:
                target.set('操作するタブを選択してください' if target_rows else '')
            else:
                target_box.current(chosen)
        for widget, normal in editable:
            widget.configure(state='disabled' if controller.active or controller.closed else normal)
        start_button.configure(state='disabled' if controller.active or controller.closed else 'normal')
        import_button.configure(state='disabled' if controller.active or controller.closed else 'normal')
        if controller.can_close:
            root.destroy()
            return
        root.after(150, poll)

    def close():
        controller.close()
        # Keep the event loop alive while stop closes the owned helper.
        status.set('切断中…')

    root.protocol('WM_DELETE_WINDOW', close)
    poll()
    root.mainloop()

def main():
    # Edge passes its extension origin as argv[1]. It must receive protocol
    # bytes on stdout, never a GUI/log line. This executable serves both roles.
    if '--native-host' in sys.argv or any(a.startswith('chrome-extension://') for a in sys.argv[1:]):
        from .native_host import main as native_main
        native_main()
        return
    parser = argparse.ArgumentParser(description='AoiTalk portable Windows PC Bridge')
    parser.add_argument('--config', type=Path, default=data_dir() / 'connection.json')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--install-edge', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--verify-browser-helper', action='store_true')
    parser.add_argument('--verify-profile-driver', action='store_true')
    parser.add_argument('--action-supervisor', action='store_true',
                        help='Run one durable saved-action supervisor (internal)')
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--run-id')
    notices = parser.add_mutually_exclusive_group()
    notices.add_argument('--licenses', action='store_true', help='Show bundled licenses and source availability offline')
    notices.add_argument('--export-licenses', type=Path, metavar='NEW_ZIP', help='Export exact notice/source bytes to a new ZIP')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args()
    if args.licenses or args.export_licenses is not None:
        from .license_bundle import LicenseBundleError, export_notices, notices_text
        try:
            if args.licenses:
                if callable(getattr(sys.stdout, 'reconfigure', None)):
                    sys.stdout.reconfigure(encoding='utf-8')
                print(notices_text())
            else:
                print(str(export_notices(args.export_licenses)))
        except (LicenseBundleError, OSError):
            print('License bundle unavailable, invalid, or export destination already exists.', file=sys.stderr)
            raise SystemExit(1) from None
        return
    if args.action_supervisor:
        # The portable executable starts this hidden worker through
        # ``CREATE_NO_WINDOW``.  It has no GUI and communicates only through
        # the run-scoped SQLite journal.
        if args.journal is None or not args.run_id:
            raise SystemExit(2)
        from .action_supervisor import run_supervisor
        raise SystemExit(run_supervisor(args.journal, args.run_id))
    if args.verify_profile_driver:
        import hashlib
        from .profile_browser_driver import packaged_runtime, PLAYWRIGHT_VERSION, dedicated_runtime_available
        from .paths import extension_source
        try:
            import win32crypt
            _, _, script = packaged_runtime()
            if not dedicated_runtime_available():
                raise ValueError('profile_driver_dedicated_bundle_unavailable')
            encrypted = win32crypt.CryptProtectData(b'profile-driver-probe', None, b'offline-probe', None, None, 1)
            if win32crypt.CryptUnprotectData(encrypted, b'offline-probe', None, None, 1)[1] != b'profile-driver-probe':
                raise ValueError('profile_driver_checkpoint_runtime_unavailable')
            files = [script, *(extension_source() / name for name in ('background.js', 'content.js', 'media-prepare-youtube.js'))]
            print(json.dumps({'verified': True, 'driver_version': 1, 'dedicated_protocol': 1, 'checkpoint_runtime_verified': True, 'playwright_version': PLAYWRIGHT_VERSION,
                'sources': [{'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]}), flush=True)
        except Exception:
            print(json.dumps({'verified': False, 'error': 'profile_driver_bundle_unavailable'}), flush=True)
            raise SystemExit(1) from None
        return
    if args.verify_browser_helper:
        from .browser_backend import load_helper_bundle
        from .agent_browser_adapter import REQUIRED_CAPABILITIES
        try:
            bundle = load_helper_bundle()
        except Exception:
            print(json.dumps({'verified': False, 'error': 'agent_browser_bundle_unavailable'}), flush=True)
            raise SystemExit(1) from None
        print(json.dumps({'verified': True, 'version': VERSION, 'bridge_protocol': 1,
                          'capabilities': sorted(REQUIRED_CAPABILITIES), 'daemon_stamp': bundle.daemon_stamp,
                          'helper': {'version': bundle.manifest.version, 'sha256': bundle.manifest.sha256,
                                     'size_bytes': bundle.manifest.size_bytes}}), flush=True)
        return
    from .desktop import enable_dpi
    enable_dpi()
    if args.install_edge:
        from .install import install_edge
        print(json.dumps(install_edge(), ensure_ascii=True), flush=True)
    elif args.self_test:
        from .desktop import DesktopController, INPUT
        import ctypes
        from pywinauto import Desktop
        assert Desktop is not None
        controller = DesktopController()
        print(json.dumps({'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                          'input_size': ctypes.sizeof(INPUT), 'desktop': controller.bounds(),
                          'window_count': len(controller.windows())}), flush=True)
    elif args.headless:
        value = json.loads(args.config.read_text(encoding='utf-8'))
        client = BridgeClient(value, lambda status: print(status.encode('ascii', 'backslashreplace').decode(), flush=True))
        try:
            client.run()
        except KeyboardInterrupt:
            client.stop()
    else:
        if getattr(sys, 'frozen', False) and os.name == 'nt':
            import ctypes
            ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p
            window = ctypes.windll.kernel32.GetConsoleWindow()
            if window:
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(window), 0)
        gui(args.config)
