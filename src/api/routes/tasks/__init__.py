"""Task ルーターの分割サブモジュール。

各 `register_*` 関数は `create_task_router`（src/api/task_routes.py）から
共有コンテキストを受け取り、担当エンドポイントを APIRouter へ登録する。
"""

from .attachments_routes import register_attachment_routes
from .google_calendar_routes import register_google_calendar_routes
from .notifications_routes import register_notification_routes
from .occurrences_time_routes import register_occurrence_time_routes
from .spaces_routes import register_space_routes
from .tags_reorder_routes import register_tag_reorder_routes
from .tasks_routes import register_task_routes

__all__ = [
    "register_attachment_routes",
    "register_google_calendar_routes",
    "register_notification_routes",
    "register_occurrence_time_routes",
    "register_space_routes",
    "register_tag_reorder_routes",
    "register_task_routes",
]
