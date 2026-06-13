import logging

from src.bot.logging import setup_discord_logging


def test_setup_discord_logging_creates_file_handler_once(tmp_path):
    root_logger = logging.getLogger()
    before = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "name", None) == "aoitalk_discord_file"
    ]
    for handler in before:
        root_logger.removeHandler(handler)
        handler.close()

    log_path = setup_discord_logging(tmp_path)
    setup_discord_logging(tmp_path)

    handlers = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "name", None) == "aoitalk_discord_file"
    ]

    try:
        assert len(handlers) == 1
        assert log_path.parent == tmp_path / "logs" / "discord"
        assert (tmp_path / "logs" / "discord" / "latest.log").exists()
    finally:
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()
