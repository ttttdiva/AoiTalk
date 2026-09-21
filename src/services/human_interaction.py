"""Unified human-in-the-loop interaction transport for planning and questions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
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
    ws_type: str = ""
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

    def __init__(
        self,
        *,
        timeout_seconds: float = 600.0,
        agent_run_service: Any | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, HumanInteractionRequest] = {}
        self._broadcast_callback: BroadcastCallback | None = None
        self._terminalized_run_ids: set[str] = set()
        self._agent_run_service = agent_run_service

    def _resolve_agent_run_service(self) -> Any | None:
        if self._agent_run_service is not None:
            return self._agent_run_service
        try:
            # Lazy import avoids a module cycle and keeps standalone unit tests
            # (which have no configured database) lightweight.
            from .agent_run_service import AgentRunService

            self._agent_run_service = AgentRunService()
        except Exception:
            return None
        return self._agent_run_service

    @staticmethod
    def _safe_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Redact and bound interaction payloads before writing an audit event.

        Interaction payloads can contain provider-generated plan text, action
        arguments, or user responses.  Redaction is therefore performed before
        clipping and any failure omits the payload entirely; stringifying the
        raw value as a fallback would turn an audit event into a disclosure
        path.
        """

        try:
            # Import lazily to keep this transport usable in dependency-light
            # tests and to avoid introducing a module cycle at import time.
            from .outbound_privacy_service import redact_secret_for_local_display

            redacted = redact_secret_for_local_display(payload)

            def clip(value: Any, depth: int = 0) -> Any:
                # Leave enough room for the bounded material-action preview
                # envelope (payload -> preview -> actions -> action -> args)
                # while still preventing arbitrarily deep provider objects.
                if depth > 8:
                    return "[truncated]"
                if isinstance(value, Mapping):
                    output: dict[str, Any] = {}
                    for key, item in list(value.items())[:64]:
                        safe_key = redact_secret_for_local_display(str(key))
                        if not isinstance(safe_key, str):
                            raise ValueError("interaction payload key redaction failed")
                        output[safe_key] = clip(item, depth + 1)
                    return output
                if isinstance(value, (list, tuple)):
                    return [clip(item, depth + 1) for item in list(value)[:64]]
                if isinstance(value, str):
                    return value if len(value) <= 4000 else value[:4000] + "..."
                if value is None or isinstance(value, (bool, int, float)):
                    return value
                # Unknown provider objects are not JSON-safe and their repr can
                # contain credentials.  Omit them instead of stringifying.
                return "[omitted]"

            safe = clip(redacted)
            if not isinstance(safe, dict):
                raise ValueError("interaction payload redaction returned non-object")
            return safe
        except Exception:
            # Never include exception text: provider adapters may embed the raw
            # payload in it.  This stable marker is the only safe audit value.
            return {
                "payload_omitted": True,
                "redaction_error": "redaction_failed",
            }

    async def _persist_interaction_event(
        self,
        request: HumanInteractionRequest,
        event_type: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        run_id = str(request.agent_run_id or "").strip()
        if not run_id:
            return
        service = self._resolve_agent_run_service()
        if service is None:
            return
        payload: dict[str, Any] = {
            "pending_interaction_id": request.request_id,
            "interaction_kind": request.kind.value,
            "revision": int(request.revision or 0),
            "session_id": request.session_id,
            "user_id": request.user_id,
        }
        if event_type == "interaction.requested":
            payload["request"] = self._safe_event_payload(request.payload)
        if result is not None:
            payload["resolution"] = self._safe_event_payload(result)
        try:
            await service.record_event(
                run_id,
                event_type,
                status=status,
                payload=payload,
            )
        except Exception as exc:
            logger.debug("Interaction audit event skipped (%s): %s", event_type, exc)

    def _schedule_interaction_event(
        self,
        request: HumanInteractionRequest,
        event_type: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        try:
            loop = request.loop or asyncio.get_running_loop()
            if loop.is_closed() or not loop.is_running():
                return
            loop.create_task(
                self._persist_interaction_event(
                    request,
                    event_type,
                    status=status,
                    result=result,
                )
            )
        except Exception:
            return

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
                self._schedule_interaction_event(
                    request,
                    "interaction.cancelled",
                    status="cancelled",
                    result={"cancelled": True},
                )
                self._pending.pop(request_id, None)

    def clear_terminalized_run(self, agent_run_id: str) -> None:
        self._terminalized_run_ids.discard(str(agent_run_id or "").strip())

    @classmethod
    def _request_envelope(
        cls,
        request: HumanInteractionRequest,
    ) -> dict[str, Any]:
        """Build the canonical initial/replay websocket envelope."""

        revision = int(request.revision or 0)
        event_id = f"human-interaction:{request.request_id}:{revision}"
        # Redact before either initial delivery or replay.  Planning callers
        # already provide a display projection, but keeping this transport
        # boundary defensive protects generic human interactions too.
        data = cls._safe_event_payload(request.payload)
        # Correlation and scope fields are server-owned.  Apply them after the
        # provider payload so a payload cannot retarget or replace a pending
        # interaction during either initial delivery or replay.
        data.update(
            {
                "request_id": request.request_id,
                "pending_interaction_id": request.request_id,
                "kind": request.kind.value,
                "interaction_kind": request.kind.value,
                "agent_run_id": request.agent_run_id,
                "session_id": request.session_id,
                "revision": revision,
                "event_id": event_id,
            }
        )
        return {
            "type": request.ws_type or cls._default_ws_type(request.kind),
            "event_id": event_id,
            "data": data,
        }

    async def select_pending_replays(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Select live pending interactions for one authenticated socket scope.

        ``_pending`` remains the only actionable source.  Durable audit events
        are intentionally not consulted because they cannot recreate the
        in-memory Future that owns response consumption.
        """

        scoped_user_id = str(user_id or "").strip()
        scoped_session_id = str(session_id or "").strip()
        if not scoped_user_id or not scoped_session_id:
            return []

        service = self._resolve_agent_run_service()
        if service is None:
            return []

        selected: list[dict[str, Any]] = []
        for request_id, request in list(self._pending.items()):
            run_id = str(request.agent_run_id or "").strip()
            if (
                request.status != HumanInteractionStatus.PENDING
                or request.user_id != scoped_user_id
                or request.session_id != scoped_session_id
                or not run_id
                or run_id in self._terminalized_run_ids
            ):
                continue
            try:
                run = await service.get_run(run_id)
            except Exception as exc:
                logger.debug(
                    "[HumanInteraction] Pending replay run lookup failed (%s): %s",
                    run_id,
                    exc,
                )
                continue
            run_status = (
                str(run.get("status") or "").strip()
                if isinstance(run, dict)
                else ""
            )
            if not run_status or run_status in {"succeeded", "failed", "cancelled"}:
                continue
            # A response or terminalization may have raced the durable lookup.
            # Recheck the authoritative in-memory entry before exposing it.
            if (
                self._pending.get(request_id) is not request
                or request.status != HumanInteractionStatus.PENDING
                or run_id in self._terminalized_run_ids
            ):
                continue
            selected.append(self._request_envelope(request))
        return selected

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
            ws_type=ws_type or self._default_ws_type(kind),
            future=future,
            loop=loop,
        )
        self._pending[request_id] = request

        # Persist the request before broadcasting it.  A websocket disconnect
        # can therefore still be audited and reconciled after process restart.
        await self._persist_interaction_event(
            request,
            "interaction.requested",
            status="pending",
        )

        try:
            await self._broadcast_callback(self._request_envelope(request))
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            request.status = HumanInteractionStatus.TIMEOUT
            logger.warning("[HumanInteraction] Timed out: %s", request_id)
            await self._persist_interaction_event(
                request,
                "interaction.timeout",
                status="timeout",
            )
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
        self._schedule_interaction_event(
            request,
            "interaction.resolution",
            status="resolved",
            result=result,
        )
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
                    "pending_interaction_id": request.request_id,
                    "interaction_kind": request.kind.value,
                    "revision": request.revision,
                    # Keep this inspection surface on the same redacted side
                    # as websocket replay and interaction audit events.
                    "payload": self._safe_event_payload(request.payload),
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
