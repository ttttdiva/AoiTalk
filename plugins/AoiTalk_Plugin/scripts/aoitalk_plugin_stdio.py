"""Canonical stdio entry point used by tunnel-client and local smoke checks."""
from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
os.environ.setdefault("AOITALK_PLUGIN_ROOT", str(PACKAGE_ROOT))
os.environ.setdefault("AOITALK_REPOSITORY_ROOT", str(REPOSITORY_ROOT))

from aoitalk_plugin.runtime import prepare_runtime


def main() -> None:
    prepare_runtime()
    from aoitalk_plugin.aoitalk_plugin_server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
