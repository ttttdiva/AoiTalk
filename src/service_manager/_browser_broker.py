"""Thin service-manager exports for the process-local browser supervisor."""

from src.services.local_browser.broker import (
    managed_browser_broker_status,
    start_managed_browser_broker,
    stop_managed_browser_broker,
)

__all__ = [
    "managed_browser_broker_status",
    "start_managed_browser_broker",
    "stop_managed_browser_broker",
]
