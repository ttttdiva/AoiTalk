"""API routes for durable agent run tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...memory.database import get_database_manager
from ...memory.project_repository import ProjectRepository
from ...services.agent_run_service import AgentRunService
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer


def register_agent_run_routes(app: FastAPI, server: "WebChatServer") -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def _current_user(request: Request) -> dict:
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user_info

    async def _assert_session_access(session_id: str | None, user_id: str) -> None:
        if not session_id:
            return
        if not await server._websocket_session_allowed(session_id, user_id):
            raise HTTPException(status_code=403, detail="Access denied")

    async def _assert_project_access(project_id: str, user_info: dict) -> None:
        if str(user_info.get("role") or "") == "admin":
            return
        db_manager = get_database_manager()
        session = await db_manager.get_session()
        try:
            member = await ProjectRepository.get_member(
                session,
                project_id=UUID(project_id),
                user_id=UUID(str(user_info["id"])),
            )
            if member is None:
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid project id") from exc
        finally:
            await session.close()

    @app.get("/api/agent-runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        request: Request,
        include_events: bool = Query(True),
        include_tool_calls: bool = Query(True),
        include_edges: bool = Query(False),
        include_timeline: bool = Query(True),
        _: None = Depends(require_auth),
    ):
        user_info = await _current_user(request)
        service = AgentRunService()
        run = await service.get_run(
            run_id,
            include_events=include_events,
            include_tool_calls=include_tool_calls,
            include_edges=include_edges,
            include_timeline=include_timeline,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Agent run not found")
        await _assert_session_access(run.get("session_id"), str(user_info["id"]))
        if run.get("project_id") and not run.get("session_id"):
            await _assert_project_access(str(run["project_id"]), user_info)
        return JSONResponse({"success": True, "agent_run": run})

    @app.get("/api/conversations/{session_id}/agent-runs")
    async def list_conversation_agent_runs(
        session_id: str,
        request: Request,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        _: None = Depends(require_auth),
    ):
        user_info = await _current_user(request)
        await _assert_session_access(session_id, str(user_info["id"]))
        runs = await AgentRunService().list_runs(
            session_id=session_id,
            status=status,
            limit=limit,
        )
        return JSONResponse(
            {
                "success": True,
                "session_id": session_id,
                "agent_runs": runs,
            }
        )

    @app.get("/api/projects/{project_id}/agent-runs")
    async def list_project_agent_runs(
        project_id: str,
        request: Request,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        _: None = Depends(require_auth),
    ):
        user_info = await _current_user(request)
        await _assert_project_access(project_id, user_info)
        runs = await AgentRunService().list_runs(
            project_id=project_id,
            status=status,
            limit=limit,
        )
        return JSONResponse(
            {
                "success": True,
                "project_id": project_id,
                "agent_runs": runs,
            }
        )
