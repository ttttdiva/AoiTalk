"""タスク参照の共通処理とAgent Run由来参照の解決。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    AgentRun,
    AgentRunEdge,
    ConversationParticipant,
    ConversationSession,
    TaskReference,
)


async def resolve_agent_run_origin(
    session: AsyncSession,
    *,
    agent_run_id: str | UUID | None,
    user_id: UUID,
) -> dict[str, Any] | None:
    """現在Runから親を辿り、閲覧可能な発生元チャットを返す。"""
    try:
        current_id = UUID(str(agent_run_id)) if agent_run_id else None
    except (TypeError, ValueError):
        return None
    if current_id is None:
        return None

    visited: set[UUID] = set()
    run = await session.get(AgentRun, current_id)
    while run is not None and run.id not in visited:
        visited.add(run.id)
        if run.session_id is not None:
            access = await session.execute(
                select(ConversationSession).where(
                    ConversationSession.id == run.session_id,
                    ConversationSession.deleted_at.is_(None),
                    or_(
                        ConversationSession.user_id == str(user_id),
                        ConversationSession.id.in_(
                            select(ConversationParticipant.session_id).where(
                                ConversationParticipant.session_id == run.session_id,
                                ConversationParticipant.participant_type == "user",
                                ConversationParticipant.participant_id == str(user_id),
                                ConversationParticipant.status == "joined",
                            )
                        ),
                    ),
                )
            )
            conversation = access.scalar_one_or_none()
            if conversation is not None:
                return {
                    "session": conversation,
                    "source_run_id": run.id,
                    "trigger_message_id": run.trigger_message_id,
                }
        next_run_id = run.parent_run_id
        if next_run_id is None and run.root_run_id and run.root_run_id != run.id:
            next_run_id = run.root_run_id
        if next_run_id is None:
            edge_result = await session.execute(
                select(AgentRunEdge.parent_run_id)
                .where(AgentRunEdge.child_run_id == run.id)
                .order_by(AgentRunEdge.created_at.desc())
                .limit(1)
            )
            next_run_id = edge_result.scalar_one_or_none()
        if next_run_id is None:
            break
        run = await session.get(AgentRun, next_run_id)
    return None


async def attach_workspace_file_reference(
    session: AsyncSession,
    *,
    task_id: UUID,
    project_id: UUID,
    user_id: UUID,
    path: str,
    display_name: str = "",
    metadata: dict[str, Any] | None = None,
) -> TaskReference | None:
    """タスクへ添付原本(workspace上のファイル)の参照を作る。"""
    raw_path = str(path or "").strip().replace("\\", "/")
    if not raw_path:
        return None
    if raw_path.startswith("/") or re.match(r"^[A-Za-z]:/", raw_path):
        raise ValueError("workspace file path must be project-relative")
    if ".." in PurePosixPath(raw_path).parts:
        raise ValueError("workspace file path must not traverse parent directories")

    project_prefix = f"_projects/project_{project_id}/"
    if raw_path.startswith("_projects/project_"):
        if not raw_path.startswith(project_prefix):
            raise ValueError("workspace file path belongs to a different project")
        target_path = raw_path[len(project_prefix) :]
    else:
        target_path = raw_path
    if not target_path:
        return None

    # Task reference APIと同じproject-relative path / dedupe形式へ揃える。
    dedupe_key = f"|{target_path[:1198]}|"
    existing = await session.execute(
        select(TaskReference).where(
            TaskReference.task_id == task_id,
            TaskReference.reference_type == "workspace_file",
            TaskReference.relation_type == "source",
            TaskReference.dedupe_key == dedupe_key,
        )
    )
    reference = existing.scalar_one_or_none()
    if reference is not None:
        return reference

    reference = TaskReference(
        task_id=task_id,
        project_id=project_id,
        reference_type="workspace_file",
        relation_type="source",
        target_path=target_path,
        display_name=(display_name or target_path.rsplit("/", 1)[-1])[:500],
        dedupe_key=dedupe_key,
        reference_metadata=metadata or {},
        created_by=user_id,
    )
    session.add(reference)
    await session.flush()
    return reference


async def attach_agent_run_source_reference(
    session: AsyncSession,
    *,
    task_id: UUID,
    project_id: UUID,
    user_id: UUID,
    agent_run_id: str | UUID | None,
) -> TaskReference | None:
    origin = await resolve_agent_run_origin(
        session, agent_run_id=agent_run_id, user_id=user_id
    )
    if origin is None:
        return None

    conversation: ConversationSession = origin["session"]
    target_id = str(conversation.id)
    existing = await session.execute(
        select(TaskReference).where(
            TaskReference.task_id == task_id,
            TaskReference.reference_type == "conversation_session",
            TaskReference.relation_type == "source",
            TaskReference.dedupe_key == target_id,
        )
    )
    reference = existing.scalar_one_or_none()
    if reference is not None:
        return reference

    trigger_message_id = origin.get("trigger_message_id")
    reference = TaskReference(
        task_id=task_id,
        project_id=project_id,
        reference_type="conversation_session",
        relation_type="source",
        target_id=target_id,
        display_name=conversation.title or "無題の会話",
        dedupe_key=target_id,
        reference_metadata={
            "agent_run_id": str(origin["source_run_id"]),
            "trigger_message_id": str(trigger_message_id)
            if trigger_message_id
            else None,
            "title_snapshot": conversation.title or "無題の会話",
        },
        created_by=user_id,
    )
    session.add(reference)
    await session.flush()
    return reference
