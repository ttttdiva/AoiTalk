"""Compatibility module entry for the canonical standalone stdio launcher."""
from __future__ import annotations

from .runtime import prepare_runtime


def main() -> None:
    prepare_runtime()
    from .aoitalk_plugin_server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
