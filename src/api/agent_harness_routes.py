"""Agent harness status and manual tick API routes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..agent_harness.config import AgentHarnessSettings
from ..agent_harness.orchestrator import AgentHarnessOrchestrator
from ..agent_harness.runner import build_runner
from ..agent_harness.tracker import BuiltInTaskTrackerAdapter
from ..agent_harness.workflow import load_harness_workflow
from ..agent_harness.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


def create_agent_harness_router(
    *,
    require_auth_dependency,
    config: Any,
    get_db_manager,
) -> APIRouter:
    """Create a small API surface for the disabled-by-default harness."""

    router = APIRouter(prefix="/api/agent-harness", tags=["agent-harness"])
    orchestrator: AgentHarnessOrchestrator | None = None
    orchestrator_lock = asyncio.Lock()
    tick_lock = asyncio.Lock()
    background_task: asyncio.Task | None = None

    async def get_orchestrator() -> AgentHarnessOrchestrator:
        nonlocal orchestrator
        if orchestrator is not None:
            return orchestrator
        async with orchestrator_lock:
            if orchestrator is not None:
                return orchestrator
            repo_root = Path(__file__).resolve().parents[2]
            settings = AgentHarnessSettings.from_config(config, root_dir=repo_root)
            workflow = load_harness_workflow(settings.workflow_file)
            orchestrator = AgentHarnessOrchestrator(
                settings=settings,
                tracker=BuiltInTaskTrackerAdapter(get_db_manager, settings),
                runner=build_runner(settings),
                workspace_manager=WorkspaceManager(
                    settings.workspace_root,
                    settings.hooks,
                    repo_root=repo_root,
                    base_ref=settings.workspace_base_ref,
                    branch_prefix=settings.workspace_branch_prefix,
                ),
                workflow=workflow,
            )
            return orchestrator

    async def run_polling_loop() -> None:
        while True:
            orch = await get_orchestrator()
            if not orch.settings.auto_start:
                return
            try:
                async with tick_lock:
                    await orch.tick()
            except Exception:
                logger.exception("Agent harness polling tick failed")
            await asyncio.sleep(max(0.001, orch.settings.polling_interval_ms / 1000))

    async def start_auto_polling() -> None:
        nonlocal background_task
        orch = await get_orchestrator()
        if orch.settings.auto_start:
            background_task = asyncio.create_task(run_polling_loop())

    async def stop_auto_polling() -> None:
        if background_task is not None:
            background_task.cancel()
            try:
                await background_task
            except asyncio.CancelledError:
                pass

    router.agent_harness_start = start_auto_polling  # type: ignore[attr-defined]
    router.agent_harness_stop = stop_auto_polling  # type: ignore[attr-defined]

    @router.get("/state")
    async def get_state(request: Request, _=Depends(require_auth_dependency)):
        """Return current in-memory harness state without starting new runs."""
        try:
            orch = await get_orchestrator()
            return JSONResponse(content={"success": True, "state": orch.snapshot()})
        except Exception as exc:
            logger.exception("Agent harness state failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/runs/{identifier}")
    async def get_run(identifier: str, request: Request, _=Depends(require_auth_dependency)):
        """Return detail for a running or retrying work item."""
        try:
            orch = await get_orchestrator()
            detail = orch.run_detail(identifier)
            if detail is None:
                raise HTTPException(status_code=404, detail="Run not found")
            return JSONResponse(content={"success": True, "run": detail})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Agent harness run detail failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/refresh")
    async def refresh(request: Request, _=Depends(require_auth_dependency)):
        """Run one reconciliation/dispatch tick."""
        try:
            orch = await get_orchestrator()
            async with tick_lock:
                state = await orch.tick()
            return JSONResponse(
                content={
                    "success": True,
                    "enabled": orch.settings.enabled,
                    "dispatched": True,
                    "state": state,
                }
            )
        except Exception as exc:
            logger.exception("Agent harness refresh failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
