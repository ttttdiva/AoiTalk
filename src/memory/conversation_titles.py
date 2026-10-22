"""Atomic title ownership updates shared by conversation repositories."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ConversationSession


async def update_title_in_transaction(
    session: AsyncSession,
    session_id: UUID,
    title: str,
    *,
    source: str | None = None,
    expected_title: str | None = None,
) -> bool:
    """Preserve current context and respect manual ownership while locked.

    The caller commits the transaction. Select scalar columns rather than a
    cached ORM object, so a title job sees a rename even when its text did not
    change. The lock covers both the context read and the title/context write.
    """
    result = await session.execute(
        select(ConversationSession.title, ConversationSession.context)
        .where(
            ConversationSession.id == session_id,
            ConversationSession.deleted_at.is_(None),
        )
        .with_for_update()
    )
    row = result.one_or_none()
    if row is None:
        return False
    current_title, current_context = row
    if expected_title is not None and str(current_title or "") != expected_title:
        return False
    context = dict(current_context) if isinstance(current_context, dict) else {}
    generation = context.get("title_generation")
    if (
        source in {"llm", "fallback"}
        and isinstance(generation, dict)
        and generation.get("source") == "manual"
    ):
        return False

    values = {"title": title}
    if source:
        context["title_generation"] = {"source": source}
        values["context"] = context
    result = await session.execute(
        update(ConversationSession)
        .where(
            ConversationSession.id == session_id,
            ConversationSession.deleted_at.is_(None),
        )
        .values(**values)
    )
    return result.rowcount > 0
