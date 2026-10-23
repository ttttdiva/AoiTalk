"""Shared entry-point initialization for the standalone AoiTalk MCP process."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from threading import Lock
from .paths import PACKAGE_ROOT, REPOSITORY_ROOT


LOG_ROOT = REPOSITORY_ROOT / "logs" / "aoitalk_plugin"
_INITIALIZE_LOCK = Lock()
_INITIALIZED = False

_SECRET_ENV_NAMES = (
    "CONTROL_PLANE_API_KEY",
    "OPENAI_ADMIN_KEY",
    "OPENAI_ADMIN_API_KEY",
    "OPENAI_API_KEY",
    "HOME_MCP_AOITALK_RUNTIME_API_KEY",
    "AOITALK_PLUGIN_RUNTIME_API_KEY",
    "TUNNEL_RUNTIME_API_KEY",
)


def prepare_runtime() -> None:
    """Reject scoped gateway children, scrub secrets, and route diagnostics."""
    global _INITIALIZED
    with _INITIALIZE_LOCK:
        if _INITIALIZED:
            return
        if os.environ.get("HOME_MCP_PROFILE"):
            raise RuntimeError("HOME_MCP_PROFILE cannot be used with the AoiTalk plugin entry")

        for name in _SECRET_ENV_NAMES:
            os.environ.pop(name, None)

        log_root = Path(os.environ.get("AOITALK_PLUGIN_LOG_ROOT", str(LOG_ROOT))).expanduser()
        tool_logs = log_root / "tool_logs"
        tool_logs.mkdir(parents=True, exist_ok=True)
        log_root.mkdir(parents=True, exist_ok=True)
        os.environ["HOME_MCP_TOOL_LOG_DIR"] = str(tool_logs)

        # MCP stdio owns stdout. Send diagnostics to stderr and the plugin log.
        if not logging.getLogger().handlers:
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            stderr = logging.StreamHandler(sys.stderr)
            stderr.setFormatter(formatter)
            logfile = logging.FileHandler(log_root / "runtime.log", encoding="utf-8")
            logfile.setFormatter(formatter)
            logging.basicConfig(level=logging.INFO, handlers=[stderr, logfile])
        _INITIALIZED = True
