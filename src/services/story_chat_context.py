"""Story Studio の writing chat 文脈。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, TypeVar
from uuid import UUID

from ..memory.database import get_db_session
from ..memory.models.story import StoryWritingSession

try:
    from ..tools.writing_tools import get_story_context
except ModuleNotFoundError as exc:
    # Enterprise removes the optional writing tool module.  Normal clients
    # still import this helper, so absence means no prompt Story context.
    if exc.name not in {"src.tools.writing_tools", "tools.writing_tools"}:
        raise
    get_story_context = None
from sqlalchemy import select


STORY_WRITING_TOOLS = frozenset({"agent_team_delegate"})

_T = TypeVar("_T")


class StoryChatContextBuildError(RuntimeError):
    """StoryWritingSession がある会話で執筆文脈を構築できなかった。"""


@dataclass(frozen=True)
class StoryChatContext:
    mode: str
    prompt: str
    agent_name: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class StoryChatContextResolution:
    """執筆文脈の解決結果。writing session 有無と失敗理由を分離する。"""

    context: Optional[StoryChatContext]
    has_writing_session: bool
    error: Optional[str] = None


def is_story_workflow_tool_allowed(
    tool_name: str,
    context: StoryChatContext,
) -> bool:
    return tool_name in context.allowed_tools


def story_context_activation_tag(context: StoryChatContext | None) -> set[str]:
    """Return the generic activation tag for an existing Story context."""

    return {"story"} if context is not None else set()


async def _has_writing_session(conversation_id: str) -> bool:
    try:
        conv_id = UUID(str(conversation_id))
    except (TypeError, ValueError):
        return False
    async with await get_db_session() as session:
        writing = await session.scalar(
            select(StoryWritingSession).where(
                StoryWritingSession.conversation_session_id == conv_id
            )
        )
        return writing is not None


async def _build_story_chat_context_unchecked(
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    if not conversation_id or get_story_context is None:
        return None
    try:
        conv_id = UUID(str(conversation_id))
    except (TypeError, ValueError):
        return None
    async with await get_db_session() as session:
        writing = await session.scalar(
            select(StoryWritingSession).where(
                StoryWritingSession.conversation_session_id == conv_id
            )
        )
        if writing is None:
            return None
    prompt = await get_story_context.function(conversation_id=str(conv_id))
    return StoryChatContext(
        mode="writing",
        prompt=str(prompt),
        agent_name="StoryWriter",
        allowed_tools=STORY_WRITING_TOOLS,
    )


async def build_story_chat_context(
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    """Resolve Story context with the historical compatibility fail-open API."""

    try:
        return await _build_story_chat_context_unchecked(conversation_id)
    except Exception:
        return None


async def build_story_chat_context_strict(
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    """Resolve trusted Story context without hiding DB/tool failures."""

    return await _build_story_chat_context_unchecked(conversation_id)


async def resolve_story_chat_context_for_chat(
    conversation_id: Optional[str],
) -> StoryChatContextResolution:
    """チャット用の執筆文脈解決。writing session ありは fail-closed。"""
    if not conversation_id:
        return StoryChatContextResolution(None, False)

    has_session = await _has_writing_session(conversation_id)
    if not has_session:
        try:
            context = await _build_story_chat_context_unchecked(conversation_id)
        except Exception:
            return StoryChatContextResolution(None, False)
        return StoryChatContextResolution(context, False)

    try:
        context = await build_story_chat_context_strict(conversation_id)
    except Exception as exc:
        message = str(exc).strip() or "Story執筆文脈の構築に失敗しました"
        return StoryChatContextResolution(None, True, message)

    if context is None:
        return StoryChatContextResolution(
            None,
            True,
            "Story執筆文脈を構築できませんでした",
        )
    return StoryChatContextResolution(context, True)


def run_story_chat_context_sync(
    run_sync: Callable[[object], _T],
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    """LLM engine 向け同期エントリ。writing session ありの失敗は例外化する。"""
    resolution = run_sync(resolve_story_chat_context_for_chat(conversation_id))
    if resolution.has_writing_session and resolution.error:
        raise StoryChatContextBuildError(resolution.error)
    return resolution.context


async def _resolve_story_workflow_context_unchecked(
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    """Resolve only Story activation metadata (never build the prompt)."""

    if not conversation_id:
        return None
    try:
        conv_id = UUID(str(conversation_id))
    except (TypeError, ValueError):
        return None
    async with await get_db_session() as session:
        writing = await session.scalar(
            select(StoryWritingSession).where(
                StoryWritingSession.conversation_session_id == conv_id
            )
        )
        if writing is None:
            return None
    return StoryChatContext(
        mode="writing",
        prompt="",
        agent_name="StoryWriter",
        allowed_tools=STORY_WRITING_TOOLS,
    )


async def resolve_story_workflow_context_strict(
    conversation_id: Optional[str],
) -> Optional[StoryChatContext]:
    """Strict, lightweight Story activation resolver for tool exposure.

    This checks only durable StoryWritingSession existence and the canonical
    allow-list.  It deliberately does not execute ``get_story_context`` so
    schema exposure never rebuilds a large prompt.
    """

    return await _resolve_story_workflow_context_unchecked(conversation_id)


__all__ = [
    "STORY_WRITING_TOOLS",
    "StoryChatContext",
    "StoryChatContextBuildError",
    "StoryChatContextResolution",
    "build_story_chat_context",
    "build_story_chat_context_strict",
    "resolve_story_chat_context_for_chat",
    "resolve_story_workflow_context_strict",
    "run_story_chat_context_sync",
    "is_story_workflow_tool_allowed",
    "story_context_activation_tag",
]
