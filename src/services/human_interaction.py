"""Unified human-in-the-loop interaction transport for planning and questions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from ..tools.external_llm_permission import get_permission_request_scope

logger = logging.getLogger(__name__)

BroadcastCallback = Callable[[dict[str, Any]], Awaitable[None]]


class HumanInteractionKind(str, Enum):
    ASK_USER_QUESTION = "ask_user_question"
    PLAN_APPROVAL = "plan_approval"
    TOOL_PERMISSION = "tool_permission"
    EXTERNAL_PROMPT = "external_prompt"


class HumanInteractionStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class HumanInteractionRequest:
    request_id: str
    kind: HumanInteractionKind
    payload: dict[str, Any]
    agent_run_id: str = ""
    session_id: str = ""
    user_id: str = ""
    revision: int = 0
    status: HumanInteractionStatus = HumanInteractionStatus.PENDING
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)


_human_interaction_manager: "HumanInteractionManager | None" = None


def get_human_interaction_manager() -> "HumanInteractionManager | None":
    return _human_interaction_manager


def set_human_interaction_manager(manager: "HumanInteractionManager") -> None:
    global _human_interaction_manager
    _human_interaction_manager = manager


class HumanInteractionManager:
    """Pending request/response transport shared by planning interactions."""

    def __init__(self, *, timeout_seconds: float = 600.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, HumanInteractionRequest] = {}
        self._broadcast_callback: BroadcastCallback | None = None
        self._terminalized_run_ids: set[str] = set()

    def set_broadcast_callback(self, callback: BroadcastCallback | None) -> None:
        self._broadcast_callback = callback

    def terminalize_run(self, agent_run_id: str) -> None:
        run_id = str(agent_run_id or "").strip()
        if not run_id:
            return
        self._terminalized_run_ids.add(run_id)
        for request_id, request in list(self._pending.items()):
            if request.agent_run_id == run_id:
                self._resolve_request(
                    request,
                    result={"cancelled": True},
                    status=HumanInteractionStatus.CANCELLED,
                )
                self._pending.pop(request_id, None)

    def clear_terminalized_run(self, agent_run_id: str) -> None:
        self._terminalized_run_ids.discard(str(agent_run_id or "").strip())

    async def request_interaction(
        self,
        *,
        kind: HumanInteractionKind,
        payload: dict[str, Any],
        agent_run_id: str = "",
        session_id: str = "",
        user_id: str = "",
        revision: int = 0,
        ws_type: str | None = None,
    ) -> Any:
        if self._broadcast_callback is None:
            logger.warning("[HumanInteraction] No broadcast callback; denying")
            return None

        run_id = str(agent_run_id or "").strip()
        if run_id and run_id in self._terminalized_run_ids:
            logger.warning("[HumanInteraction] Run already terminalized: %s", run_id)
            return None

        scope_user_id, scope_session_id = get_permission_request_scope()
        effective_user_id = user_id or scope_user_id or ""
        effective_session_id = session_id or scope_session_id or ""
        if scope_session_id and session_id and session_id != scope_session_id:
            logger.warning("[HumanInteraction] Cross-session request rejected")
            return None

        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        request = HumanInteractionRequest(
            request_id=request_id,
            kind=kind,
            payload=dict(payload),
            agent_run_id=run_id,
            session_id=effective_session_id,
            user_id=effective_user_id,
            revision=revision,
            future=future,
            loop=loop,
        )
        self._pending[request_id] = request

        message_type = ws_type or self._default_ws_type(kind)
        try:
            await self._broadcast_callback(
                {
                    "type": message_type,
                    "data": {
                        "request_id": request_id,
                        "interaction_kind": kind.value,
                        "agent_run_id": run_id,
                        "session_id": effective_session_id,
                        "revision": revision,
                        **payload,
                    },
                }
            )
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            request.status = HumanInteractionStatus.TIMEOUT
            logger.warning("[HumanInteraction] Timed out: %s", request_id)
            return None
        finally:
            self._pending.pop(request_id, None)

    def handle_response(
        self,
        request_id: str,
        result: dict[str, Any],
        *,
        requester_user_id: str | None = None,
        requester_session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        request = self._pending.get(request_id)
        if request is None:
            logger.warning("[HumanInteraction] Unknown request: %s", request_id)
            return False
        if request.status != HumanInteractionStatus.PENDING:
            logger.warning("[HumanInteraction] Stale response for %s", request_id)
            return False
        if request.user_id and request.user_id != str(requester_user_id or ""):
            logger.warning("[HumanInteraction] User mismatch for %s", request_id)
            return False
        if request.session_id and request.session_id != str(requester_session_id or ""):
            logger.warning("[HumanInteraction] Session mismatch for %s", request_id)
            return False
        if expected_revision is not None and request.revision != expected_revision:
            logger.warning("[HumanInteraction] Revision mismatch for %s", request_id)
            return False
        if request.agent_run_id and request.agent_run_id in self._terminalized_run_ids:
            logger.warning("[HumanInteraction] Run terminalized for %s", request_id)
            return False
        self._resolve_request(request, result=result, status=HumanInteractionStatus.RESOLVED)
        self._pending.pop(request_id, None)
        return True

    def get_pending_for_run(self, agent_run_id: str) -> list[dict[str, Any]]:
        run_id = str(agent_run_id or "").strip()
        pending: list[dict[str, Any]] = []
        for request in self._pending.values():
            if request.agent_run_id != run_id:
                continue
            pending.append(
                {
                    "request_id": request.request_id,
                    "interaction_kind": request.kind.value,
                    "revision": request.revision,
                    "payload": dict(request.payload),
                }
            )
        return pending

    @staticmethod
    def _default_ws_type(kind: HumanInteractionKind) -> str:
        if kind == HumanInteractionKind.ASK_USER_QUESTION:
            return "ask_user_question_request"
        if kind == HumanInteractionKind.PLAN_APPROVAL:
            return "plan_approval_request"
        return "human_interaction_request"

    @staticmethod
    def _resolve_request(
        request: HumanInteractionRequest,
        *,
        result: dict[str, Any],
        status: HumanInteractionStatus,
    ) -> None:
        request.status = status
        if request.future is None or request.loop is None:
            return
        if request.future.done():
            return

        def _set_result() -> None:
            if not request.future.done():
                request.future.set_result(result)

        request.loop.call_soon_threadsafe(_set_result)
