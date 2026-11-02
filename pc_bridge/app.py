"""Portable GUI/CLI. Closing the window stops remote control."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import re
import sys
import time
from urllib.parse import urlsplit

from . import VERSION
from .client import BridgeClient
from .action_identity import stable_machine_id
from .paths import data_dir
from .browser_setup import (BACKENDS, CHANNELS, BrowserSetupController, connection_config,
                            launch_background, load_config, safe_error, save_config,
                            validate_config)
from .edge_cdp import EdgeCdpError, validate_broker_websocket_endpoint


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

    def discover_browser_targets(self):
        """Run the same local target discovery callback used by the GUI."""
        return self.current_browser_setup().discover()

    def select_browser_target(self, scope, tab_id):
        if (scope is None or scope != self._view_scope
                or not any(row['tab_id'] == tab_id for row in self._view_targets)):
            return False
        return self.current_browser_setup().select(tab_id)

    def open_browser_target(self, scope, url):
        origin = _target_origin_prefix(url)
        if (scope is None or scope != self._view_scope or self.browser_purpose != 'general'
                or url != origin + '/'
                or origin not in {'https://crowdworks.jp', 'https://www.crowdworks.jp'}
                or any(row.get('origin') == origin for row in self._view_targets)):
            return False
        return self.current_browser_setup().open_target(url)

    def open_browser_profile(self, scope, account_id):
        if (scope is None or scope != self._view_scope or self.browser_purpose != 'media'
                or not any(row['platform_account_id'] == account_id for row in self._view_profiles)):
            return False
        return self.media_browser.open_profile(account_id)

    def start(self, value, *, broker_endpoint=None, reconnect_target_policy=None):
        if self.closed or self.active:
            self.status = '接続中または切断中です。停止してから再接続してください。'
            return False
        value = validate_config(value, require_connection=True)
        if any(key.casefold() == 'agent_browser_auto_connect' for key in os.environ):
            raise ValueError('direct_edge_cdp_forbidden')
        runtime_value = dict(value)
        runtime_value['agent_browser'] = dict(value.get('agent_browser', {}))
        if broker_endpoint:
            try:
                runtime_value['agent_browser']['broker_endpoint'] = validate_broker_websocket_endpoint(
                    broker_endpoint.strip())
            except (EdgeCdpError, AttributeError) as exc:
                if isinstance(exc, EdgeCdpError) and str(exc) == 'direct_edge_cdp_forbidden':
                    raise ValueError('direct_edge_cdp_forbidden') from None
                raise ValueError('edge_broker_endpoint_invalid') from None
            runtime_value = validate_config(runtime_value, require_connection=True, allow_broker_endpoint=True)
        if value.get('browser_backend') == 'agent_browser':
            # A prior browser target can disappear between GUI lifetimes.
            # Headless callers may still supply an explicit known target.
            value.setdefault('agent_browser', {}).pop('target_id', None)
            runtime_value.setdefault('agent_browser', {}).pop('target_id', None)
        save_config(self.config_path, value)
        self._generation += 1
        generation = self._generation
        emit = lambda message: self._events.put((generation, 'status', message))
        client = self._factory(runtime_value, emit)
        if reconnect_target_policy is not None:
            setter = getattr(client, 'set_reconnect_target_policy', None)
            if callable(setter):
                setter(reconnect_target_policy['origin'],
                       open_target_if_missing=reconnect_target_policy['open_target_if_missing'])
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
    broker_endpoint = tk.StringVar(value='')
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
    ttk.Label(browser_frame, text='Legacy Edgeチャネル').grid(row=1, column=0, sticky='w')
    channel_box = ttk.Combobox(browser_frame, textvariable=channel, values=CHANNELS, state='readonly')
    channel_box.grid(row=1, column=1, sticky='ew', pady=4)
    editable.append((channel_box, 'readonly'))
    for row, title, variable in [(2, 'Gateway broker endpoint（一時入力・設定には保存しません）', broker_endpoint),
                                 (3, 'Legacy User Dataフォルダ（broker接続では使用しません）', profile)]:
        ttk.Label(browser_frame, text=title).grid(row=row, column=0, sticky='w')
        entry = ttk.Entry(browser_frame, textvariable=variable, show='•' if variable is broker_endpoint else '')
        entry.grid(row=row, column=1, sticky='ew', pady=4)
        editable.append((entry, 'normal'))
    ttk.Label(browser_frame, text='既存EdgeはGateway edge_cdp_brokerのBrowser.bind中継だけを使います。保存済みのchannel/User Data設定はこの接続では参照しません。endpointは一時入力で保存されません。', wraplength=620).grid(row=4, column=0, columnspan=2, sticky='w')
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
                                      backend=backend.get(), channel=channel.get(), broker_endpoint=broker_endpoint.get(),
                                      user_data_dir=profile.get())
            if controller.start(value, broker_endpoint=broker_endpoint.get()):
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
                broker_endpoint.set('')
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
    discover_button = ttk.Button(controls, text='対象タブを確認', command=controller.discover_browser_targets)
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

def _loopback_cdp_origin(value):
    del value
    raise ValueError('direct_edge_cdp_forbidden')


def _target_origin_prefix(value):
    """Limit prefix selection to the origin metadata exposed by local setup."""
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(char) <= 32 for char in value)):
        raise ValueError('browser_setup_target_prefix_invalid')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError('browser_setup_target_prefix_invalid') from None
    if (parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None
            or parsed.password is not None or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('browser_setup_target_prefix_invalid')
    host = f'[{host}]' if ':' in host else host
    return f'{parsed.scheme}://{host}' + (f':{port}' if port else '')


def select_discovered_target(controller, scope, targets, *, target_id=None, url_prefix=None):
    """Use the GUI controller selection path only for one exact local target."""
    if (target_id is None) == (url_prefix is None):
        raise ValueError('browser_setup_target_selector_invalid')
    if target_id is not None:
        if not isinstance(target_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', target_id):
            raise ValueError('browser_setup_target_invalid')
        matches = [row for row in targets if row.get('target_id') == target_id]
    else:
        origin = _target_origin_prefix(url_prefix)
        matches = [row for row in targets if row.get('origin') == origin]
    if len(matches) != 1:
        return {'selected': False, 'match_count': len(matches),
                'total_target_count': len(targets)}
    row = matches[0]
    if not controller.select_browser_target(scope, row['tab_id']):
        raise ValueError('browser_setup_target_invalid')
    return {'selected': True, 'match_count': 1, 'total_target_count': len(targets),
            'tab_id': row['tab_id']}


def _open_target_url(url_prefix):
    origin = _target_origin_prefix(url_prefix)
    if origin not in {'https://crowdworks.jp', 'https://www.crowdworks.jp'}:
        raise ValueError('browser_setup_target_open_scope_invalid')
    return origin + '/'


def _validate_requested_cdp(config, requested_origin):
    del config, requested_origin
    raise ValueError('direct_edge_cdp_forbidden')


def _validate_broker_endpoint(config, broker_endpoint):
    if not isinstance(config, dict) or not isinstance(broker_endpoint, str):
        raise ValueError('edge_broker_endpoint_invalid')
    settings = config.get('agent_browser', {})
    if isinstance(settings, dict) and 'endpoint' in settings:
        raise ValueError('direct_edge_cdp_forbidden')
    if any(key.casefold() == 'agent_browser_auto_connect' for key in os.environ):
        raise ValueError('direct_edge_cdp_forbidden')
    try:
        return validate_broker_websocket_endpoint(broker_endpoint)
    except EdgeCdpError as exc:
        if str(exc) == 'direct_edge_cdp_forbidden':
            raise ValueError('direct_edge_cdp_forbidden') from None
        raise ValueError('edge_broker_endpoint_invalid') from None


def _wait_for_browser_setup(controller, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = controller.poll()
        setup = controller.current_browser_setup()
        if not setup.busy:
            return view
        time.sleep(0.05)
    raise TimeoutError('browser_setup_timeout')


def _headless_setup_error(setup, targets):
    """Return a fixed safe diagnostic from the setup model shared with the GUI."""
    candidates = [getattr(setup, 'error', None)]
    diagnostics = getattr(setup, 'diagnostics', None)
    if isinstance(diagnostics, dict):
        candidates.append(diagnostics.get('error'))
    had_error = False
    for candidate in candidates:
        if not candidate:
            continue
        had_error = True
        code = safe_error(candidate)
        if code != 'browser_setup_failed':
            return code
    if had_error:
        return 'browser_setup_failed'
    if not targets:
        return 'agent_browser_no_targets'
    return None


class HeadlessBrowserSetupError(RuntimeError):
    """Static CLI failure record; never carries an exception message."""
    def __init__(self, phase, code, *, diagnostics=None, connection_method="unknown"):
        self.phase = phase if phase in {'bridge_start', 'discovery', 'target_select', 'selection'} else 'setup'
        self.code = safe_error(code)
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        safe_operations = {'connect', 'health_check', 'target_list', 'other'}
        safe_browser_codes = {
            'unavailable', 'invalid_endpoint', 'invalid_arguments', 'connection_blocked',
            'connection_required', 'target_unavailable', 'EXACT_TARGET_REQUIRED', 'STALE_REF',
            'STRICT_REF_REQUIRED', 'DIALOG_BLOCKED', 'FOCUS_CHANGED', 'tab_gone', 'rebind_pending',
            'edge_broker_endpoint_required', 'edge_broker_endpoint_invalid',
            'edge_broker_client_connect_failed', 'edge_broker_default_context_unavailable',
            'edge_broker_client_command_failed', 'edge_broker_event_monitor_unavailable',
            'edge_broker_browser_disconnected', 'edge_broker_browser_context_closed',
            'agent_browser_session_faulted', 'agent_browser_target_unavailable',
            'agent_browser_target_gone', 'agent_browser_rebind_pending',
            'agent_browser_operation_still_running',
            'agent_browser_target_open_failed', 'agent_browser_target_open_scope_invalid',
            'browser_setup_target_open_scope_invalid', 'browser_setup_target_open_rejected',
            'unrecognized',
        }
        safe_connection_methods = {'gateway_browser_bind_websocket', 'unknown'}
        operation = diagnostics.get('browser_operation')
        browser_error_code = diagnostics.get('browser_error_code')
        reported_method = diagnostics.get('connection_method', connection_method)
        self.diagnostics = {
            'connection_method': reported_method if reported_method in safe_connection_methods else 'unknown',
            'browser_operation': operation if operation in safe_operations else None,
            'browser_error_code': browser_error_code if browser_error_code in safe_browser_codes else None,
        }
        super().__init__(self.code)


def headless_failure_record(error):
    if isinstance(error, HeadlessBrowserSetupError):
        phase, code = error.phase, error.code
    else:
        phase, code = 'setup', safe_error(error)
    diagnostics = error.diagnostics if isinstance(error, HeadlessBrowserSetupError) else {
        'connection_method': 'unknown', 'browser_operation': None, 'browser_error_code': None,
    }
    return {'status': 'edge_target_setup_failed', 'phase': phase, 'error': code, **diagnostics}


def _safe_reconnect_record(value):
    """Project a reconnect event to fixed metadata only; never include tab data."""
    if not isinstance(value, dict):
        return None
    phases = {'playwright_disconnect', 'playwright_outcome_unknown', 'broker_status', 'broker_endpoint',
              'playwright_attach', 'target_select', 'ready'}
    operations = {'connect', 'health_check', 'target_list', 'target_select', 'target_open',
                  'read', 'verify', 'navigate', 'action', 'other'}
    phase = value.get('phase')
    operation = value.get('operation')
    method = value.get('connection_method')
    if phase not in phases or operation not in operations or method != 'gateway_browser_bind_websocket':
        return None
    error_code = value.get('error_code')
    if error_code is not None:
        if not isinstance(error_code, str) or not re.fullmatch(
                r'(?:edge_broker|browser_setup|agent_browser)_[a-z0-9_]{1,64}', error_code):
            error_code = 'browser_setup_recovery_failed'
    record = {
        'phase': phase,
        'error_code': error_code,
        'operation': operation,
        'connection_method': method,
    }
    for key in ('match_count', 'total_target_count', 'target_open_attempt_count'):
        count = value.get(key)
        if type(count) is int and count >= 0 and (key != 'target_open_attempt_count' or count <= 1):
            record[key] = count
    return record


def _safe_reconnect_transition(value):
    """Allowlist one recovery transition without tab or transport values."""
    if not isinstance(value, dict):
        return None
    events = {'disconnect_detected', 'reconnect_attempt', 'reconnect_ok', 'reconnect_failed', 'terminal'}
    phases = {'playwright_disconnect', 'playwright_outcome_unknown', 'broker_status', 'broker_endpoint',
              'playwright_attach', 'target_select', 'ready', 'dispatch_lock', 'playwright_quiescence'}
    operations = {'connect', 'health_check', 'target_list', 'target_select', 'target_open',
                  'read', 'verify', 'navigate', 'action', 'other'}
    event, phase, operation = value.get('event'), value.get('phase'), value.get('operation')
    if (event not in events or phase not in phases or operation not in operations
            or value.get('connection_method') != 'gateway_browser_bind_websocket'):
        return None
    code = value.get('error_code')
    if code is not None and (not isinstance(code, str) or not re.fullmatch(
            r'(?:edge_broker|browser_setup|agent_browser|browser_recovery)_[a-z0-9_]{1,64}', code)):
        code = 'browser_setup_recovery_failed'
    attempt = value.get('attempt')
    record = {
        'event': event,
        'attempt': attempt if type(attempt) is int and 1 <= attempt <= 1_000_000 else None,
        'phase': phase,
        'error_code': code,
        'operation': operation,
        'connection_method': 'gateway_browser_bind_websocket',
    }
    for key in ('match_count', 'total_target_count', 'target_open_attempt_count'):
        count = value.get(key)
        if type(count) is int and count >= 0 and (key != 'target_open_attempt_count' or count <= 1):
            record[key] = count
    return record


def run_headless_edge_setup(config_path, broker_endpoint, *, target_id=None, url_prefix=None,
                            open_target_if_missing=False,
                            client_factory=BridgeClient, launch=launch_background,
                            wait_for_setup=_wait_for_browser_setup, stop_wait=30):
    """Start the Bridge and use only the Gateway's bound Playwright broker."""
    if (target_id is None) == (url_prefix is None):
        raise ValueError('browser_setup_target_selector_invalid')
    if open_target_if_missing and (target_id is not None or url_prefix is None):
        raise ValueError('browser_setup_target_open_selector_required')
    config = load_config(config_path)
    validate_config(config, require_connection=True)
    broker_endpoint = _validate_broker_endpoint(config, broker_endpoint)
    connection_method = 'gateway_browser_bind_websocket'
    controller = ConnectionController(config_path, client_factory=client_factory, launch=launch)
    reconnect_policy = None
    if url_prefix is not None:
        reconnect_policy = {
            'origin': _target_origin_prefix(url_prefix),
            'open_target_if_missing': open_target_if_missing,
        }
    if not controller.start(config, broker_endpoint=broker_endpoint,
                            reconnect_target_policy=reconnect_policy):
        raise HeadlessBrowserSetupError('bridge_start', 'bridge_connection_start_rejected')
    try:
        if not controller.discover_browser_targets():
            setup = controller.current_browser_setup()
            raise HeadlessBrowserSetupError(
                'discovery', _headless_setup_error(setup, getattr(setup, 'targets', []))
                or 'browser_setup_discovery_rejected',
                diagnostics=getattr(setup, 'diagnostics', None), connection_method=connection_method)
        try:
            view = wait_for_setup(controller)
        except TimeoutError:
            setup = controller.current_browser_setup()
            raise HeadlessBrowserSetupError(
                'discovery', 'browser_setup_timeout', diagnostics=getattr(setup, 'diagnostics', None),
                connection_method=connection_method) from None
        setup = controller.current_browser_setup()
        error = _headless_setup_error(setup, view['targets'])
        if error and not (open_target_if_missing and error == 'agent_browser_no_targets'
                          and not view['targets']):
            raise HeadlessBrowserSetupError('discovery', error, diagnostics=setup.diagnostics,
                                            connection_method=connection_method)
        selection = select_discovered_target(
            controller, view['scope'], view['targets'], target_id=target_id, url_prefix=url_prefix)
        if open_target_if_missing and selection['match_count'] == 0:
            try:
                target_url = _open_target_url(url_prefix)
            except ValueError as exc:
                raise HeadlessBrowserSetupError('target_select', str(exc), diagnostics=setup.diagnostics,
                                                connection_method=connection_method) from None
            if not controller.open_browser_target(view['scope'], target_url):
                raise HeadlessBrowserSetupError('target_select', 'browser_setup_target_open_rejected',
                                                diagnostics=setup.diagnostics,
                                                connection_method=connection_method)
            try:
                view = wait_for_setup(controller)
            except TimeoutError:
                setup = controller.current_browser_setup()
                raise HeadlessBrowserSetupError(
                    'target_select', 'browser_setup_timeout', diagnostics=getattr(setup, 'diagnostics', None),
                    connection_method=connection_method) from None
            setup = controller.current_browser_setup()
            error = _headless_setup_error(setup, view['targets'])
            if error == 'agent_browser_no_targets' and not view['targets']:
                error = None
            if error:
                raise HeadlessBrowserSetupError('target_select', error, diagnostics=setup.diagnostics,
                                                connection_method=connection_method)
            selection = select_discovered_target(
                controller, view['scope'], view['targets'], target_id=target_id, url_prefix=url_prefix)
        if not selection['selected']:
            print(json.dumps({'status': 'target_match_count', 'match_count': selection['match_count'],
                              'total_target_count': selection['total_target_count']}, ensure_ascii=False), flush=True)
            code = ('browser_setup_target_not_found' if selection['match_count'] == 0
                    else 'browser_setup_target_ambiguous')
            raise HeadlessBrowserSetupError('target_select', code, diagnostics=setup.diagnostics,
                                            connection_method=connection_method)
        try:
            view = wait_for_setup(controller)
        except TimeoutError:
            setup = controller.current_browser_setup()
            raise HeadlessBrowserSetupError(
                'selection', 'browser_setup_timeout', diagnostics=getattr(setup, 'diagnostics', None),
                connection_method=connection_method) from None
        setup = controller.current_browser_setup()
        error = _headless_setup_error(setup, view['targets'])
        if error:
            raise HeadlessBrowserSetupError('selection', error, diagnostics=setup.diagnostics,
                                            connection_method=connection_method)
        chosen = next((row for row in view['targets'] if row['tab_id'] == selection['tab_id']), None)
        if (chosen is None or chosen.get('selected') is not True
                or setup.diagnostics.get('connected') is not True
                or setup.diagnostics.get('selected') is not True):
            raise HeadlessBrowserSetupError('selection', 'browser_setup_selection_unverified',
                                            diagnostics=setup.diagnostics, connection_method=connection_method)
        print(json.dumps({'status': 'edge_target_selected', 'match_count': 1,
                          'total_target_count': selection['total_target_count'],
                          'connection_method': connection_method}, ensure_ascii=False), flush=True)
        last_recovery_diagnostic = None
        while True:
            controller.poll()
            client = controller.client
            event_reader = getattr(client, 'drain_browser_recovery_events', None)
            if callable(event_reader):
                for event in event_reader():
                    record = _safe_reconnect_transition(event)
                    if record is not None:
                        print(json.dumps({'status': 'browser_reconnect_transition', **record}, ensure_ascii=False,
                                         separators=(',', ':')), flush=True)
            diagnostic_reader = getattr(client, 'browser_recovery_diagnostics', None)
            diagnostic = _safe_reconnect_record(diagnostic_reader()) if callable(diagnostic_reader) else None
            if diagnostic is not None and diagnostic != last_recovery_diagnostic:
                print(json.dumps({'status': 'browser_reconnect', **diagnostic}, ensure_ascii=False,
                                 separators=(',', ':')), flush=True)
                last_recovery_diagnostic = diagnostic
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        deadline = time.monotonic() + stop_wait
        while not controller.can_close and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.05)


def build_argument_parser():
    parser = argparse.ArgumentParser(description='AoiTalk portable Windows PC Bridge')
    parser.add_argument('--config', type=Path, default=data_dir() / 'connection.json')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--broker-endpoint', metavar='PLAYWRIGHT_WS')
    parser.add_argument('--connect-edge-cdp', metavar='LOOPBACK_ORIGIN', help=argparse.SUPPRESS)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--target-id')
    target.add_argument('--target-tab-url-prefix')
    parser.add_argument('--open-target-if-missing', action='store_true')
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
    return parser


def validate_setup_arguments(parser, args):
    selector_requested = args.target_id is not None or args.target_tab_url_prefix is not None
    if args.connect_edge_cdp is not None:
        parser.error('direct_edge_cdp_forbidden')
    if args.open_target_if_missing and args.target_tab_url_prefix is None:
        parser.error('--open-target-if-missing requires --target-tab-url-prefix')
    if (args.broker_endpoint is not None) != selector_requested:
        parser.error('--broker-endpoint and exactly one target selector must be supplied together')
    if selector_requested and not args.headless:
        parser.error('headless Edge setup requires --headless')


def main():
    # Edge passes its extension origin as argv[1]. It must receive protocol
    # bytes on stdout, never a GUI/log line. This executable serves both roles.
    if '--native-host' in sys.argv or any(a.startswith('chrome-extension://') for a in sys.argv[1:]):
        from .native_host import main as native_main
        native_main()
        return
    parser = build_argument_parser()
    args = parser.parse_args()
    validate_setup_arguments(parser, args)
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
        print(json.dumps({'verified': False, 'error': 'agent_browser_helper_disabled'}), flush=True)
        raise SystemExit(1)
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
    elif args.broker_endpoint is not None:
        try:
            run_headless_edge_setup(args.config, args.broker_endpoint,
                                    target_id=args.target_id,
                                    url_prefix=args.target_tab_url_prefix,
                                    open_target_if_missing=args.open_target_if_missing)
        except Exception as exc:
            print(json.dumps(headless_failure_record(exc), ensure_ascii=False),
                  file=sys.stderr, flush=True)
            raise SystemExit(1) from None
    elif args.headless:
        try:
            value = load_config(args.config)
            client = BridgeClient(value, lambda status: print(status.encode('ascii', 'backslashreplace').decode(), flush=True))
        except Exception as exc:
            print(json.dumps(headless_failure_record(exc), ensure_ascii=False), file=sys.stderr, flush=True)
            raise SystemExit(1) from None
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
