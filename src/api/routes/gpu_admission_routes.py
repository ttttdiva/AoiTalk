"""Local-admin lease surface for cooperative GPU admission.

Only the lease lifecycle is exposed: acquire (idempotent per caller operation id),
launch of the configured trial runtime under a held lease, cancel, and an
observational status. Process manifests, release and cleanup evidence are
internal driver/adapter calls and are deliberately not reachable here: an API
caller can neither supply proof nor name a process or a profile. Without a
trial backend installed no lease can be granted (503), so these routes cannot
pause indexing on their own. They are operator tooling and stay out of the
shared OpenAPI contract, so neither Web nor Mobile types change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...services.gpu_admission import MAX_LEASE_TTL_SECONDS, Conflict
from ...services.gpu_admission_driver import BackendError
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer


class GpuLeaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: float = Field(gt=0, le=MAX_LEASE_TTL_SECONDS, allow_inf_nan=False)


class GpuLeaseCancelRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=1, max_length=256)
    boot: str = Field(min_length=1, max_length=128)


class GpuLeaseLaunchRequest(GpuLeaseCancelRequest):
    """Same fence as a cancel; ``operation_id`` identifies this launch for idempotent replays."""


def register_gpu_admission_routes(app: FastAPI, server: "WebChatServer") -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def require_admin(request: Request, _: None = Depends(require_auth)) -> None:
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Admin only")

    def driver():
        found = getattr(server, "_gpu_admission_driver", None)
        if found is None:
            raise HTTPException(status_code=503, detail="gpu_admission_unavailable")
        return found

    @app.get("/api/runtime/gpu-admission", include_in_schema=False)
    async def get_gpu_admission_status(response: Response, _: None = Depends(require_admin)):
        response.headers["Cache-Control"] = "no-store"
        snapshot = driver().snapshot()
        launcher = getattr(server, "_gpu_trial_launcher", None)
        # Observational only. The reason says *why* no backend is installed, so an absent
        # backend is never read as "configured and idle".
        snapshot["launch"] = launcher.status() if launcher is not None else None
        snapshot["backend_unavailable_reason"] = (
            None if snapshot["backend_configured"] else getattr(server, "_gpu_admission_unavailable_reason", None))
        return snapshot

    @app.post("/api/runtime/gpu-admission/lease", include_in_schema=False)
    async def acquire_gpu_lease(
        payload: GpuLeaseRequest, response: Response, _: None = Depends(require_admin),
    ):
        response.headers["Cache-Control"] = "no-store"
        current = driver()
        if not current.backend_configured:
            raise HTTPException(status_code=503, detail="gpu_lease_backend_unavailable")
        if current.settling and not current.coordinator.knows_operation(payload.owner, payload.operation_id):
            # A backend call of the previous lease is still running: a late durable write must
            # never land in the next lease's state. Replays of an answered acquire still work.
            raise HTTPException(status_code=409, detail="gpu_driver_settling")
        try:
            receipt = current.coordinator.acquire(payload.owner, payload.operation_id, payload.ttl_seconds)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_lease")
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        # The token is returned only to the caller holding this operation id.
        return {"owner": receipt["owner"], "operation_id": receipt["operation_id"],
                "ttl_seconds": receipt["ttl"], "token": receipt["token"], "boot": receipt["boot"],
                "active": receipt["active"]}

    @app.post("/api/runtime/gpu-admission/lease/launch", include_in_schema=False)
    async def launch_gpu_trial(
        payload: GpuLeaseLaunchRequest, response: Response, _: None = Depends(require_admin),
    ):
        """Start the configured trial runtime under the caller's held lease (idempotent per operation id).

        The caller names nothing to launch: the profile is deployment configuration, and the
        lease's owner/token/boot fence decides whether the launch may start at all."""
        response.headers["Cache-Control"] = "no-store"
        launcher = getattr(server, "_gpu_trial_launcher", None)
        if launcher is None:
            raise HTTPException(status_code=503, detail="gpu_trial_launcher_unavailable")
        try:
            return await launcher.launch(payload.owner, payload.token, payload.boot, payload.operation_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_launch")
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except BackendError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/runtime/gpu-admission/lease/cancel", include_in_schema=False)
    async def cancel_gpu_lease(
        payload: GpuLeaseCancelRequest, response: Response, _: None = Depends(require_admin),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return driver().coordinator.cancel(
                payload.owner, payload.operation_id, payload.token, payload.boot)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_cancel")
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
