"""WSL2 上で service_manager.start_services() → kill_services() を実地検証するスクリプト。

直接 main.py を起動すると heavy deps 読み込みに時間がかかるため、
service_manager だけを import して子プロセス生成と後片付けを確認する。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src import service_manager  # noqa: E402


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_listening(port):
            return True
        time.sleep(1)
    return False


def main() -> int:
    print("=== start_services() 実行 ===", flush=True)
    service_manager.start_services()

    frontend_proc, caddy_proc = service_manager._child_processes
    print(f"frontend pid={frontend_proc.pid} caddy pid={caddy_proc.pid}", flush=True)

    print("=== port 3002 (frontend) 待機 ===", flush=True)
    fe_ok = _wait_port(3002, 60)
    print(f"frontend listening: {fe_ok}", flush=True)

    print("=== port 6002 (caddy) 待機 ===", flush=True)
    cd_ok = _wait_port(6002, 30)
    print(f"caddy listening: {cd_ok}", flush=True)

    # pgid 確認（Linux のみ）
    try:
        fe_pgid = os.getpgid(frontend_proc.pid)
        cd_pgid = os.getpgid(caddy_proc.pid)
        print(f"frontend pgid={fe_pgid} caddy pgid={cd_pgid}", flush=True)
    except Exception as exc:  # pragma: no cover - Windows 実行時は通らない
        print(f"getpgid skipped: {exc}", flush=True)

    # frontend.log の先頭数行を覗き見（Linux 側 tee 動作確認）
    log_path = service_manager._service_log_dir(PROJECT_ROOT) / "frontend.log"
    if log_path.exists():
        print("=== frontend.log 先頭 10 行 ===", flush=True)
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                print(line.rstrip(), flush=True)

    print("=== kill_services() 実行 ===", flush=True)
    service_manager.kill_services()

    time.sleep(2)
    print(f"frontend.poll={frontend_proc.poll()} caddy.poll={caddy_proc.poll()}", flush=True)

    # kill_services() 後、ポートが閉じたか
    fe_after = _port_listening(3002)
    cd_after = _port_listening(6002)
    print(f"after kill: port3002_listening={fe_after} port6002_listening={cd_after}", flush=True)

    ok = fe_ok and cd_ok and not fe_after and not cd_after
    print(f"=== RESULT: {'OK' if ok else 'NG'} ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
