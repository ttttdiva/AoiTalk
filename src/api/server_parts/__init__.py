"""WebChatServer を関心別 Mixin へ分割したパッケージ。

server.py 本体は合成クラス (__init__ / ルート登録オーケストレーション) のみを保持し、
実際の振る舞いは以下の Mixin へ委譲する。ロジックは server.py からの移動のみで一切変更していない。
"""

from .auth_mixin import AuthMixin
from .chat_message_mixin import ChatMessageMixin
from .conversation_mixin import ConversationMixin
from .messaging_mixin import MessagingMixin
from .mobile_commands_mixin import MobileCommandsMixin

__all__ = [
    "AuthMixin",
    "ChatMessageMixin",
    "ConversationMixin",
    "MessagingMixin",
    "MobileCommandsMixin",
]
