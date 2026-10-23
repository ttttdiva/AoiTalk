"""Sync v3 /ops の applier。v2 push の ``_dispatch_sync_operation`` へ委譲する。"""
from __future__ import annotations

import copy
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    ConversationSession,
    DocsLibrary,
    KnowledgeNode,
    KnowledgeSupertag,
)
from ..memory.project_repository import ProjectRepository
from ..services.app_operation_lock import app_operation_lock, project_operation_lock
from ..services.app_storage import get_workspaces_root
from ..services.task_management_service import TaskManagementService
from ..sync_v3.rows import load_rows
from ..services.agent_root_lifecycle import attach_private_root_metadata
from .docs_sync import (
    docs_composite_response_readable,
    load_current_docs_entity,
    resolve_docs_conflict_library,
    serialize_docs_node_for_sync,
)
from .sync_routes import (
    SyncOperation,
    _apply_sync_conversation_statuses,
    _conversation_session_payload,
    _dispatch_sync_operation,
    _load_current_task_payload,
    _load_current_time_entry_payload,
)

V3_TABLES = (
    "projects",
    "tasks",
    "time_entries",
    "conversation_sessions",
    "knowledge_nodes",
    "knowledge_node_supertags",
    "knowledge_field_values",
    "knowledge_supertags",
)
DOCS_TABLES = frozenset(
    {
        "knowledge_nodes",
        "knowledge_node_supertags",
        "knowledge_field_values",
        "knowledge_supertags",
    }
)


def _parse_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


class V2DelegatingApplier:
    def __init__(
        self,
        table: str,
        service: TaskManagementService,
        workspace_root: str | os.PathLike[str] | None,
        session_factory: Callable[[], Awaitable[AsyncSession]],
    ) -> None:
        self.table = table
        self.service = service
        self.workspace_root = workspace_root
        self.session_factory = session_factory

    @asynccontextmanager
    async def outer_lock(self, op: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        entity_id = _parse_uuid(op["entity_id"])
        root = get_workspaces_root(self.workspace_root)
        if self.table == "projects" and op["action"] == "delete" and entity_id is not None:
            async with project_operation_lock(entity_id, workspace_root=root):
                yield {}
            return
        if (
            self.table == "knowledge_nodes"
            and op["action"] in ("update", "delete")
            and "body_json" in op["patch"]
            and entity_id is not None
        ):
            session = await self.session_factory()
            try:
                row = (
                    await session.execute(
                        select(KnowledgeNode.node_type, KnowledgeNode.app_id).where(
                            KnowledgeNode.id == entity_id
                        )
                    )
                ).first()
                await session.rollback()
            finally:
                await session.close()
            if row is not None and row.node_type == "app_readme" and row.app_id is not None:
                async with app_operation_lock(row.app_id, workspace_root=root):
                    yield {"app_id": row.app_id}
                return
        yield {}

    async def apply(
        self,
        session: AsyncSession,
        user_id: UUID,
        user_info: dict[str, Any],
        op: dict[str, Any],
        held: dict[str, Any],
        after_commit: list[Callable[[], None]],
    ) -> tuple[str | None, dict[str, Any]]:
        operation = SyncOperation(
            op_id=str(op["op_id"]),
            table=op["table"],
            action=op["action"],
            entity_id=op["entity_id"],
            payload=copy.deepcopy(op["patch"]),
            base_updated_at=op["base"].get("updated_at"),
        )
        outcome: dict[str, Any] = {}
        entity = await _dispatch_sync_operation(
            self.service,
            session,
            user_id=user_id,
            user_info=user_info,
            operation=operation,
            workspace_root=self.workspace_root,
            commit=False,
            after_commit=after_commit,
            held_app_lock=held.get("app_id"),
            outcome=outcome,
        )
        entity = entity if isinstance(entity, dict) else {}
        if self.table == "knowledge_node_supertags" and "node_id" in entity and "supertag_id" in entity:
            key = f"{entity['node_id']}:{entity['supertag_id']}"
        elif self.table == "knowledge_field_values":
            key = op["entity_id"]
        else:
            key = entity.get("id") or op["entity_id"]
        return str(key), outcome

    async def load_current(
        self, session: AsyncSession, user_id: UUID, table: str, key: str
    ) -> dict[str, Any] | None:
        """commit 後の現在行を、v2 push が同じ table の応答 entity に使う serializer で返す。"""
        # 削除済みの行は None にする。v2 の delete 応答（idempotent・task_ids 等の削除結果）は
        # 削除後の行から再構成できず、tombstone をそのまま返すと v2 と異なる形になる。
        if table == "tasks":
            entity_id = _parse_uuid(key)
            payload = (
                await _load_current_task_payload(self.service, session, entity_id, user_id=user_id)
                if entity_id is not None
                else None
            )
            return None if payload is None or payload.get("deleted_at") else payload
        if table == "time_entries":
            entity_id = _parse_uuid(key)
            payload = (
                await _load_current_time_entry_payload(
                    self.service, session, entity_id, user_id=user_id
                )
                if entity_id is not None
                else None
            )
            return None if payload is None or payload.get("deleted_at") else payload
        if table in ("projects", "conversation_sessions"):
            # 可視性は pull と同じ判定を使い、見えない行は serializer へ渡さない。
            visible = (await load_rows(session, user_id, table, [key])).get(key)
            entity_id = _parse_uuid(key)
            if visible is None or visible[0] != "row" or entity_id is None:
                return None
            if table == "projects":
                project = await ProjectRepository.get_by_id(session, entity_id)
                return project.to_dict() if project is not None else None
            row = (
                await session.execute(
                    select(ConversationSession).where(ConversationSession.id == entity_id)
                )
            ).scalar_one_or_none()
            if row is None or row.deleted_at is not None:
                return None
            await attach_private_root_metadata(session, [row])
            payload = _conversation_session_payload(row)
            await _apply_sync_conversation_statuses(session, [payload])
            return payload
        return await self._load_current_docs(session, user_id, table, key)

    async def load_current_for_op(
        self,
        session: AsyncSession,
        user_id: UUID,
        table: str,
        key: str,
        op: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load a response using v2's payload-based library on Docs conflicts."""
        if (
            table in DOCS_TABLES
            and record.get("status") == "rejected"
            and record.get("code") == "field_conflict"
        ):
            return await self._load_current_docs(
                session,
                user_id,
                table,
                key,
                conflict_payload=op.get("patch") or {},
            )
        return await self.load_current(session, user_id, table, key)

    async def _load_current_docs(
        self,
        session: AsyncSession,
        user_id: UUID,
        table: str,
        key: str,
        *,
        conflict_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        head = _parse_uuid(key.split(":", 1)[0])
        if head is None:
            return None
        if conflict_payload is not None:
            library = await resolve_docs_conflict_library(
                session,
                user_id=user_id,
                entity_id=key,
                payload=conflict_payload,
                read_only=True,
            )
            library_id = library.id if library is not None else None
        else:
            # Applied entities use the library of the row written by v2's
            # dispatcher. Conflict responses instead use its payload resolver.
            if table == "knowledge_supertags":
                tag = await session.get(KnowledgeSupertag, head)
                library_id = tag.docs_library_id if tag is not None else None
            else:
                node = await session.get(KnowledgeNode, head)
                library_id = node.docs_library_id if node is not None else None
        if library_id is None:
            return None
        composite = table in ("knowledge_node_supertags", "knowledge_field_values")
        if composite and not await docs_composite_response_readable(
            session,
            user_id=user_id,
            table=table,
            entity_id=key,
            docs_library_id=library_id,
        ):
            return None
        entity = await load_current_docs_entity(
            session, docs_library_id=library_id, table=table, entity_id=key, user_id=user_id
        )
        # 行が無い・見えない場合の stub は、版を持たないため entity として返さない。
        if entity is None or (entity.get("deleted") is True and entity.get("updated_at") is None):
            return None
        if table == "knowledge_nodes":
            # v2 push の応答は node に source/access/read_only を付ける（_finish_docs_operation）。
            node = await session.get(KnowledgeNode, head)
            library = await session.get(DocsLibrary, library_id)
            if node is None or library is None:
                return None
            return await serialize_docs_node_for_sync(
                session, node, library=library, user_id=user_id
            )
        return entity


def build_v3_appliers(
    service: TaskManagementService,
    workspace_root: str | os.PathLike[str] | None,
    session_factory: Callable[[], Awaitable[AsyncSession]],
) -> dict[str, V2DelegatingApplier]:
    return {
        table: V2DelegatingApplier(table, service, workspace_root, session_factory)
        for table in V3_TABLES
    }
