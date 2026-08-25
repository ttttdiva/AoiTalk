"""Discord bot logging setup."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


_LOG_PATH: Optional[Path] = None


def setup_discord_logging(project_root: Path | None = None) -> Path:
    """Attach a UTF-8 file handler for Discord bot and VC diagnostics."""
    global _LOG_PATH

    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]

    from src.utils.log_layout import get_log_layout
    from src.utils.log_housekeeping import run_log_housekeeping

    layout = get_log_layout(project_root)
    layout.ensure_dirs()
    run_log_housekeeping(layout)

    log_dir = layout.discord_dir

    if _LOG_PATH is None or _LOG_PATH.parent != log_dir:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _LOG_PATH = log_dir / f"bot_{timestamp}.log"

    handler_name = "aoitalk_discord_file"
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if getattr(handler, "name", None) == handler_name:
            return _LOG_PATH

    file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    file_handler.name = handler_name
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        )
    )

    root_logger.addHandler(file_handler)
    root_logger.setLevel(min(root_logger.level or logging.INFO, logging.INFO))

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("src.bot").setLevel(logging.INFO)

    latest_path = log_dir / "latest.log"
    try:
        latest_path.write_text(str(_LOG_PATH), encoding="utf-8")
    except Exception:
        pass

    return _LOG_PATH
