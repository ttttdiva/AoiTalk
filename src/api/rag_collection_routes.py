"""
RAG Collection API routes for AoiTalk Web Interface

Provides endpoints for managing RAG collections (vector DBs) and
their linkage to projects and users.
"""

import logging
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Data Models ──────────────────────────────────────────────────────────

class CreateRagCollectionPayload(BaseModel):
    name: str
    description: Optional[str] = None
    source_directory: str
    include_patterns: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None
    auto_index: bool = True


class UpdateRagCollectionPayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class LinkCollectionPayload(BaseModel):
    collection_id: str


class StartIndexPayload(BaseModel):
    clear_existing: bool = False


class ImportQdrantCollectionPayload(BaseModel):
    qdrant_name: str
    display_name: str


class LinkUserPayload(BaseModel):
    user_id: str
    permission: str = "read"


# ── Router Factory ───────────────────────────────────────────────────────

def create_rag_collection_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency
) -> APIRouter:
    """Create the RAG collection router with dependencies injected."""
    router = APIRouter(tags=["rag"])

    from ..memory.rag_collection_repository import RagCollectionRepository
    from ..memory.models import RagCollection
    from ..memory.project_repository import ProjectRepository
    from ..rag.index_task_manager import get_index_task_manager
    from ..rag.qdrant_client import SharedQdrantClient
    from ..rag.config import get_rag_config

    # ── Access Control Helpers ────────────────────────────────────────────

    async def _get_user_or_401(request: Request) -> dict:
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user_info

    def _is_admin(user_info: dict) -> bool:
        return user_info.get("role") == "admin"

    async def _check_collection_access(session, user_info: dict, collection_id: UUID):
        """閲覧権限チェック。権限がなければ403を送出。"""
        if _is_admin(user_info):
            return
        has_access = await RagCollectionRepository.can_user_access_collection(
            session, UUID(user_info["id"]), collection_id
        )
        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")

    async def _check_collection_write(session, user_info: dict, collection_id: UUID):
        """書込み権限チェック。権限がなければ403を送出。"""
        if _is_admin(user_info):
            return
        has_write = await RagCollectionRepository.has_write_permission(
            session, UUID(user_info["id"]), collection_id
        )
        if not has_write:
            raise HTTPException(status_code=403, detail="Write permission required")

    async def _check_project_membership(session, user_info: dict, project_id: UUID):
        """プロジェクトメンバーシップチェック。メンバーでなければ403。"""
        if _is_admin(user_info):
            return
        member = await ProjectRepository.get_member(
            session, project_id, UUID(user_info["id"])
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a member of this project")

    def _require_admin(user_info: dict):
        """管理者限定。非管理者は403。"""
        if not _is_admin(user_info):
            raise HTTPException(status_code=403, detail="Admin privileges required")

    # ── Collection CRUD ──────────────────────────────────────────────────

    @router.post("/api/rag/collections")
    async def create_collection(
        payload: CreateRagCollectionPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                collection = await RagCollectionRepository.create_collection(
                    session,
                    created_by=UUID(user_info["id"]),
                    name=payload.name,
                    description=payload.description,
                    source_directory=payload.source_directory,
                    include_patterns=payload.include_patterns,
                    exclude_patterns=payload.exclude_patterns,
                )

                result = collection.to_dict()

                # Auto-start indexing if requested
                if payload.auto_index and payload.source_directory:
                    task_manager = get_index_task_manager()
                    task_manager.set_db_manager(db_manager)
                    await task_manager.start_indexing(
                        collection_id=str(collection.id),
                        collection_name=collection.collection_name,
                        source_directory=payload.source_directory,
                        include_patterns=payload.include_patterns or collection.include_patterns,
                        exclude_patterns=payload.exclude_patterns or collection.exclude_patterns,
                    )
                    result["indexing_started"] = True

                logger.info(f"RAG collection created: {collection.name} ({collection.collection_name})")
                return JSONResponse({"success": True, "collection": result})
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to create RAG collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/rag/collections")
    async def list_collections(
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                if _is_admin(user_info):
                    collections = await RagCollectionRepository.list_all(session)
                else:
                    collections = await RagCollectionRepository.list_accessible_collections(
                        session, UUID(user_info["id"])
                    )
                return JSONResponse({"success": True, "collections": collections})
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to list RAG collections: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Discovery & Import (admin only) ──────────────────────────────────
    # NOTE: These must be registered BEFORE /api/rag/collections/{collection_id}
    # to avoid FastAPI matching "discover"/"import" as a collection_id.

    @router.get("/api/rag/collections/discover")
    async def discover_qdrant_collections(
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Discover Qdrant collections not yet registered in DB."""
        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        try:
            # Get all Qdrant collections
            config = get_rag_config()
            qdrant_collections = SharedQdrantClient.list_all_collections(config.qdrant)

            # Get DB-registered collection names
            db_manager = get_db_manager()
            registered_names = set()
            if db_manager:
                session = await db_manager.get_session()
                try:
                    db_collections = await RagCollectionRepository.list_all(session)
                    registered_names = {c["collection_name"] for c in db_collections}
                finally:
                    await session.close()

            # Filter to unregistered
            unregistered = [
                c for c in qdrant_collections
                if c["name"] not in registered_names
            ]

            return JSONResponse({
                "success": True,
                "unregistered": unregistered,
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to discover collections: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/rag/collections/import")
    async def import_qdrant_collection(
        payload: ImportQdrantCollectionPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Import an existing Qdrant collection into DB registry."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        try:
            # Verify collection exists in Qdrant
            config = get_rag_config()
            qdrant_collections = SharedQdrantClient.list_all_collections(config.qdrant)
            qdrant_info = None
            for c in qdrant_collections:
                if c["name"] == payload.qdrant_name:
                    qdrant_info = c
                    break

            if not qdrant_info:
                raise HTTPException(
                    status_code=404,
                    detail=f"Qdrant collection '{payload.qdrant_name}' not found"
                )

            session = await db_manager.get_session()
            try:
                # Check not already registered
                existing = await RagCollectionRepository.get_by_collection_name(
                    session, payload.qdrant_name
                )
                if existing:
                    raise HTTPException(
                        status_code=409,
                        detail="Collection already registered"
                    )

                # Create DB record with the existing Qdrant collection name
                collection = RagCollection(
                    name=payload.display_name,
                    collection_name=payload.qdrant_name,
                    status="ready",
                    points_count=qdrant_info.get("points_count", 0),
                    created_by=UUID(user_info["id"]),
                )
                session.add(collection)
                await session.commit()
                await session.refresh(collection)

                logger.info(
                    f"Imported Qdrant collection '{payload.qdrant_name}' as '{payload.display_name}'"
                )
                return JSONResponse({
                    "success": True,
                    "collection": collection.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to import collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Collection by ID ──────────────────────────────────────────────

    @router.get("/api/rag/collections/{collection_id}")
    async def get_collection(
        collection_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                collection = await RagCollectionRepository.get_by_id(
                    session, UUID(collection_id)
                )
                if not collection:
                    raise HTTPException(status_code=404, detail="Collection not found")
                await _check_collection_access(session, user_info, UUID(collection_id))
                return JSONResponse({"success": True, "collection": collection.to_dict()})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get RAG collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.patch("/api/rag/collections/{collection_id}")
    async def update_collection(
        collection_id: str,
        payload: UpdateRagCollectionPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                await _check_collection_write(session, user_info, UUID(collection_id))
                collection = await RagCollectionRepository.update_collection(
                    session, UUID(collection_id),
                    name=payload.name, description=payload.description
                )
                if not collection:
                    raise HTTPException(status_code=404, detail="Collection not found")
                return JSONResponse({"success": True, "collection": collection.to_dict()})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update RAG collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/rag/collections/{collection_id}")
    async def delete_collection(
        collection_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                collection = await RagCollectionRepository.get_by_id(
                    session, UUID(collection_id)
                )
                if not collection:
                    raise HTTPException(status_code=404, detail="Collection not found")

                await _check_collection_write(session, user_info, UUID(collection_id))

                # Delete Qdrant collection
                try:
                    from ..rag.manager import get_rag_manager_for_collection
                    manager = get_rag_manager_for_collection(collection.collection_name)
                    if await manager.initialize():
                        await manager.clear_index()
                except Exception as e:
                    logger.warning(f"Failed to delete Qdrant collection: {e}")

                await RagCollectionRepository.delete_collection(session, UUID(collection_id))
                return JSONResponse({"success": True})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete RAG collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Indexing ─────────────────────────────────────────────────────────

    @router.post("/api/rag/collections/{collection_id}/index")
    async def start_indexing(
        collection_id: str,
        payload: StartIndexPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                await _check_collection_write(session, user_info, UUID(collection_id))

                collection = await RagCollectionRepository.get_by_id(
                    session, UUID(collection_id)
                )
                if not collection:
                    raise HTTPException(status_code=404, detail="Collection not found")
                if collection.status == "indexing":
                    raise HTTPException(status_code=409, detail="Indexing already in progress")
                if not collection.source_directory:
                    raise HTTPException(status_code=400, detail="No source directory configured")

                task_manager = get_index_task_manager()
                task_manager.set_db_manager(db_manager)
                task = await task_manager.start_indexing(
                    collection_id=str(collection.id),
                    collection_name=collection.collection_name,
                    source_directory=collection.source_directory,
                    clear_existing=payload.clear_existing,
                    include_patterns=collection.include_patterns,
                    exclude_patterns=collection.exclude_patterns,
                )

                if task is None:
                    raise HTTPException(status_code=409, detail="Indexing already in progress")

                return JSONResponse(
                    {"success": True, "message": "Indexing started", "task": task.to_dict()},
                    status_code=202
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to start indexing: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/rag/collections/{collection_id}/index/status")
    async def get_index_status(
        collection_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        user_info = await _get_user_or_401(request)

        # アクセス権チェック（DBセッションが必要）
        db_manager = get_db_manager()
        if db_manager:
            try:
                session = await db_manager.get_session()
                try:
                    await _check_collection_access(session, user_info, UUID(collection_id))
                finally:
                    await session.close()
            except HTTPException:
                raise
            except Exception:
                pass

        task_manager = get_index_task_manager()
        status = task_manager.get_task_status(collection_id)
        if status:
            return JSONResponse({"success": True, "task": status})

        # No active task – return DB status
        if db_manager:
            try:
                session = await db_manager.get_session()
                try:
                    collection = await RagCollectionRepository.get_by_id(
                        session, UUID(collection_id)
                    )
                    if collection:
                        return JSONResponse({
                            "success": True,
                            "task": {
                                "collection_id": collection_id,
                                "status": collection.status,
                                "points_count": collection.points_count or 0,
                            }
                        })
                finally:
                    await session.close()
            except Exception:
                pass

        return JSONResponse({"success": True, "task": None})

    # ── Project Linkage ──────────────────────────────────────────────────

    @router.post("/api/projects/{project_id}/rag-collections")
    async def link_collection_to_project(
        project_id: str,
        payload: LinkCollectionPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                await _check_project_membership(session, user_info, UUID(project_id))
                await _check_collection_access(session, user_info, UUID(payload.collection_id))

                link = await RagCollectionRepository.link_to_project(
                    session,
                    collection_id=UUID(payload.collection_id),
                    project_id=UUID(project_id),
                    linked_by=UUID(user_info["id"]),
                )
                if not link:
                    raise HTTPException(status_code=400, detail="Failed to link")
                return JSONResponse({"success": True, "link": link.to_dict()})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to link collection to project: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/projects/{project_id}/rag-collections")
    async def get_project_collections(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                await _check_project_membership(session, user_info, UUID(project_id))
                collections = await RagCollectionRepository.get_project_collections(
                    session, UUID(project_id)
                )
                return JSONResponse({"success": True, "collections": collections})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project collections: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/projects/{project_id}/rag-collections/{col_id}")
    async def unlink_collection_from_project(
        project_id: str,
        col_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)

        try:
            session = await db_manager.get_session()
            try:
                # プロジェクトのwrite権限が必要
                if not _is_admin(user_info):
                    has_perm = await ProjectRepository.has_permission(
                        session, UUID(project_id), UUID(user_info["id"]), "write"
                    )
                    if not has_perm:
                        raise HTTPException(status_code=403, detail="Write permission required")

                removed = await RagCollectionRepository.unlink_from_project(
                    session, UUID(col_id), UUID(project_id)
                )
                if not removed:
                    raise HTTPException(status_code=404, detail="Link not found")
                return JSONResponse({"success": True})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to unlink collection: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── User-Collection Direct Linkage (admin only) ──────────────────────

    @router.post("/api/rag/collections/{collection_id}/users")
    async def link_collection_to_user(
        collection_id: str,
        payload: LinkUserPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """コレクションをユーザーに直接紐付け（管理者限定）"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        if payload.permission not in ("read", "write"):
            raise HTTPException(status_code=400, detail="Permission must be 'read' or 'write'")

        try:
            session = await db_manager.get_session()
            try:
                # コレクション存在確認
                collection = await RagCollectionRepository.get_by_id(
                    session, UUID(collection_id)
                )
                if not collection:
                    raise HTTPException(status_code=404, detail="Collection not found")

                link = await RagCollectionRepository.link_to_user(
                    session,
                    collection_id=UUID(collection_id),
                    user_id=UUID(payload.user_id),
                    linked_by=UUID(user_info["id"]),
                    permission=payload.permission,
                )
                if not link:
                    raise HTTPException(status_code=400, detail="Failed to link")
                return JSONResponse({"success": True, "link": link.to_dict()})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to link collection to user: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/rag/collections/{collection_id}/users")
    async def get_collection_users(
        collection_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """コレクションに紐付いたユーザー一覧（管理者限定）"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        try:
            session = await db_manager.get_session()
            try:
                links = await RagCollectionRepository.get_collection_user_links(
                    session, UUID(collection_id)
                )
                return JSONResponse({"success": True, "links": links})
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get collection users: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/rag/collections/{collection_id}/users/{user_id}")
    async def unlink_collection_from_user(
        collection_id: str,
        user_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """コレクションとユーザーの紐付け解除（管理者限定）"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        try:
            session = await db_manager.get_session()
            try:
                removed = await RagCollectionRepository.unlink_from_user(
                    session, UUID(collection_id), UUID(user_id)
                )
                if not removed:
                    raise HTTPException(status_code=404, detail="Link not found")
                return JSONResponse({"success": True})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to unlink collection from user: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/users/{user_id}/rag-collections")
    async def get_user_rag_collections(
        user_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """ユーザーに直接紐付いたコレクション一覧（管理者限定）"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await _get_user_or_401(request)
        _require_admin(user_info)

        try:
            session = await db_manager.get_session()
            try:
                collections = await RagCollectionRepository.get_user_linked_collections(
                    session, UUID(user_id)
                )
                return JSONResponse({"success": True, "collections": collections})
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get user RAG collections: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
