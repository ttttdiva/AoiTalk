"""Observe and release the shared private embedding worker without loading it."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..router_helpers import (
    await_task_completion_before_cancellation,
    cookie_auth_dependency,
)

if TYPE_CHECKING:
    from ..server import WebChatServer


class EmbeddingRuntimeStatus(BaseModel):
    state: Literal["unloaded", "loading", "loaded", "busy", "releasing", "failed", "closed"]
    loaded: bool
    process_running: bool
    pid: int | None
    active_requests: int
    queued_requests: int
    pending_requests: int
    configured_device: str
    effective_device: str | None
    precision: str | None
    fingerprint: str
    dimension: int
    idle_timeout_seconds: float
    idle_seconds: float | None
    metrics: dict[str, Any]
    metrics_source: Literal["last_worker_response"] | None
    last_release_reason: str | None
    last_error: str | None


class EmbeddingRuntimeView(BaseModel):
    # None means no wrapper has been created; observation must not create one.
    status: EmbeddingRuntimeStatus | None


class EmbeddingRuntimeReleaseResult(EmbeddingRuntimeView):
    accepted: bool
    released: bool
    reason: Literal["manual", "not_loaded", "busy", "closed", "error"]


def _peek_runtime():
    # Even importing the RAG package can import heavyweight optional modules.
    # A runtime cannot exist before its module does, so this read stays passive.
    module = sys.modules.get("src.rag.embedding")
    return module.peek_embedding_runtime() if module is not None else None


def register_embedding_runtime_routes(app: FastAPI, server: "WebChatServer") -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def require_admin(request: Request, _: None = Depends(require_auth)) -> None:
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Admin only")

    @app.get("/api/runtime/embedding", response_model=EmbeddingRuntimeView)
    async def get_embedding_runtime(
        response: Response, _: None = Depends(require_admin),
    ):
        response.headers["Cache-Control"] = "no-store"
        runtime = _peek_runtime()
        return {"status": runtime.get_status() if runtime is not None else None}

    @app.post(
        "/api/runtime/embedding/release",
        response_model=EmbeddingRuntimeReleaseResult,
        responses={
            409: {"model": EmbeddingRuntimeReleaseResult},
            503: {"model": EmbeddingRuntimeReleaseResult},
        },
    )
    async def release_embedding_runtime(
        response: Response, _: None = Depends(require_admin),
    ):
        response.headers["Cache-Control"] = "no-store"
        runtime = _peek_runtime()
        result = (
            await await_task_completion_before_cancellation(runtime.release())
            if runtime is not None
            else {"released": False, "reason": "not_loaded", "status": None}
        )
        accepted = result["reason"] in {"manual", "not_loaded"}
        if result["reason"] in {"busy", "closed"}:
            response.status_code = 409
        elif result["reason"] == "error":
            response.status_code = 503
        return {**result, "accepted": accepted}
