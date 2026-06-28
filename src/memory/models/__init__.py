"""Database models for conversation memory management

ドメイン別モジュールに分割したモデルパッケージ。公開APIは従来どおり
``from src.memory.models import <Model>`` で全モデル名を提供する。
"""

from sqlalchemy.orm import configure_mappers

from .base import Base
from .conversations import (
    ConversationSession,
    ConversationParticipant,
    ConversationMessage,
    ConversationArchive,
    ConversationHistory,
)
from .agent_runs import (
    AgentRun,
    AgentRunEdge,
    AgentRunEvent,
    AgentRunToolCall,
)
from .users import (
    User,
    LongLivedApiToken,
    WebUILoginLog,
    AppConfigSetting,
    FileExplorerBookmark,
    GoogleCalendarConnection,
    Feedback,
)
from .spotify import (
    SpotifyActivityLog,
    SpotifySessionSummary,
)
from .projects import (
    Space,
    Project,
    ProjectContextPack,
    ContextMemory,
    ProjectInfoCategory,
    ProjectDocument,
    ProjectFact,
    ProjectInfoSyncState,
    ProjectMember,
    ProjectJoinRequest,
)
from .records import (
    RecordTable,
    RecordField,
    RecordRow,
    RecordView,
    RecordAttachment,
    RecordEvent,
)
from .tasks import (
    LocalTask,
    TaskEvent,
    TaskExecutionSession,
    Task,
    TaskAssignee,
    TaskComment,
    TaskAttachment,
    TaskActivity,
    TaskDependency,
    TaskRecurrenceRule,
    TaskOccurrence,
    TimeEntry,
    Tag,
    TaskTag,
)
from .notifications import (
    ProjectNotificationSetting,
    NotificationDelivery,
)
from .knowledge import (
    KnowledgeSource,
    KnowledgeSourcePermission,
    KnowledgeDocument,
    KnowledgeChunk,
    KnowledgeLink,
    KnowledgeAnnotation,
    KnowledgeEditEvent,
)
from .remote import (
    RemoteServerProfile,
)

configure_mappers()

__all__ = [
    "AgentRun",
    "AgentRunEdge",
    "AgentRunEvent",
    "AgentRunToolCall",
    "AppConfigSetting",
    "Base",
    "ContextMemory",
    "ConversationArchive",
    "ConversationHistory",
    "ConversationMessage",
    "ConversationParticipant",
    "ConversationSession",
    "Feedback",
    "FileExplorerBookmark",
    "GoogleCalendarConnection",
    "KnowledgeAnnotation",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeEditEvent",
    "KnowledgeLink",
    "KnowledgeSource",
    "KnowledgeSourcePermission",
    "LocalTask",
    "LongLivedApiToken",
    "NotificationDelivery",
    "Project",
    "ProjectContextPack",
    "ProjectDocument",
    "ProjectFact",
    "ProjectInfoCategory",
    "ProjectInfoSyncState",
    "ProjectJoinRequest",
    "ProjectMember",
    "ProjectNotificationSetting",
    "RecordAttachment",
    "RecordEvent",
    "RecordField",
    "RecordRow",
    "RecordTable",
    "RecordView",
    "RemoteServerProfile",
    "Space",
    "SpotifyActivityLog",
    "SpotifySessionSummary",
    "Tag",
    "Task",
    "TaskActivity",
    "TaskAssignee",
    "TaskAttachment",
    "TaskComment",
    "TaskDependency",
    "TaskEvent",
    "TaskExecutionSession",
    "TaskOccurrence",
    "TaskRecurrenceRule",
    "TaskTag",
    "TimeEntry",
    "User",
    "WebUILoginLog",
]
