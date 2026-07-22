"""Docs REST API routes (/api/docs/*)。

完了条件1（Bearer トークンで動作）を満たす正準 REST 面。書き込み系は
``apply_docs_operation`` に委譲し、``/api/sync/push`` の Docs ハンドラと同一実装を共有する。
モバイルが直接使うのは online 限定の search / today のみ。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..memory.models import KnowledgeNode
from ..services.docs_graph_service import DocsGraphService
from .docs_sync import (
    DocsOperationError,
    apply_docs_operation,
    serialize_docs_edge,
    serialize_docs_field,
    serialize_docs_field_value,
    serialize_docs_node,
    serialize_docs_node_supertag,
    serialize_docs_placement,
    serialize_docs_supertag,
    serialize_docs_supertag_field,
)

logger = logging.getLogger(__name__)


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid since") from exc


class NodeCreateBody(BaseModel):
    id: Optional[str] = None
    parent_id: Optional[str] = None
    project_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    body_json: Optional[dict[str, Any]] = None
    node_type: Optional[str] = None
    sort_order: Optional[float] = None
    day_date: Optional[str] = None


class NodePatchBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    body_json: Optional[dict[str, Any]] = None
    parent_id: Optional[str] = None
    sort_order: Optional[float] = None
    project_id: Optional[str] = None
    day_date: Optional[str] = None


class NodeMoveBody(BaseModel):
    new_parent_id: str
    sort_order: Optional[float] = None
    leave_reference: bool = False


class SupertagAddBody(BaseModel):
    supertag_id: Optional[str] = None
    name: Optional[str] = None


class FieldValueBody(BaseModel):
    value: Any = None


class SupertagCreateBody(BaseModel):
    name: str
    base_type: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None


def create_docs_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/docs", tags=["docs"])

    async def _get_current_user(request: Request) -> UUID:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"])

    def _map_error(exc: Exception) -> HTTPException:
        if isinstance(exc, DocsOperationError):
            return HTTPException(status_code=exc.status_code, detail=exc.message)
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    async def _write(request: Request, table: str, action: str, entity_id: str, payload: dict[str, Any]):
        user_id = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            service = DocsGraphService(session)
            workspace = await service.ensure_workspace(user_id)
            await session.commit()
            try:
                return await apply_docs_operation(
                    session,
                    service,
                    user_id=user_id,
                    workspace_id=workspace.id,
                    table=table,
                    action=action,
                    entity_id=entity_id,
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                raise _map_error(exc) from exc
        finally:
            await session.close()

    # ---------------- 読み取り ----------------

    @router.get("/tree")
    async def get_tree(
        request: Request,
        since: Optional[str] = None,
        include_archived: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        since_dt = _parse_since(since)
        session = await get_db_manager().get_session()
        try:
            service = DocsGraphService(session)
            workspace = await service.ensure_workspace(user_id)
            await session.commit()
            from sqlalchemy import or_, select
            from ..memory.models import (
                KnowledgeEdge,
                KnowledgeField,
                KnowledgeFieldValue,
                KnowledgeNodePlacement,
                KnowledgeNodeSupertag,
                KnowledgeSupertag,
                KnowledgeSupertagField,
            )

            node_stmt = select(KnowledgeNode).where(KnowledgeNode.workspace_id == workspace.id)
            if not (include_archived == "1"):
                node_stmt = node_stmt.where(KnowledgeNode.archived_at.is_(None))
            if since_dt:
                node_stmt = node_stmt.where(
                    or_(KnowledgeNode.updated_at > since_dt, KnowledgeNode.archived_at > since_dt)
                )
            nodes = list((await session.execute(node_stmt)).scalars().all())

            supertags = list(
                (await session.execute(
                    select(KnowledgeSupertag).where(KnowledgeSupertag.workspace_id == workspace.id)
                )).scalars().all()
            )
            fields = list(
                (await session.execute(
                    select(KnowledgeField).where(KnowledgeField.workspace_id == workspace.id)
                )).scalars().all()
            )
            supertag_fields = list(
                (await session.execute(
                    select(KnowledgeSupertagField)
                    .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
                    .where(KnowledgeSupertag.workspace_id == workspace.id)
                )).scalars().all()
            )
            node_supertags = list(
                (await session.execute(
                    select(KnowledgeNodeSupertag)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
                    .where(KnowledgeNode.workspace_id == workspace.id)
                )).scalars().all()
            )
            field_values = list(
                (await session.execute(
                    select(KnowledgeFieldValue)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
                    .where(KnowledgeNode.workspace_id == workspace.id)
                )).scalars().all()
            )
            placements = list(
                (await session.execute(
                    select(KnowledgeNodePlacement)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodePlacement.node_id)
                    .where(KnowledgeNode.workspace_id == workspace.id)
                )).scalars().all()
            )
            edges = list(
                (await session.execute(
                    select(KnowledgeEdge)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeEdge.source_node_id)
                    .where(KnowledgeNode.workspace_id == workspace.id)
                )).scalars().all()
            )
            return {
                "workspace": {
                    "id": str(workspace.id),
                    "name": workspace.name,
                    "owner_user_id": str(workspace.owner_user_id) if workspace.owner_user_id else None,
                },
                "nodes": [serialize_docs_node(n) for n in nodes],
                "supertags": [serialize_docs_supertag(s) for s in supertags],
                "node_supertags": [serialize_docs_node_supertag(n) for n in node_supertags],
                "supertag_fields": [serialize_docs_supertag_field(s) for s in supertag_fields],
                "fields": [serialize_docs_field(f) for f in fields],
                "field_values": [serialize_docs_field_value(v) for v in field_values],
                "placements": [serialize_docs_placement(p) for p in placements],
                "edges": [serialize_docs_edge(e) for e in edges],
            }
        finally:
            await session.close()

    @router.get("/search")
    async def search(
        request: Request,
        q: Optional[str] = None,
        tag: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 20,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            service = DocsGraphService(session)
            workspace = await service.ensure_workspace(user_id)
            await session.commit()
            project_id = None
            if project:
                project_obj = await service.resolve_project(project)
                project_id = project_obj.id if project_obj else None
            nodes = await service.search(
                workspace_id=workspace.id,
                query=q or "",
                project_id=project_id,
                tag=tag or "",
                limit=limit,
            )
            parents = await service._parent_titles(nodes)
            node_ids = [n.id for n in nodes]
            from sqlalchemy import select
            from ..memory.models import KnowledgeNodeSupertag, KnowledgeSupertag

            tags_by_node: dict[Any, list[str]] = {}
            if node_ids:
                tag_rows = await session.execute(
                    select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
                    .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
                    .where(KnowledgeNodeSupertag.node_id.in_(node_ids))
                )
                for nid, name in tag_rows.all():
                    tags_by_node.setdefault(nid, []).append(name)
            results = [
                {
                    "id": str(n.id),
                    "title": n.title,
                    "tags": tags_by_node.get(n.id, []),
                    "project_id": str(n.project_id) if n.project_id else None,
                    "parent_title": parents.get(n.id),
                }
                for n in nodes
            ]
            return {"results": results}
        finally:
            await session.close()

    @router.get("/nodes/{node_id}")
    async def get_node(
        request: Request,
        node_id: str,
        _auth=Depends(require_auth_dependency),
    ):
        """認証ユーザーのDocs workspace内にあるNodeを読み取る。"""
        user_id = await _get_current_user(request)
        try:
            node_uuid = UUID(node_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid node id") from exc
        session = await get_db_manager().get_session()
        try:
            service = DocsGraphService(session)
            workspace = await service.ensure_workspace(user_id)
            await session.commit()
            from sqlalchemy import select
            from ..memory.models import (
                KnowledgeEdge,
                KnowledgeField,
                KnowledgeFieldValue,
                KnowledgeNodePlacement,
                KnowledgeNodeSupertag,
                KnowledgeSupertag,
                KnowledgeSupertagField,
            )

            node = await session.scalar(
                select(KnowledgeNode).where(
                    KnowledgeNode.id == node_uuid,
                    KnowledgeNode.workspace_id == workspace.id,
                )
            )
            if node is None:
                raise HTTPException(status_code=404, detail="Docs node not found")
            node_supertags = list(
                (await session.execute(
                    select(KnowledgeNodeSupertag).where(
                        KnowledgeNodeSupertag.node_id == node.id
                    )
                )).scalars().all()
            )
            field_values = list(
                (await session.execute(
                    select(KnowledgeFieldValue).where(
                        KnowledgeFieldValue.node_id == node.id
                    )
                )).scalars().all()
            )
            placements = list(
                (await session.execute(
                    select(KnowledgeNodePlacement).where(
                        KnowledgeNodePlacement.node_id == node.id
                    )
                )).scalars().all()
            )
            edges = list(
                (await session.execute(
                    select(KnowledgeEdge).where(
                        (KnowledgeEdge.source_node_id == node.id)
                        | (KnowledgeEdge.target_node_id == node.id)
                    )
                )).scalars().all()
            )
            supertag_ids = [item.supertag_id for item in node_supertags]
            supertags = []
            if supertag_ids:
                supertags = list(
                    (await session.execute(
                        select(KnowledgeSupertag).where(
                            KnowledgeSupertag.id.in_(supertag_ids),
                            KnowledgeSupertag.workspace_id == workspace.id,
                        )
                    )).scalars().all()
                )
            fields = list(
                (await session.execute(
                    select(KnowledgeField).where(KnowledgeField.workspace_id == workspace.id)
                )).scalars().all()
            )
            supertag_fields = list(
                (await session.execute(
                    select(KnowledgeSupertagField)
                    .where(KnowledgeSupertagField.supertag_id.in_(supertag_ids))
                )).scalars().all()
            ) if supertag_ids else []
            return {
                "node": serialize_docs_node(node),
                "nodes": [serialize_docs_node(node)],
                "supertags": [serialize_docs_supertag(item) for item in supertags],
                "node_supertags": [serialize_docs_node_supertag(item) for item in node_supertags],
                "supertag_fields": [serialize_docs_supertag_field(item) for item in supertag_fields],
                "fields": [serialize_docs_field(item) for item in fields],
                "field_values": [serialize_docs_field_value(item) for item in field_values],
                "placements": [serialize_docs_placement(item) for item in placements],
                "edges": [serialize_docs_edge(item) for item in edges],
            }
        finally:
            await session.close()

    @router.get("/today")
    async def today(
        request: Request,
        date: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        from datetime import date as date_cls
        import zoneinfo

        user_id = await _get_current_user(request)
        if date:
            try:
                target = date_cls.fromisoformat(date)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid date") from exc
        else:
            target = datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).date()
        session = await get_db_manager().get_session()
        try:
            service = DocsGraphService(session)
            workspace = await service.ensure_workspace(user_id)
            node, supertag, node_supertags = await service.ensure_daily_page(
                workspace_id=workspace.id, user_id=user_id, day=target
            )
            await session.commit()
            return {
                "node": serialize_docs_node(node),
                "supertag": serialize_docs_supertag(supertag),
                "node_supertags": [serialize_docs_node_supertag(ns) for ns in node_supertags],
            }
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            raise _map_error(exc) from exc
        finally:
            await session.close()

    # ---------------- 書き込み（apply_docs_operation 委譲） ----------------

    @router.post("/nodes")
    async def create_node(request: Request, body: NodeCreateBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_none=False)
        entity_id = payload.get("id") or ""
        node = await _write(request, "knowledge_nodes", "create", entity_id, payload)
        return {"node": node}

    @router.patch("/nodes/{node_id}")
    async def patch_node(request: Request, node_id: str, body: NodePatchBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_unset=True)
        node = await _write(request, "knowledge_nodes", "update", node_id, payload)
        return {"node": node}

    @router.post("/nodes/{node_id}/move")
    async def move_node(request: Request, node_id: str, body: NodeMoveBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump()
        node = await _write(request, "knowledge_nodes", "move", node_id, payload)
        return {"node": node}

    @router.post("/nodes/{node_id}/archive")
    async def archive_node(request: Request, node_id: str, _auth=Depends(require_auth_dependency)):
        node = await _write(request, "knowledge_nodes", "archive", node_id, {})
        return {"node": node}

    @router.post("/nodes/{node_id}/supertags")
    async def add_supertag(request: Request, node_id: str, body: SupertagAddBody, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, **body.model_dump(exclude_none=True)}
        entity = await _write(request, "knowledge_node_supertags", "create", node_id, payload)
        return {"ok": True, "entity": entity}

    @router.delete("/nodes/{node_id}/supertags/{supertag_id}")
    async def remove_supertag(request: Request, node_id: str, supertag_id: str, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, "supertag_id": supertag_id}
        entity = await _write(request, "knowledge_node_supertags", "delete", f"{node_id}:{supertag_id}", payload)
        return {"ok": True, "entity": entity}

    @router.put("/nodes/{node_id}/fields/{field_id}")
    async def set_field(request: Request, node_id: str, field_id: str, body: FieldValueBody, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, "field_id": field_id, "value": body.value}
        entity = await _write(request, "knowledge_field_values", "update", f"{node_id}:{field_id}", payload)
        return {"ok": True, "entity": entity}

    @router.post("/supertags")
    async def create_supertag(request: Request, body: SupertagCreateBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_none=True)
        entity = await _write(request, "knowledge_supertags", "create", "", payload)
        return {"ok": True, "entity": entity}

    return router
