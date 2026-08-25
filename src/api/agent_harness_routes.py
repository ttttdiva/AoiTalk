"""Agent harness status and manual tick API routes."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..agent_harness.config import (
    AgentHarnessConfigError,
    AgentHarnessSettings,
    public_agent_harness_settings,
    strip_agent_harness_secret_keys,
    validate_agent_harness_update,
)
from ..agent_harness.orchestrator import AgentHarnessOrchestrator
from ..agent_harness.runner import build_runner
from ..agent_harness.tracker import BuiltInTaskTrackerAdapter
from ..agent_harness.workflow import load_harness_workflow
from ..agent_harness.workspace import WorkspaceManager
from ..services.outbound_privacy_service import current_effective_privacy_mode

logger = logging.getLogger(__name__)


def create_agent_harness_router(
    *,
    require_auth_dependency,
    config: Any,
    get_db_manager,
    is_admin_user=None,
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
        if current_effective_privacy_mode(config) != "direct":
            logger.info("Agent harness disabled by outbound privacy policy")
            return
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

    async def require_admin(request: Request) -> None:
        """Require the server's real admin check for mutating/execution APIs."""

        if is_admin_user is None:
            # The production server always supplies ``WebChatServer``'s
            # database-backed callback.  A missing callback is a wiring error,
            # not an implicit authorization grant.
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        try:
            allowed = is_admin_user(request)
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:
            logger.exception("Agent Harness admin check failed")
            allowed = False
        if not bool(allowed):
            raise HTTPException(status_code=403, detail="Administrator privileges required")

    @router.get("/state")
    async def get_state(request: Request, _=Depends(require_auth_dependency)):
        """Return current in-memory harness state without starting new runs."""
        try:
            orch = await get_orchestrator()
            return JSONResponse(content={"success": True, "state": orch.snapshot()})
        except Exception as exc:
            logger.exception("Agent harness state failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/config")
    async def get_config(request: Request, _=Depends(require_auth_dependency)):
        """Return only independent autonomous-task settings (never Team routes)."""
        raw = config.get("agent_harness", {}) if hasattr(config, "get") else {}
        return JSONResponse(content={"success": True, "independent": True, "settings": public_agent_harness_settings(raw)})

    @router.put("/config")
    async def update_config(request: Request, _=Depends(require_auth_dependency)):
        await require_admin(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON payload is required") from exc
        try:
            settings = validate_agent_harness_update(payload)
        except AgentHarnessConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        current = config.get("agent_harness", {}) if hasattr(config, "get") else {}
        if not isinstance(current, dict):
            current = {}
        # Merge only the already-validated branches.  ``dict.update`` on an
        # arbitrary nested payload was the original RCE primitive (hooks,
        # custom_command, executable paths, and workspace roots all survived).
        merged = strip_agent_harness_secret_keys(copy.deepcopy(current))
        # A pre-existing hand-edited/legacy file may already contain these
        # deployment-owned branches.  Once an administrator uses the public
        # endpoint, remove them instead of carrying an unsafe value forward
        # while merely toggling ``enabled``.
        requested_runner = str((settings.get("codex") or {}).get("runner") or "").strip().lower() if isinstance(settings.get("codex"), dict) else ""
        for deployment_key in (
            "hooks",
            "workflow_file",
            "workspace_root",
            "workspace_base_ref",
            "workspace_branch_prefix",
        ):
            merged.pop(deployment_key, None)
        # The command itself is deployment-owned and cannot be written by this
        # endpoint.  Preserve a preconfigured, runtime-sanitized command only
        # when the administrator explicitly keeps the legacy runner selected;
        # selecting any other runner removes the stale command branch.
        if requested_runner != "custom_command":
            merged.pop("custom_command", None)
        for cli_key in ("codex", "claude"):
            if isinstance(merged.get(cli_key), dict):
                merged[cli_key] = copy.deepcopy(merged[cli_key])
                merged[cli_key].pop("bin_path", None)
        if isinstance(merged.get("codex"), dict) and merged["codex"].get("runner") == "custom_command":
            merged["codex"]["runner"] = "codex_exec"
        for key, value in settings.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                branch = copy.deepcopy(merged[key])
                branch.update(copy.deepcopy(value))
                merged[key] = branch
            else:
                merged[key] = copy.deepcopy(value)
        if hasattr(config, "save_to_file"):
            if not config.save_to_file("agent_harness", merged):
                raise HTTPException(status_code=500, detail="Failed to persist Agent Harness settings")
        elif hasattr(config, "set"):
            config.set("agent_harness", merged)
        return JSONResponse(content={"success": True, "independent": True, "settings": public_agent_harness_settings(merged)})

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
            await require_admin(request)
            if current_effective_privacy_mode(config) != "direct":
                raise HTTPException(
                    status_code=409,
                    detail="保護クラウド / ローカル限定モードでは外部CLI Agent Harnessを使用できません。",
                )
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
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Agent harness refresh failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
