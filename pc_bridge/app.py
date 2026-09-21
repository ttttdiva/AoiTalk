"""Portable GUI/CLI. Closing the window stops remote control."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import sys
import threading

from . import VERSION
from .client import BridgeClient, websocket_url
from .paths import data_dir


def gui(config_path):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    config_error = ''
    try:
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError()
    except (OSError, ValueError):
        config = {}
        config_error = '保存済み設定を読み込めません。設定を再入力または読込してください。'
    root = tk.Tk()
    root.title('AoiTalk PC Bridge')
    root.geometry('650x460')
    root.minsize(600, 420)
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text='AoiTalk PC Bridge', font=('Yu Gothic UI', 18, 'bold')).grid(row=0, column=0, sticky='w')
    ttk.Label(frame, text='このPCのEdge・Windows操作をAoiTalkに接続します。').grid(row=1, column=0, sticky='w', pady=(2, 14))
    server = tk.StringVar(value=config.get('server_url', ''))
    token = tk.StringVar(value=config.get('token', ''))
    ca = tk.StringVar(value=config.get('ca_file', ''))
    for row, title, variable, mask in [(2, 'AoiTalkサーバーURL（インターネット経由はHTTPS）', server, ''),
                                        (4, '接続コード（AoiTalk設定 → PC接続で発行）', token, '•'),
                                        (6, '独自CA証明書ファイル（通常は空欄）', ca, '')]:
        ttk.Label(frame, text=title).grid(row=row, column=0, sticky='w')
        ttk.Entry(frame, textvariable=variable, show=mask).grid(row=row + 1, column=0, sticky='ew', pady=(2, 9))
    status = tk.StringVar(value=config_error or '未接続')
    ttk.Label(frame, textvariable=status, wraplength=570).grid(row=10, column=0, sticky='w', pady=10)
    events = queue.Queue()
    client = None
    thread = None

    def stop():
        nonlocal client
        if client:
            threading.Thread(target=client.stop, daemon=True).start()
        status.set('切断中…')

    def start():
        nonlocal client, thread
        if thread and thread.is_alive():
            status.set('すでに接続中です。変更する場合は切断してください。')
            return
        try:
            websocket_url(server.get())
            if not token.get().strip():
                raise ValueError('接続コードを入力してください')
            value = {'server_url': server.get().strip(), 'token': token.get().strip(), 'ca_file': ca.get().strip()}
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            client = BridgeClient(value, events.put)
            thread = threading.Thread(target=client.run, daemon=True)
            thread.start()
        except Exception as exc:
            status.set(str(exc))

    def import_config():
        path = filedialog.askopenfilename(filetypes=[('Bridge設定', '*.json')])
        if path:
            try:
                value = json.loads(Path(path).read_text(encoding='utf-8'))
                server.set(value['server_url'])
                token.set(value['token'])
                ca.set(value.get('ca_file', ''))
            except Exception:
                status.set('設定ファイルを読み込めませんでした')

    def install():
        try:
            from .install import install_edge
            result = install_edge()
            root.clipboard_clear()
            root.clipboard_append(result['extension_folder'])
            os.startfile(result['extension_folder'])
            messagebox.showinfo('Edge拡張', 'ホストを登録しました。\nEdgeの edge://extensions で「展開して読み込み」から次のフォルダを選びます。\n\n' + result['extension_folder'] + '\n\nフォルダのパスをコピーしました。既存のAoiTalk Browser拡張がある場合は再読み込みしてください。')
        except Exception as exc:
            status.set('Edge登録失敗: ' + str(exc))

    buttons = ttk.Frame(frame)
    buttons.grid(row=8, column=0, sticky='w', pady=(4, 5))
    ttk.Button(buttons, text='接続', command=start).pack(side='left', padx=(0, 8))
    ttk.Button(buttons, text='切断', command=stop).pack(side='left', padx=(0, 8))
    ttk.Button(buttons, text='設定ファイル読込', command=import_config).pack(side='left')
    ttk.Button(frame, text='Edge拡張を準備', command=install).grid(row=9, column=0, sticky='w', pady=4)
    ttk.Label(frame, text='接続中は、このPCをAoiTalkから操作できます。\n閉じると切断します。待受ポート・常駐サービスの登録は不要です。', wraplength=570).grid(row=11, column=0, sticky='w')

    def poll():
        try:
            while True:
                status.set(events.get_nowait())
        except queue.Empty:
            pass
        root.after(150, poll)

    def close():
        stop()
        root.destroy()

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
    from .desktop import enable_dpi
    enable_dpi()
    parser = argparse.ArgumentParser(description='AoiTalk portable Windows PC Bridge')
    parser.add_argument('--config', type=Path, default=data_dir() / 'connection.json')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--install-edge', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args()
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
