"""Conversation title generation for chat sessions."""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

from ..memory.models import ConversationMessage

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 50
TITLE_FALLBACK_LENGTH = 40
TITLE_CONTENT_LIMIT = 500
TITLE_GENERATION_CONTEXT_KEY = "title_generation"

TitleGenerator = Callable[[str], Awaitable[Optional[str]] | Optional[str]]


@dataclass(frozen=True)
class GeneratedConversationTitle:
    title: str
    source: str


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fallback_title_from_first_message(first_message: str) -> str:
    title = _compact_text(first_message)
    if len(title) > TITLE_FALLBACK_LENGTH:
        return title[: TITLE_FALLBACK_LENGTH - 3] + "..."
    return title


def clean_generated_title(value: str) -> str:
    title = value.strip()
    title = title.splitlines()[0] if title else ""
    title = re.sub(r"^(タイトル|題名)\s*[:：]\s*", "", title)
    title = title.strip(" \t\r\n\"'`「」『』【】[]")
    return _compact_text(title)


def _first_user_message(messages: Sequence[ConversationMessage]) -> Optional[str]:
    for message in messages:
        if message.role == "user" and _compact_text(message.content):
            return message.content
    return None


def _title_generation_source(session) -> Optional[str]:
    context = getattr(session, "context", None)
    if not isinstance(context, dict):
        return None
    generation = context.get(TITLE_GENERATION_CONTEXT_KEY)
    if not isinstance(generation, dict):
        return None
    source = generation.get("source")
    return source if isinstance(source, str) else None


def _is_replaceable_fallback_title(
    title: str,
    messages: Sequence[ConversationMessage],
) -> bool:
    first_user = _first_user_message(messages)
    if not first_user:
        return False
    return _compact_text(title) == fallback_title_from_first_message(first_user)


async def _update_generated_title(
    repo,
    session_id: str,
    generated: GeneratedConversationTitle,
) -> bool:
    update_title = getattr(repo, "update_session_title")
    signature = inspect.signature(update_title)
    if "source" in signature.parameters:
        return await update_title(
            session_id,
            generated.title,
            source=generated.source,
        )
    if "title_source" in signature.parameters:
        return await update_title(
            session_id,
            generated.title,
            title_source=generated.source,
        )
    return await update_title(session_id, generated.title)


def _has_title_context(messages: Sequence[ConversationMessage]) -> bool:
    user_count = 0
    has_assistant = False
    for message in messages:
        if not _compact_text(message.content):
            continue
        if message.role == "user":
            user_count += 1
        elif message.role == "assistant":
            has_assistant = True
    return user_count >= 1 and (has_assistant or user_count >= 2)


def _prompt_messages(messages: Sequence[ConversationMessage]) -> list[ConversationMessage]:
    selected: list[ConversationMessage] = []
    user_count = 0
    assistant_count = 0
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        if not _compact_text(message.content):
            continue
        if message.role == "user":
            if user_count >= 2:
                continue
            user_count += 1
        elif message.role == "assistant":
            if assistant_count >= 2:
                continue
            assistant_count += 1
        selected.append(message)
    return selected


def build_title_prompt(messages: Sequence[ConversationMessage]) -> str:
    lines = []
    for message in _prompt_messages(messages):
        role = "ユーザー" if message.role == "user" else "アシスタント"
        content = _compact_text(message.content)[:TITLE_CONTENT_LIMIT]
        lines.append(f"{role}: {content}")

    excerpt = "\n".join(lines)
    return f"""以下は会話の最初の1〜2往復です。この会話を履歴一覧で識別しやすい短い日本語タイトルにしてください。

条件:
- 15文字以内を目安にする
- 固有名詞や主題を優先する
- ユーザー本文の丸写しを避ける
- タイトルのみ出力する

会話:
{excerpt}"""


async def generate_conversation_title(
    messages: Sequence[ConversationMessage],
    llm_generator: Optional[TitleGenerator],
) -> Optional[GeneratedConversationTitle]:
    first_user = _first_user_message(messages)
    if not first_user or not _has_title_context(messages):
        return None

    if llm_generator:
        try:
            generated = llm_generator(build_title_prompt(messages))
            if inspect.isawaitable(generated):
                generated = await generated
            if generated:
                title = clean_generated_title(str(generated))
                if 0 < len(title) <= TITLE_MAX_LENGTH:
                    return GeneratedConversationTitle(title=title, source="llm")
        except Exception as exc:
            logger.warning("LLM conversation title generation failed: %s", exc)

    fallback = fallback_title_from_first_message(first_user)
    if fallback:
        return GeneratedConversationTitle(title=fallback, source="fallback")
    return None


async def ensure_conversation_title(
    *,
    repo,
    session_id: str,
    llm_generator: Optional[TitleGenerator] = None,
) -> Optional[GeneratedConversationTitle]:
    session = await repo.get_session_by_id(session_id, with_messages=False)
    if not session:
        return None

    if hasattr(repo, "get_active_branch_messages"):
        messages = await repo.get_active_branch_messages(session_id)
    else:
        messages = await repo.get_session_messages(session_id)

    current_title = _compact_text(session.title or "")
    current_source = _title_generation_source(session)
    can_replace_current = (
        not current_title
        or current_source == "fallback"
        or _is_replaceable_fallback_title(current_title, messages)
    )
    if not can_replace_current:
        return None

    generated = await generate_conversation_title(messages, llm_generator)
    if not generated:
        return None
    if current_title == generated.title and current_source == generated.source:
        return None
    if current_title and generated.source != "llm":
        return None

    updated = await _update_generated_title(repo, session_id, generated)
    return generated if updated else None
