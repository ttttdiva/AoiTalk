"""Start with the existing AoiTalk Python environment; no WebUI or DB required."""
import argparse
import logging
import os
import signal
import threading

from .backend import LifecycleBackend
from .launcher import launch_coordinator, uses_windows_venv
from .registry import isolate_runtime_environment
from .windows_job import install_process_ownership
from .server import Coordinator, Server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared local inference lifecycle coordinator")
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--direct-python", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    # Node/console callers may enter through venv\Scripts\python.exe. That
    # redirector creates its own kill-on-close job, even after breakaway. Relaunch
    # once using CPython's base-interpreter contract before owning any workers.
    if uses_windows_venv() and not args.direct_python:
        launch_coordinator()
        return
    isolate_runtime_environment(os.environ)
    ownership = install_process_ownership()
    if ownership is None:
        logging.warning("Broker hard-crash child cleanup is currently guaranteed on Windows only")
    coordinator = Coordinator(LifecycleBackend())
    # Bind before starting any worker. A competing launcher exits without touching models.
    server = Server(("127.0.0.1", 47641), coordinator)
    closing = threading.Event()

    def maintenance():
        while not closing.wait(5):
            coordinator.reap()

    def stop(*_):
        with coordinator.lock:
            if coordinator.tickets or coordinator.pending:
                logging.warning("Coordinator has leases or pending acquisitions; release or cancel their owners before shutdown")
                return
        closing.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=maintenance, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        closing.set()
        server.server_close()
        # Only exact worker objects created by this coordinator may be stopped.
        # A forced coordinator exit leaves unknown listeners fenced on next startup.
        coordinator.backend.core.stop_openai_compatible_local_servers()


if __name__ == "__main__":
    main()
