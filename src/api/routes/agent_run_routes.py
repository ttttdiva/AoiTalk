"""API routes for durable agent run tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...memory.database import get_database_manager
from ...memory.models import ConversationSession
from ...memory.project_repository import ProjectRepository
from ...services.agent_run_service import AgentRunService
from ...services.project_context import has_project_read_access
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

    async def _assert_session_access(session_id: str | None, user_info: dict) -> None:
        if not session_id:
            return
        user_id = str(user_info.get("id") or "")
        # ``_websocket_session_allowed`` deliberately keeps the admin override
        # explicit.  Do not infer it from membership: global admins may inspect
        # a session without being listed as a participant.
        checker_kwargs = {}
        if str(user_info.get("role") or "") == "admin":
            checker_kwargs["is_admin"] = True
        if not await server._websocket_session_allowed(
            session_id,
            user_id,
            **checker_kwargs,
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        # The checker intentionally short-circuits for admins.  Re-resolve the
        # session in that case so an arbitrary/nonexistent id still fails
        # closed rather than producing an empty successful listing.
        if checker_kwargs.get("is_admin") is True:
            await _session_project_id(session_id)

    async def _session_project_id(session_id: str) -> str | None:
        """Load the live project binding for a session, failing closed.

        AgentRun stores ``session_id`` and ``project_id`` independently for
        backwards compatibility.  The route must not trust a malformed or
        stale row: if the session cannot be resolved, callers receive the
        same generic denial used by the session ACL check.
        """
        try:
            session_uuid = UUID(str(session_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail="Access denied") from exc

        db_session = None
        try:
            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            conversation = await db_session.get(ConversationSession, session_uuid)
        except Exception as exc:
            # An ACL/binding lookup failure must never turn into a data leak.
            raise HTTPException(status_code=403, detail="Access denied") from exc
        finally:
            if db_session is not None:
                try:
                    await db_session.close()
                except Exception:
                    # The authorization decision is already fail-closed if
                    # loading failed; a close error must not replace it with
                    # an accidental 500 response.
                    pass

        if conversation is None or getattr(conversation, "deleted_at", None) is not None:
            raise HTTPException(status_code=403, detail="Access denied")
        project_id = getattr(conversation, "project_id", None)
        return str(project_id) if project_id else None

    async def _assert_run_binding(
        run: dict,
        *,
        expected_session_id: str | None = None,
        expected_project_id: str | None = None,
    ) -> None:
        """Ensure persisted run/session/project scope cannot cross projects."""
        run_session_id = str(run.get("session_id") or "").strip() or None
        run_project_id = str(run.get("project_id") or "").strip() or None

        if expected_session_id is not None and run_session_id != str(expected_session_id):
            raise HTTPException(status_code=403, detail="Access denied")
        if expected_project_id is not None and run_project_id != str(expected_project_id):
            raise HTTPException(status_code=403, detail="Access denied")

        if run_session_id is None:
            return

        session_project_id = await _session_project_id(run_session_id)
        # A project-bound run must belong to the same project as its session;
        # likewise, an explicitly unscoped run cannot be attached to a
        # project-bound session.  This protects both GET and list endpoints
        # even if an old/corrupt AgentRun row bypassed creation-time checks.
        if session_project_id != run_project_id:
            raise HTTPException(status_code=403, detail="Access denied")

    async def _assert_project_access(project_id: str, user_info: dict) -> None:
        db_manager = get_database_manager()
        session = await db_manager.get_session()
        try:
            project_uuid = UUID(project_id)
            project = await ProjectRepository.get_by_id(session, project_uuid)
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            if not await has_project_read_access(
                session,
                project,
                user_id=str(user_info.get("id") or ""),
                user_role=str(user_info.get("role") or "") or None,
            ):
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
        await _assert_session_access(run.get("session_id"), user_info)
        if run.get("project_id"):
            await _assert_project_access(str(run["project_id"]), user_info)
        elif not run.get("session_id"):
            if (
                str(user_info.get("role") or "") != "admin"
                and str(run.get("user_id") or "") != str(user_info["id"])
            ):
                raise HTTPException(status_code=403, detail="Access denied")
        await _assert_run_binding(run)
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
        await _assert_session_access(session_id, user_info)
        runs = await AgentRunService().list_runs(
            session_id=session_id,
            status=status,
            limit=limit,
        )
        for run in runs:
            await _assert_run_binding(run, expected_session_id=session_id)
            if run.get("project_id"):
                await _assert_project_access(str(run["project_id"]), user_info)
            elif not run.get("session_id") and (
                str(user_info.get("role") or "") != "admin"
                and str(run.get("user_id") or "") != str(user_info["id"])
            ):
                raise HTTPException(status_code=403, detail="Access denied")
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
        for run in runs:
            await _assert_run_binding(run, expected_project_id=project_id)
        return JSONResponse(
            {
                "success": True,
                "project_id": project_id,
                "agent_runs": runs,
            }
        )
