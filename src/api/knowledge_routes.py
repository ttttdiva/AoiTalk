"""Knowledge Workspace API routes."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..knowledge.service import KnowledgeSearchFilters, KnowledgeService
from ..memory.models import KnowledgeAnnotation

logger = logging.getLogger(__name__)


class CreateKnowledgeSourcePayload(BaseModel):
    name: str
    description: Optional[str] = None
    root_path: Optional[str] = None
    project_id: Optional[str] = None
    source_type: str = "local_dir"
    # GROWI 連携用（source_type == "growi"）
    base_url: Optional[str] = None
    api_token: Optional[str] = None
    include_patterns: Optional[list[str]] = None
    exclude_patterns: Optional[list[str]] = None
    sync_mode: str = "manual"
    write_policy: str = "propose_patch"
    access_policy: Optional[dict[str, Any]] = None
    auto_sync: bool = False


class UpdateKnowledgeSourcePayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    api_token: Optional[str] = None
    include_patterns: Optional[list[str]] = None
    exclude_patterns: Optional[list[str]] = None
    sync_mode: Optional[str] = None
    write_policy: Optional[str] = None
    access_policy: Optional[dict[str, Any]] = None


class GrowiTestPayload(BaseModel):
    base_url: str
    api_token: str


class KnowledgeSearchPayload(BaseModel):
    query: str = ""
    source_id: Optional[str] = None
    project_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    extension: Optional[str] = None
    path_prefix: Optional[str] = None
    top_k: int = 10


class OrganizeKnowledgePayload(BaseModel):
    dry_run: bool = True
    limit: int = 200


class AnnotationStatusPayload(BaseModel):
    status: str


class ProposeReplacementPayload(BaseModel):
    replacement_content: str
    reason: str = ""


def create_knowledge_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

    async def _get_user_or_401(request: Request) -> dict[str, Any]:
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user_info

    def _actor_id(user_info: dict[str, Any]) -> UUID:
        return UUID(str(user_info["id"]))

    def _is_admin(user_info: dict[str, Any]) -> bool:
        return user_info.get("role") == "admin"

    @router.get("/sources")
    async def list_sources(
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            sources = await KnowledgeService.list_sources(
                session,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            return {"sources": [source.to_dict() for source in sources]}
        finally:
            await session.close()

    @router.post("/sources")
    async def create_source(
        payload: CreateKnowledgeSourcePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            if payload.source_type == "growi":
                source = await KnowledgeService.create_growi_source(
                    session,
                    actor_user_id=_actor_id(user_info),
                    name=payload.name,
                    base_url=payload.base_url or payload.root_path or "",
                    api_token=payload.api_token or "",
                    description=payload.description,
                    exclude_patterns=payload.exclude_patterns,
                    sync_mode=payload.sync_mode,
                    access_policy=payload.access_policy,
                )
            elif payload.project_id:
                from ..memory.project_repository import ProjectRepository
                from ..tools.file_explorer import get_root_dir

                project_uuid = UUID(payload.project_id)
                actor_id = _actor_id(user_info)
                if not _is_admin(user_info):
                    allowed = await ProjectRepository.has_permission(
                        session,
                        project_id=project_uuid,
                        user_id=actor_id,
                        permission="write",
                    )
                    if not allowed:
                        raise HTTPException(status_code=403, detail="Project write permission required")
                storage_path = await ProjectRepository.get_storage_path(project_uuid)
                root_path = get_root_dir() / storage_path
                root_path.mkdir(parents=True, exist_ok=True)
                source = await KnowledgeService.create_project_workspace_source(
                    session,
                    actor_user_id=actor_id,
                    project_id=project_uuid,
                    name=payload.name,
                    root_path=str(root_path),
                    description=payload.description,
                    include_patterns=payload.include_patterns,
                    exclude_patterns=payload.exclude_patterns,
                    sync_mode=payload.sync_mode,
                    write_policy=payload.write_policy,
                )
            else:
                raise ValueError("Knowledge Sourceの作成にはproject_idが必要です")
            sync_result = None
            if payload.auto_sync:
                sync_result = await KnowledgeService.sync_source(
                    session,
                    source_id=source.id,
                    actor_user_id=_actor_id(user_info),
                    is_admin=_is_admin(user_info),
                )
            await session.commit()
            return {"source": source.to_dict(), "sync": sync_result}
        except HTTPException:
            await session.rollback()
            raise
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            await session.rollback()
            logger.exception("Failed to create knowledge source")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.post("/sources/growi/test")
    async def test_growi_connection(
        payload: GrowiTestPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        await _get_user_or_401(request)
        from ..knowledge.growi_client import GrowiClient, GrowiClientError

        try:
            client = GrowiClient(base_url=payload.base_url, api_token=payload.api_token)
            result = await client.test_connection()
            return {"ok": True, "detail": result}
        except GrowiClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/sources/{source_id}")
    async def update_source(
        source_id: str,
        payload: UpdateKnowledgeSourcePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            source_uuid = UUID(source_id)
            allowed = await KnowledgeService.can_write_source(
                session,
                source_id=source_uuid,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="Write permission required")
            source = await KnowledgeService.get_source(session, source_uuid)
            if not source:
                raise HTTPException(status_code=404, detail="Knowledge source not found")
            updates = payload.model_dump(exclude_unset=True)
            # GROWI 連携フィールドは別カラム/暗号化プロパティへマッピングする。
            base_url = updates.pop("base_url", None)
            api_token = updates.pop("api_token", None)
            if base_url is not None:
                if source.source_type != "growi":
                    raise HTTPException(
                        status_code=400, detail="base_url は GROWI ソースのみ更新できます"
                    )
                normalized = base_url.strip().rstrip("/")
                if not normalized.startswith(("http://", "https://")):
                    raise HTTPException(
                        status_code=400,
                        detail="GROWI のURLは http:// または https:// で始まる必要があります",
                    )
                source.root_path = normalized
            if api_token is not None and api_token != "":
                if source.source_type != "growi":
                    raise HTTPException(
                        status_code=400, detail="api_token は GROWI ソースのみ更新できます"
                    )
                source.growi_api_token = api_token.strip()
            for key, value in updates.items():
                setattr(source, key, value)
            await session.commit()
            return {"source": source.to_dict()}
        except HTTPException:
            await session.rollback()
            raise
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.delete("/sources/{source_id}")
    async def delete_source(
        source_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            deleted = await KnowledgeService.delete_source(
                session,
                source_id=UUID(source_id),
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="Knowledge source not found")
            await session.commit()
            return {"success": True}
        except HTTPException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post("/sources/{source_id}/sync")
    async def sync_source(
        source_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            result = await KnowledgeService.sync_source(
                session,
                source_id=UUID(source_id),
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            await session.commit()
            return result
        except PermissionError as exc:
            await session.rollback()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.get("/sources/{source_id}/status")
    async def source_status(
        source_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            source_uuid = UUID(source_id)
            allowed = await KnowledgeService.can_read_source(
                session,
                source_id=source_uuid,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="Access denied")
            counts = await KnowledgeService.recount_source(session, source_uuid)
            source = await KnowledgeService.get_source(session, source_uuid)
            return {"source": source.to_dict() if source else None, "counts": counts}
        finally:
            await session.close()

    @router.post("/search")
    async def search(
        payload: KnowledgeSearchPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            filters = KnowledgeSearchFilters(
                source_id=UUID(payload.source_id) if payload.source_id else None,
                project_id=UUID(payload.project_id) if payload.project_id else None,
                tags=tuple(payload.tags or []),
                extension=payload.extension,
                path_prefix=payload.path_prefix,
            )
            results = await KnowledgeService.search(
                session,
                query=payload.query,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
                filters=filters,
                limit=max(1, min(payload.top_k, 50)),
            )
            return {"results": results}
        finally:
            await session.close()

    @router.get("/documents/{document_id}")
    async def read_document(
        document_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            return await KnowledgeService.read_document(
                session,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
                document_id=UUID(document_id),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.get("/documents/{document_id}/outline")
    async def document_outline(
        document_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            return await KnowledgeService.outline(
                session,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
                document_id=UUID(document_id),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.post("/sources/{source_id}/organize")
    async def organize_source(
        source_id: str,
        payload: OrganizeKnowledgePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            result = await KnowledgeService.organize(
                session,
                source_id=UUID(source_id),
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
                dry_run=payload.dry_run,
                limit=max(1, min(payload.limit, 2000)),
            )
            if not payload.dry_run:
                await session.commit()
            return result
        except PermissionError as exc:
            await session.rollback()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            await session.close()

    @router.get("/sources/{source_id}/annotations")
    async def list_annotations(
        source_id: str,
        request: Request,
        status: str = "proposed",
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            source_uuid = UUID(source_id)
            allowed = await KnowledgeService.can_read_source(
                session,
                source_id=source_uuid,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="Access denied")
            result = await session.execute(
                select(KnowledgeAnnotation)
                .options(selectinload(KnowledgeAnnotation.document))
                .where(
                    KnowledgeAnnotation.document.has(source_id=source_uuid),
                    KnowledgeAnnotation.status == status,
                )
                .order_by(KnowledgeAnnotation.created_at.desc())
                .limit(200)
            )
            return {
                "annotations": [
                    annotation.to_dict(include_document=True)
                    for annotation in result.scalars().all()
                ]
            }
        finally:
            await session.close()

    @router.patch("/annotations/{annotation_id}")
    async def update_annotation_status(
        annotation_id: str,
        payload: AnnotationStatusPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        if payload.status not in {"proposed", "accepted", "rejected", "stale"}:
            raise HTTPException(status_code=400, detail="Invalid annotation status")
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            result = await session.execute(
                select(KnowledgeAnnotation)
                .options(selectinload(KnowledgeAnnotation.document))
                .where(KnowledgeAnnotation.id == UUID(annotation_id))
            )
            annotation = result.scalar_one_or_none()
            if not annotation:
                raise HTTPException(status_code=404, detail="Annotation not found")
            allowed = await KnowledgeService.can_write_source(
                session,
                source_id=annotation.document.source_id,
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="Write permission required")
            annotation.status = payload.status
            await session.commit()
            return {"annotation": annotation.to_dict(include_document=True)}
        except HTTPException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post("/documents/{document_id}/propose-replacement")
    async def propose_replacement(
        document_id: str,
        payload: ProposeReplacementPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _get_user_or_401(request)
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            event = await KnowledgeService.propose_text_replacement(
                session,
                document_id=UUID(document_id),
                actor_user_id=_actor_id(user_info),
                is_admin=_is_admin(user_info),
                replacement_content=payload.replacement_content,
                reason=payload.reason,
            )
            await session.commit()
            return {"edit_event": event.to_dict()}
        except PermissionError as exc:
            await session.rollback()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            await session.close()

    return router
