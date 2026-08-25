"""Persistence helpers for one web chat turn.

The web chat session database is the source of truth. LLM clients may keep
short in-memory history for prompt construction, but web turns should be
saved and restored through this helper so provider-specific clients cannot
silently drop a session.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from .chat_attachment_utils import (
    add_project_attachment_context_marker,
    build_message_with_attachment_context,
)
from ..llm.conversation_context import (
    compact_model_transcript_for_history,
    merge_model_transcript_snapshot,
)
from ..memory.manager import ConversationMemoryManager
from ..memory.models import ConversationMessage

PromptMessage = dict[str, Any]
_CACHED_GENERATION_METADATA_KEYS = frozenset(
    {
        "cache_usage",
        "context_snapshot",
        "conversation_state",
        "free_team_route",
        "generation_metrics",
        "model_transcript",
        "token_usage",
    }
)
_ATTACHMENT_PATH_PREFIXES = ("_projects/", "_users/")


def _attachment_history_context(
    metadata: Any,
    *,
    project_id: Any = None,
    user_id: Any = None,
    shared_session: bool = False,
) -> str:
    """Return safe path-only references for attachments from a prior turn."""
    if not isinstance(metadata, dict):
        return ""
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return ""

    lines: list[str] = []
    safe_items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in attachments:
        if not isinstance(item, dict) or item.get("upload_failed"):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        normalized_path = raw_path.replace("\\", "/").strip()
        if (
            not normalized_path
            or normalized_path.startswith("/")
            or re.match(r"^[A-Za-z]:/", normalized_path)
            or not normalized_path.casefold().startswith(_ATTACHMENT_PATH_PREFIXES)
        ):
            continue
        parts = normalized_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            continue
        if any(ord(char) < 32 for char in normalized_path):
            continue
        if normalized_path.casefold().startswith("_users/"):
            user_prefix = f"_users/user_{str(user_id or '').strip()}/".casefold()
            if shared_session or not user_id or not normalized_path.casefold().startswith(
                user_prefix
            ):
                continue
        if normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)

        raw_name = str(item.get("name") or parts[-1]).strip()
        safe_name = re.sub(r"[\r\n\x00-\x1f\]]+", " ", raw_name).strip()
        safe_name = safe_name or parts[-1]
        safe_item = {"name": safe_name, "path": normalized_path}
        # Preserve only the upload route's structured registration bit when
        # rebuilding a prompt.  A path line alone is never enough to recreate
        # the Project attachment marker.
        if item.get("registered") is True:
            safe_item["registered"] = True
        safe_items.append(safe_item)
        lines.append(f"[添付ファイル: {safe_name}] {normalized_path}")

    context = "\n".join(lines)
    project_key = str(project_id or "").strip()
    if project_key:
        marked = add_project_attachment_context_marker(
            context,
            safe_items,
            project_key,
        )
        return marked or ""
    return context


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


def reset_turn_generation_metadata(llm_client: Any) -> None:
    """Clear cached output metadata before a new Web turn selects its session."""

    if llm_client is None:
        return
    empty_values = {
        "_last_model_transcript": [],
        "_history_authoritative_model_transcript": [],
        "_history_active_model_transcript": [],
        "_last_context_snapshots": [],
        "_last_turn_tool_records": [],
        "_last_turn_tool_rounds_exhausted": False,
        "_last_usage_records": [],
        "_last_generation_metadata": {},
        "_last_usage": {},
        "_last_generation_metrics": None,
        "_last_cli_usage": {},
        "_last_route_metadata": {},
    }
    for attribute, empty_value in empty_values.items():
        if hasattr(llm_client, attribute):
            try:
                setattr(llm_client, attribute, empty_value)
            except Exception:
                pass


def without_cached_generation_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove fields that can only describe a completed LLM generation."""

    return {
        key: value
        for key, value in metadata.items()
        if key not in _CACHED_GENERATION_METADATA_KEYS
    }


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

    async def resolve_session_character_name(
        self,
        session_id: Optional[str],
    ) -> Optional[str]:
        """Return the canonical character slug stored by a web session.

        Older sessions may contain a display name or an alias (for example
        ``aoi``). Resolve those values through the character service and repair
        the session row so subsequent turns use the same stable identifier.
        """
        if not session_id or not await self._ensure_ready():
            return None

        repo = getattr(self.memory_manager, "repository", None)
        if repo is None or not hasattr(repo, "get_session_by_id"):
            return None
        try:
            session = await repo.get_session_by_id(session_id, with_messages=False)
        except TypeError:
            session = await repo.get_session_by_id(session_id)
        if session is None:
            return None

        raw_name = str(getattr(session, "character_name", "") or "").strip()
        if not raw_name:
            return None

        try:
            from ..services.character_service import get_character_for_prompt

            character = await get_character_for_prompt(raw_name)
        except Exception:
            character = None
        canonical_name = str((character or {}).get("slug") or raw_name).strip()
        if canonical_name != raw_name and hasattr(repo, "update_session"):
            try:
                repaired = await repo.update_session(
                    session_id,
                    character_name=canonical_name,
                    touch_activity=False,
                    expected_character_name=raw_name,
                )
                if not repaired:
                    # Header switching may have won a race while this turn was
                    # resolving a legacy alias. Never let the repair overwrite
                    # the newer selection.
                    try:
                        latest = await repo.get_session_by_id(
                            session_id, with_messages=False
                        )
                    except TypeError:
                        latest = await repo.get_session_by_id(session_id)
                    latest_name = str(
                        getattr(latest, "character_name", "") or ""
                    ).strip()
                    if latest_name:
                        return latest_name
            except Exception:
                # A read/repair failure must not prevent the current turn.
                pass
        return canonical_name

    async def load_session_context(
        self,
        session_id: Optional[str],
    ) -> dict[str, Any]:
        """会話セッションの非表示ランタイム状態を返す。"""
        if not session_id or not await self._ensure_ready():
            return {}
        repo = self.memory_manager.repository
        try:
            session = await repo.get_session_by_id(session_id, with_messages=False)
        except TypeError:
            session = await repo.get_session_by_id(session_id)
        context = getattr(session, "context", None) if session is not None else None
        return dict(context) if isinstance(context, dict) else {}

    async def update_session_context(
        self,
        session_id: Optional[str],
        updates: dict[str, Any],
    ) -> bool:
        """既存 context を保持したままランタイム状態をマージする。"""
        if not session_id or not updates or not await self._ensure_ready():
            return False
        updater = getattr(self.memory_manager.repository, "update_session_context", None)
        if not callable(updater):
            return False
        return bool(await updater(session_id, dict(updates)))

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
        message_id: Optional[str] = None,
    ) -> Optional[ConversationMessage]:
        if not session_id or (not content and not metadata):
            return None
        if not await self._ensure_ready():
            return None
        message_identity = {"message_id": message_id} if message_id else {}
        message = await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="user",
            content=content,
            metadata=metadata or {},
            branch_from_message_id=branch_from_message_id,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
            **message_identity,
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

    async def load_message(
        self, message_id: str
    ) -> Optional[ConversationMessage]:
        if not message_id or not await self._ensure_ready():
            return None
        return await self.memory_manager.repository.get_message_by_id(message_id)

    async def delete_message(
        self, *, session_id: Optional[str], message_id: Optional[str]
    ) -> bool:
        """Roll back a newly persisted message that was never accepted."""
        if not session_id or not message_id or not await self._ensure_ready():
            return False
        deleter = getattr(self.memory_manager.repository, "delete_message_by_id", None)
        if not callable(deleter):
            return False
        return bool(await deleter(session_id, message_id))

    async def update_message_metadata(
        self,
        *,
        session_id: Optional[str],
        message_id: Optional[str],
        updates: dict[str, Any],
    ) -> bool:
        if not session_id or not message_id or not await self._ensure_ready():
            return False
        updater = getattr(self.memory_manager.repository, "update_message_metadata", None)
        if not callable(updater):
            return False
        return bool(await updater(session_id, message_id, updates))

    async def save_assistant_message(
        self,
        *,
        session_id: Optional[str],
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        sender_type: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[ConversationMessage]:
        if not session_id or not content:
            return None
        if not await self._ensure_ready():
            return None
        message_identity = {"message_id": message_id} if message_id else {}
        return await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="assistant",
            content=content,
            metadata=metadata or {},
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
            **message_identity,
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
        persisted_session = None
        if hasattr(repo, "get_session_by_id"):
            try:
                persisted_session = await repo.get_session_by_id(session_id, with_messages=False)
            except TypeError:
                persisted_session = await repo.get_session_by_id(session_id)
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
            metadata = getattr(msg, "message_metadata", None) or getattr(msg, "metadata", None) or {}
            if (
                isinstance(metadata, dict)
                and metadata.get("delivery_mode") == "immediate_interrupt"
                and metadata.get("interrupt_receipt_status") == "pending"
            ):
                # A route may have been cancelled after the pending row was
                # committed but before the active generation accepted it.
                # Keep it visible for reconciliation, but never inject an
                # outcome-unknown instruction into a later prompt.
                continue
            content = msg.content
            if msg.role == "user":
                attachment_context = _attachment_history_context(
                    metadata,
                    project_id=(
                        getattr(persisted_session, "project_id", None)
                        if persisted_session is not None
                        else None
                    ),
                    user_id=(
                        getattr(persisted_session, "user_id", None)
                        if persisted_session is not None
                        else None
                    ),
                    shared_session=bool(
                        getattr(persisted_session, "is_group_chat", False)
                        if persisted_session is not None
                        else False
                    ),
                )
                if attachment_context:
                    content = build_message_with_attachment_context(
                        content,
                        attachment_context,
                    )
            prompt_message: PromptMessage = {"role": msg.role, "content": content}
            model_transcript = metadata.get("model_transcript") if isinstance(metadata, dict) else None
            if msg.role == "assistant" and isinstance(model_transcript, list) and model_transcript:
                prompt_message["_model_transcript"] = model_transcript
            if not prompt_messages and persisted_session is not None:
                summary = str(getattr(persisted_session, "current_summary", "") or "").strip()
                if summary:
                    prompt_message["_summary"] = summary
                session_context = getattr(persisted_session, "context", None) or {}
                provider_state = (
                    session_context.get("llm_provider_state")
                    if isinstance(session_context, dict)
                    else None
                )
                if isinstance(provider_state, dict) and provider_state:
                    prompt_message["_provider_state"] = dict(provider_state)
            prompt_messages.append(prompt_message)

        selected = prompt_messages[-max_messages:]
        if not selected and persisted_session is not None:
            summary = str(getattr(persisted_session, "current_summary", "") or "").strip()
            session_context = getattr(persisted_session, "context", None) or {}
            provider_state = (
                session_context.get("llm_provider_state")
                if isinstance(session_context, dict)
                else None
            )
            if summary or (isinstance(provider_state, dict) and provider_state):
                selected = [{"role": "system", "content": ""}]
                if summary:
                    selected[0]["_summary"] = summary
                if isinstance(provider_state, dict) and provider_state:
                    selected[0]["_provider_state"] = dict(provider_state)
        if persisted_session is not None and selected:
            summary = str(getattr(persisted_session, "current_summary", "") or "").strip()
            if summary and not any(item.get("_summary") for item in selected):
                selected[0]["_summary"] = summary
            session_context = getattr(persisted_session, "context", None) or {}
            provider_state = (
                session_context.get("llm_provider_state")
                if isinstance(session_context, dict)
                else None
            )
            if (
                isinstance(provider_state, dict)
                and provider_state
                and not any(item.get("_provider_state") for item in selected)
            ):
                selected[0]["_provider_state"] = dict(provider_state)
        return selected

    def apply_prompt_history_to_client(
        self,
        llm_client: Any,
        *,
        session_id: Optional[str],
        prompt_history: list[PromptMessage],
    ) -> None:
        """Prime provider-local history from persisted active-branch messages."""
        provider_state = next(
            (
                dict(msg.get("_provider_state") or {})
                for msg in prompt_history
                if isinstance(msg.get("_provider_state"), dict)
            ),
            {},
        )
        if hasattr(llm_client, "_provider_state"):
            llm_client._provider_state = {
                "previous_response_id": provider_state.get("previous_response_id"),
                "fingerprint": provider_state.get("fingerprint"),
            }

        if hasattr(llm_client, "history_manager"):
            history_manager = llm_client.history_manager
            if hasattr(history_manager, "clear"):
                history_manager.clear()
            for msg in prompt_history:
                if hasattr(history_manager, "add_message") and (
                    msg.get("content") or msg.get("role") != "system"
                ):
                    history_manager.add_message(msg["role"], msg["content"])
            summary = next(
                (str(msg.get("_summary") or "") for msg in prompt_history if msg.get("_summary")),
                "",
            )
            if summary and hasattr(history_manager, "set_summary"):
                history_manager.set_summary(summary)

            model_messages: list[dict[str, Any]] = []
            previous_transcript: list[dict[str, Any]] = []
            fallback_start = 0
            for msg in prompt_history:
                transcript = msg.get("_model_transcript")
                if (
                    msg.get("role") == "assistant"
                    and isinstance(transcript, list)
                    and transcript
                ):
                    model_messages, previous_transcript, fallback_start = (
                        merge_model_transcript_snapshot(
                            model_messages,
                            previous_transcript,
                            fallback_start,
                            transcript,
                            allowed_roles={"system", "user", "assistant", "tool"},
                        )
                    )
                elif (
                    msg.get("role") in {"system", "user", "assistant", "tool"}
                    and (msg.get("content") or msg.get("role") != "system")
                ):
                    model_messages.append(
                        {
                            key: msg[key]
                            for key in ("role", "content", "tool_call_id", "tool_calls", "name")
                            if key in msg
                        }
                    )
            if hasattr(history_manager, "set_model_messages"):
                history_manager.set_model_messages(model_messages)
            full_model_transcript = [
                dict(item)
                for item in model_messages
                if isinstance(item, dict)
                and item.get("role") in {"user", "assistant", "tool"}
            ]
            try:
                llm_client._model_transcript = [dict(item) for item in model_messages]
                llm_client._last_model_transcript = [
                    dict(item) for item in previous_transcript
                ]
                llm_client._history_authoritative_model_transcript = full_model_transcript
            except Exception:
                pass

            compacted_model_messages = compact_model_transcript_for_history(
                model_messages,
                getattr(llm_client, "config", None),
            )
            if compacted_model_messages != model_messages:
                if hasattr(history_manager, "set_model_messages"):
                    history_manager.set_model_messages(compacted_model_messages)
                try:
                    llm_client._model_transcript = [
                        dict(item) for item in compacted_model_messages
                    ]
                except Exception:
                    pass
            try:
                llm_client._history_active_model_transcript = [
                    dict(item)
                    for item in compacted_model_messages
                    if isinstance(item, dict)
                    and item.get("role") in {"user", "assistant", "tool"}
                ]
            except Exception:
                pass

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
