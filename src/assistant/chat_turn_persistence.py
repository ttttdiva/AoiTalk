"""Persistence helpers for one web chat turn.

The web chat session database is the source of truth. LLM clients may keep
short in-memory history for prompt construction, but web turns should be
saved and restored through this helper so provider-specific clients cannot
silently drop a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..memory.manager import ConversationMemoryManager
from ..memory.models import ConversationMessage

PromptMessage = dict[str, str]


@dataclass
class PersistedTurnMessages:
    user_message: Optional[ConversationMessage] = None
    assistant_message: Optional[ConversationMessage] = None


@dataclass
class ClientUserContextSnapshot:
    had_session_user_id: bool
    session_user_id: Any = None
    had_session_metadata: bool = False
    session_metadata: Any = None
    had_system_prompt: bool = False
    system_prompt: Any = None


def apply_turn_user_context_to_client(
    llm_client: Any,
    *,
    sender_user_id: Optional[str] = None,
    sender_display_name: Optional[str] = None,
) -> Optional[ClientUserContextSnapshot]:
    """Apply the Web turn sender as the LLM memory user for one generation."""

    user_id = str(sender_user_id or "").strip()
    if not user_id or not hasattr(llm_client, "set_session_context"):
        return None

    snapshot = ClientUserContextSnapshot(
        had_session_user_id=hasattr(llm_client, "session_user_id"),
        session_user_id=getattr(llm_client, "session_user_id", None),
        had_session_metadata=hasattr(llm_client, "session_metadata"),
        session_metadata=dict(getattr(llm_client, "session_metadata", {}) or {}),
        had_system_prompt=hasattr(llm_client, "system_prompt"),
        system_prompt=getattr(llm_client, "system_prompt", None),
    )
    metadata = {"platform": "web"}
    display_name = str(sender_display_name or "").strip()
    if display_name:
        metadata["username"] = display_name
    llm_client.set_session_context(user_id=user_id, metadata=metadata)
    return snapshot


def restore_turn_user_context_on_client(
    llm_client: Any,
    snapshot: Optional[ClientUserContextSnapshot],
) -> None:
    if snapshot is None:
        return

    if snapshot.had_session_user_id:
        llm_client.session_user_id = snapshot.session_user_id
    elif hasattr(llm_client, "session_user_id"):
        delattr(llm_client, "session_user_id")

    if snapshot.had_session_metadata:
        llm_client.session_metadata = snapshot.session_metadata
    elif hasattr(llm_client, "session_metadata"):
        delattr(llm_client, "session_metadata")

    if snapshot.had_system_prompt:
        llm_client.system_prompt = snapshot.system_prompt
    elif hasattr(llm_client, "system_prompt"):
        delattr(llm_client, "system_prompt")


class ChatTurnPersistence:
    """Centralizes DB-backed save/restore for web chat turns."""

    def __init__(self, memory_manager: Optional[ConversationMemoryManager] = None):
        self.memory_manager = memory_manager or ConversationMemoryManager()

    async def _ensure_ready(self) -> bool:
        if self.memory_manager.is_initialized():
            return True
        return await self.memory_manager.initialize()

    async def save_user_message(
        self,
        *,
        session_id: Optional[str],
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        branch_from_message_id: Optional[str] = None,
        sender_type: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
    ) -> Optional[ConversationMessage]:
        if not session_id or (not content and not metadata):
            return None
        if not await self._ensure_ready():
            return None
        message = await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="user",
            content=content,
            metadata=metadata or {},
            branch_from_message_id=branch_from_message_id,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
        )
        if message is not None:
            try:
                from ..services.project_qa_candidate_service import (
                    queue_project_qa_candidate_extraction,
                )

                queue_project_qa_candidate_extraction(message.id)
            except Exception:
                pass
        return message

    async def save_assistant_message(
        self,
        *,
        session_id: Optional[str],
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        sender_type: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
    ) -> Optional[ConversationMessage]:
        if not session_id or not content:
            return None
        if not await self._ensure_ready():
            return None
        return await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="assistant",
            content=content,
            metadata=metadata or {},
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
        )

    async def load_prompt_history(
        self,
        *,
        session_id: Optional[str],
        exclude_message_id: Optional[str] = None,
        max_messages: int = 100,
    ) -> list[PromptMessage]:
        if not session_id:
            return []
        if not await self._ensure_ready():
            return []

        repo = self.memory_manager.repository
        if hasattr(repo, "get_active_branch_messages"):
            messages = await repo.get_active_branch_messages(session_id)
        else:
            messages = await repo.get_session_messages(session_id)

        excluded = str(exclude_message_id) if exclude_message_id else None
        prompt_messages: list[PromptMessage] = []
        for msg in messages:
            if excluded and str(msg.id) == excluded:
                break
            if msg.role not in {"system", "user", "assistant"}:
                continue
            prompt_messages.append({"role": msg.role, "content": msg.content})

        return prompt_messages[-max_messages:]

    def apply_prompt_history_to_client(
        self,
        llm_client: Any,
        *,
        session_id: Optional[str],
        prompt_history: list[PromptMessage],
    ) -> None:
        """Prime provider-local history from persisted active-branch messages."""
        if hasattr(llm_client, "history_manager"):
            history_manager = llm_client.history_manager
            if hasattr(history_manager, "clear"):
                history_manager.clear()
            for msg in prompt_history:
                if hasattr(history_manager, "add_message"):
                    history_manager.add_message(msg["role"], msg["content"])

        if hasattr(llm_client, "conversation_history"):
            llm_client.conversation_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in prompt_history
                if msg["role"] in {"user", "assistant"}
            ]

        if hasattr(llm_client, "_loaded_history_session_id"):
            llm_client._loaded_history_session_id = session_id
        if hasattr(llm_client, "_loaded_session_id"):
            llm_client._loaded_session_id = session_id
        if hasattr(llm_client, "_history_session_id"):
            llm_client._history_session_id = session_id
        if hasattr(llm_client, "_context_window_override_tokens"):
            llm_client._context_window_override_tokens = None
