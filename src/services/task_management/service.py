"""TaskManagementService composed from behavior-preserving mixins."""

from __future__ import annotations

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

    def __init__(self, broadcaster=None):
        self._broadcaster = broadcaster
