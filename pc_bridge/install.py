"""Register this executable as Edge's Native Messaging host (current user)."""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from .paths import data_dir, extension_source


def install_edge():
    if os.name != 'nt':
        raise RuntimeError('Edge registration requires Windows')
    import winreg
    data = data_dir()
    extension = data / 'edge-extension'
    extension.mkdir(parents=True, exist_ok=True)
    shutil.copytree(extension_source(), extension, dirs_exist_ok=True)
    manifest = json.loads((extension / 'manifest.json').read_text(encoding='utf-8'))
    import base64
    digest = hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
    extension_id = ''.join(chr(97 + int(c, 16)) for c in digest)
    if getattr(sys, 'frozen', False):
        launcher = str(Path(sys.executable).resolve())
    else:
        # Development only; the shipped executable never needs Python installed.
        from .paths import home
        cmd = data / 'native-host.cmd'
        cmd.write_text('@echo off\n"' + sys.executable + '" -u "' + str(home() / 'scripts/pc_bridge_main.py') + '" --native-host %*\n', encoding='utf-8')
        launcher = str(cmd)
    host = data / 'com.aoitalk.edge.json'
    host.write_text(json.dumps({
        'name': 'com.aoitalk.edge', 'description': 'AoiTalk PC Bridge',
        'path': launcher, 'type': 'stdio',
        'allowed_origins': [f'chrome-extension://{extension_id}/'],
    }, indent=2), encoding='utf-8')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Edge\NativeMessagingHosts\com.aoitalk.edge') as key:
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, str(host))
    return {'extension_id': extension_id, 'extension_folder': str(extension), 'host_manifest': str(host)}
