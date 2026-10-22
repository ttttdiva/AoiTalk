"""Unified human-in-the-loop interaction transport for planning and questions."""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from ..tools.external_llm_permission import get_permission_request_scope

logger = logging.getLogger(__name__)

BroadcastCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _copy_json_value(value: Any) -> Any:
    """Return a defensive copy of an interaction payload value."""

    return copy.deepcopy(value)


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
        # The websocket response path and the task proposal REST path can
        # consume one request concurrently.  Keep lookup, validation, future
        # resolution, and removal in one critical section so exactly one
        # consumer wins even when those paths are served by different threads.
        self._pending_lock = threading.RLock()
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
                    return (
                        value
                        if len(value) <= 4000
                        else value[:3997].rstrip() + "..."
                    )
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
        with self._pending_lock:
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
        with self._pending_lock:
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
        with self._pending_lock:
            pending_items = list(self._pending.items())
            terminalized_run_ids = set(self._terminalized_run_ids)
        for request_id, request in pending_items:
            run_id = str(request.agent_run_id or "").strip()
            if (
                request.status != HumanInteractionStatus.PENDING
                or request.user_id != scoped_user_id
                or request.session_id != scoped_session_id
                or not run_id
                or run_id in terminalized_run_ids
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
            with self._pending_lock:
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
        timeout_seconds: float | None = None,
    ) -> Any:
        if self._broadcast_callback is None:
            logger.warning("[HumanInteraction] No broadcast callback; denying")
            return None

        run_id = str(agent_run_id or "").strip()

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
        with self._pending_lock:
            if run_id and run_id in self._terminalized_run_ids:
                logger.warning("[HumanInteraction] Run already terminalized: %s", run_id)
                return None
            if kind == HumanInteractionKind.PLAN_APPROVAL and effective_session_id:
                duplicate = any(
                    pending.kind == HumanInteractionKind.PLAN_APPROVAL
                    and pending.status == HumanInteractionStatus.PENDING
                    and pending.session_id == effective_session_id
                    for pending in self._pending.values()
                )
                if duplicate:
                    # A conversation has one authoritative live plan proposal.
                    # Leave the existing Future untouched; the duplicate caller
                    # will fail closed without creating a second UI decision.
                    logger.warning(
                        "[HumanInteraction] Plan approval already pending for session: %s",
                        effective_session_id,
                    )
                    return None
            self._pending[request_id] = request

        # Persist the request before broadcasting it.  A websocket disconnect
        # can therefore still be audited and reconciled after process restart.
        await self._persist_interaction_event(
            request,
            "interaction.requested",
            status="pending",
        )

        try:
            # Persistence yields to terminalization/response races.  Recheck
            # the authoritative map before the first broadcast so a request
            # that was terminalized during persistence is never shown as live.
            with self._pending_lock:
                still_pending = self._pending.get(request_id) is request
                terminalized = bool(
                    run_id and run_id in self._terminalized_run_ids
                )
                request_status = request.status
            if not still_pending or terminalized:
                # REST plan decisions consume the pending entry under the
                # lock, then queue Future completion onto its owning loop.
                # The persistence await above can resume before that queued
                # callback runs, so a resolved request must await its Future
                # instead of being mistaken for a cancellation.
                if request_status == HumanInteractionStatus.RESOLVED:
                    return await future
                if future.done() and not future.cancelled():
                    return future.result()
                return {"cancelled": True}
            await self._broadcast_callback(self._request_envelope(request))
            return await asyncio.wait_for(future, timeout=self._timeout_seconds if timeout_seconds is None else timeout_seconds)
        except asyncio.CancelledError:
            request.status = HumanInteractionStatus.CANCELLED
            # Replacing an expired login choice cancels that interaction,
            # not the parent task which will issue the next choice.
            await self._persist_interaction_event(request, "interaction.cancelled")
            raise
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
            with self._pending_lock:
                self._pending.pop(request_id, None)
            if request.kind == HumanInteractionKind.ASK_USER_QUESTION:
                # Structural login completion cancels its pending UI task.
                # Retire only this correlated question, including replacements.
                envelope = self._request_envelope(request)
                envelope["type"] = "ask_user_question_closed"
                envelope["data"] = {key: envelope["data"][key] for key in
                                    ("request_id", "session_id", "user_id", "agent_run_id", "revision")
                                    if key in envelope["data"]}
                envelope["data"]["status"] = request.status.value
                envelope["event_id"] = f"human-interaction:{request_id}:{request.revision}:closed"
                try:
                    await self._broadcast_callback(envelope)
                except Exception:
                    logger.warning("[HumanInteraction] Question close notification failed")

    def handle_response(
        self,
        request_id: str,
        result: dict[str, Any],
        *,
        requester_user_id: str | None = None,
        requester_session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        with self._pending_lock:
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

    def list_pending_plan_approvals(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Return live plan approvals for one authenticated user/session scope.

        The payload is an internal server projection used by the task proposal
        API.  It is intentionally sourced only from the process-local pending
        map; durable audit events cannot recreate the Future that owns the
        response.
        """

        scoped_user_id = str(user_id or "").strip()
        scoped_session_id = str(session_id or "").strip()
        if not scoped_user_id or not scoped_session_id:
            return []
        with self._pending_lock:
            selected = [
                request
                for request in self._pending.values()
                if request.kind == HumanInteractionKind.PLAN_APPROVAL
                and request.status == HumanInteractionStatus.PENDING
                and request.user_id == scoped_user_id
                and request.session_id == scoped_session_id
                and request.agent_run_id
                and request.agent_run_id not in self._terminalized_run_ids
            ]
            return [
                {
                    "request_id": request.request_id,
                    "session_id": request.session_id,
                    "run_id": request.agent_run_id,
                    "user_id": request.user_id,
                    "revision": int(request.revision or 0),
                    # This method is consumed only inside the authenticated
                    # backend route.  Redact and bound the nested payload
                    # before exposing it; the raw candidate remains private.
                    "payload": self._safe_event_payload(request.payload),
                }
                for request in selected
            ]

    def resolve_plan_approval(
        self,
        request_id: str,
        *,
        decision: str,
        requester_user_id: str,
        requester_session_id: str,
        run_id: str,
        plan_id: str,
        revision: int,
        plan_digest: str,
        action_digest: str,
        material_action_preview_digest: str,
    ) -> dict[str, Any] | None:
        """Atomically validate and consume one server-owned plan approval.

        ``None`` means the request is stale, out of scope, or its immutable
        binding no longer matches.  The method never creates/requeues a new
        request, and it never accepts client-supplied actions or arguments.
        """

        normalized_request_id = str(request_id or "").strip()
        normalized_decision = str(decision or "").strip().lower()
        normalized_user_id = str(requester_user_id or "").strip()
        normalized_session_id = str(requester_session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        normalized_plan_id = str(plan_id or "").strip()
        normalized_plan_digest = str(plan_digest or "").strip()
        normalized_action_digest = str(action_digest or "").strip()
        normalized_preview_digest = str(material_action_preview_digest or "").strip()
        try:
            normalized_revision = int(revision)
        except (TypeError, ValueError):
            return None
        if normalized_decision not in {"approve", "reject"}:
            return None

        with self._pending_lock:
            request = self._pending.get(normalized_request_id)
            if request is None or request.kind != HumanInteractionKind.PLAN_APPROVAL:
                return None
            if request.status != HumanInteractionStatus.PENDING:
                return None
            if request.user_id != normalized_user_id or request.session_id != normalized_session_id:
                return None
            if request.agent_run_id != normalized_run_id:
                return None
            if request.agent_run_id in self._terminalized_run_ids:
                return None
            if int(request.revision or 0) != normalized_revision:
                return None

            payload = request.payload if isinstance(request.payload, dict) else {}
            binding = payload.get("plan_binding")
            if not isinstance(binding, dict):
                return None
            try:
                binding_revision = int(binding.get("revision"))
                payload_revision = int(payload.get("plan_revision"))
            except (TypeError, ValueError):
                return None
            expected_plan_id = str(binding.get("plan_id") or "").strip()
            expected_plan_digest = str(binding.get("plan_digest") or "").strip()
            expected_action_digest = str(binding.get("action_digest") or "").strip()
            expected_preview_digest = str(
                payload.get("material_action_preview_digest")
                or binding.get("material_action_preview_digest")
                or ""
            ).strip()
            if (
                not expected_plan_id
                or not expected_plan_digest
                or not expected_action_digest
                or not expected_preview_digest
                or binding_revision != normalized_revision
                or payload_revision != normalized_revision
                or expected_plan_id != normalized_plan_id
                or expected_plan_digest != normalized_plan_digest
                or expected_action_digest != normalized_action_digest
                or expected_preview_digest != normalized_preview_digest
            ):
                return None

            if normalized_decision == "approve":
                # The binding and preview are copied from the pending server
                # request.  No client action array/arguments enter the Future.
                resolution: dict[str, Any] = {
                    "action": "approve",
                    "pending_interaction_id": request.request_id,
                    "request_id": request.request_id,
                    "revision": normalized_revision,
                    "plan_revision": normalized_revision,
                    "plan_id": expected_plan_id,
                    "plan_binding": _copy_json_value(binding),
                    "plan_digest": expected_plan_digest,
                    "action_digest": expected_action_digest,
                    "material_action_preview_digest": expected_preview_digest,
                }
                preview = payload.get("material_action_preview")
                if isinstance(preview, dict):
                    resolution["material_action_preview"] = _copy_json_value(preview)
                display_plan_text = payload.get("plan_text")
                if isinstance(display_plan_text, str):
                    resolution["plan_text"] = display_plan_text
            else:
                resolution = {
                    "action": "cancel",
                    "cancelled": True,
                    "pending_interaction_id": request.request_id,
                    "request_id": request.request_id,
                    "revision": normalized_revision,
                    "plan_revision": normalized_revision,
                    "plan_id": expected_plan_id,
                    "plan_digest": expected_plan_digest,
                    "action_digest": expected_action_digest,
                    "material_action_preview_digest": expected_preview_digest,
                }

            self._resolve_request(
                request,
                result=resolution,
                status=HumanInteractionStatus.RESOLVED,
            )
            self._schedule_interaction_event(
                request,
                "interaction.resolution",
                status="resolved",
                result=resolution,
            )
            self._pending.pop(normalized_request_id, None)
            return {
                "request_id": request.request_id,
                "session_id": request.session_id,
                "run_id": request.agent_run_id,
                "decision": normalized_decision,
                "resolution": resolution,
            }

    def get_pending_for_run(self, agent_run_id: str) -> list[dict[str, Any]]:
        run_id = str(agent_run_id or "").strip()
        pending: list[dict[str, Any]] = []
        with self._pending_lock:
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
