"""TaskManagementService composed from behavior-preserving mixins."""

from __future__ import annotations

from typing import Any

from .helpers import HelperMixin
from .notifications import NotificationMixin
from .occurrences import OccurrenceMixin
from .tasks import TaskCrudMixin
from .time_tracking import TimeTrackingMixin
from .due_completion import DueCompletionMixin


class TaskManagementService(
    HelperMixin,
    TaskCrudMixin,
    OccurrenceMixin,
    TimeTrackingMixin,
    NotificationMixin,
    DueCompletionMixin,
):
    """Stateful service for the task system."""

    def __init__(
        self,
        broadcaster=None,
        *,
        config: Any | None = None,
        privacy_gateway: Any | None = None,
        user_id: Any | None = None,
        session_id: Any | None = None,
    ):
        self._broadcaster = broadcaster
        # NotificationMixin resolves these attributes immediately before an
        # external delivery.  Keep them optional for all existing callers,
        # while workers/API routes can provide the app privacy policy and
        # request scope instead of silently constructing a direct gateway.
        self.config = config
        self._config = config
        self.privacy_gateway = privacy_gateway
        self._privacy_gateway = privacy_gateway
        self.user_id = str(user_id or "")
        self.session_id = str(session_id or "")
