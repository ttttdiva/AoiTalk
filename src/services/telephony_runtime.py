"""SIP binding of the existing LiveVoice sideband/turn/tool/audit machinery."""
from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import SecurityError

from ..memory.conversation_repository import ConversationRepository
from .actor_principal import ActorPrincipal
from .live_voice_service import (DEFAULT_REALTIME_VOICE, LiveVoiceActor,
    LiveVoiceSession, LiveVoiceService, OpenAIRealtimeProvider, SIDEBAND_SETUP_TIMEOUT_SECONDS,
    LiveVoiceProviderError, _RealtimeSidebandConnection)
from .voice_sessions.models import VoiceSessionMode, VoiceSessionPolicy
from .voice_sessions.openai_realtime_runtime import build_realtime_session_update
from .turn_context import TurnContext


class _SIPConversationRepository(ConversationRepository):
    """Keep all canonical transcript writes on the injected application DB."""
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    async def _get_session(self):
        session = self.manager.get_session()
        return await session if inspect.isawaitable(session) else session


class _SIPSidebandProvider(OpenAIRealtimeProvider):
    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner

    async def _open_sideband(self, call_id):
        # websockets 17 uses additional_headers and has no max_redirects
        # constructor parameter. Override its explicit redirect hook instead
        # of forwarding unsupported kwargs to the event loop transport.
        if not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", call_id):
            raise LiveVoiceProviderError("telephony_call_id_invalid")
        async with self._sideband_lock:
            existing = self._sideband_connections.get(call_id)
            if existing is not None and not existing.closed:
                return existing
            if existing is not None or call_id in self._closed_sideband_ids:
                raise LiveVoiceProviderError("telephony_sideband_closed")
            gateway = self._privacy_gateways.get(call_id)
            if gateway is None:
                raise LiveVoiceProviderError("telephony_privacy_context_missing")
            gateway.ensure_provider_allowed("openai_realtime", base_url="https://api.openai.com")
            socket = await _NoRedirectConnect(
                "wss://api.openai.com/v1/realtime?call_id=" + call_id,
                additional_headers={"Authorization": "Bearer " + self._require_key()},
                open_timeout=self._timeout, close_timeout=5, max_size=128 * 1024,
                proxy=None,
            )
            connection = _RealtimeSidebandConnection(socket)
            await connection.start()
            self._sideband_connections[call_id] = connection
            return connection

    async def hangup_call(self, call_id):
        # All exits, including LiveVoice TTL/disconnect, share the durable
        # one-shot control fence. The base HTTP hangup is never used for SIP.
        await self.owner.service.terminate_runtime(self.owner)


class _NoRedirectConnect(connect):
    def process_redirect(self, exc):
        # Return an exception for every failed handshake; never another URL.
        return SecurityError("telephony_sideband_connection_failed")


class TelephonyLiveRuntime:
    def __init__(self, service, *, snapshot, api_key, sideband_provider=None):
        self.service, self.snapshot = service, snapshot
        self.principal = ActorPrincipal.agent(snapshot["agent_id"])
        self.actor = LiveVoiceActor(user_id="", role="agent", display_name="AI employee",
            agent_id=self.principal.agent_id, agent_revision_id=snapshot["agent_revision_id"])
        self.provider = sideband_provider or _SIPSidebandProvider(self, api_key=api_key, config=service.config)
        self.live = LiveVoiceService(provider=self.provider, config=service.config, db_manager=service.db,
            agent_run_service=service.agent_runs, allowed_tools={"transfer_call", "hangup_call"},
            repository_factory=lambda: _SIPConversationRepository(service.db),
            permission_checker=self._permission, tool_executor=self._execute)
        self.session = None
        self.session_config = {}
        self._closing = False
        self._close_task = None
        self._deadline_task = None

    async def prepare(self):
        snapshot = self.snapshot
        durable = await self.live._load_conversation_session(snapshot["conversation_session_id"])
        context, metadata = await self.live._resolve_privacy_scope(project_id=snapshot["project_id"], durable_session=durable)
        privacy = self.live._privacy_preflight(self.actor, session_id=snapshot["conversation_session_id"],
            project_id=snapshot["project_id"], session_context=context, project_metadata=metadata)
        policy = VoiceSessionPolicy(mode=VoiceSessionMode.REALTIME_NATIVE, realtime_model=snapshot["model"],
            native_voice=DEFAULT_REALTIME_VOICE, instructions=snapshot["instructions"])
        self.session = LiveVoiceSession(id=snapshot["call_id"], actor=self.actor,
            conversation_session_id=snapshot["conversation_session_id"], agent_run_id=snapshot["agent_run_id"],
            project_id=snapshot["project_id"], model=policy.realtime_model, voice=policy.native_voice,
            _instructions=snapshot["instructions"], session_context=context, project_metadata=metadata,
            privacy_mode=privacy, policy=policy, mode=VoiceSessionMode.REALTIME_NATIVE.value)
        self.session.turn_context = TurnContext(user_id=None, project_id=snapshot["project_id"],
            session_id=snapshot["conversation_session_id"], client_message_id="telephony:" + snapshot["call_id"],
            suppress_automatic_context=True, strict_project_scope=True)
        tools = [{"type": "function", "name": "hangup_call", "description": "End this call.",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]
        if snapshot["destination_keys"]:
            tools.append({"type": "function", "name": "transfer_call", "description": "Request a policy-governed referral to a configured destination.",
                "parameters": {"type": "object", "properties": {"destination_key": {"type": "string", "enum": snapshot["destination_keys"]}},
                               "required": ["destination_key"], "additionalProperties": False}})
        self.session_config = build_realtime_session_update(policy, tools=tools, tool_choice="auto")
        self.session_config["model"] = policy.realtime_model
        self.live._sessions[self.session.id] = self.session

    async def attach(self, provider_call_id):
        session = self.session
        session.call_id = provider_call_id
        session._sideband_provenance = object()
        gateway = self.provider._privacy_gateway(actor=self.actor.with_context(session_id=session.conversation_session_id,
            project_id=session.project_id), session_context=session.session_context, project_metadata=session.project_metadata)
        self.provider._privacy_gateways[provider_call_id] = gateway
        confirmation = self.live._register_sideband_confirmation(session, expect_type="session.updated",
            matcher=lambda event: isinstance(event.get("session"), dict) and event["session"].get("type") == "realtime")
        # Sending through the existing egress gateway establishes the socket
        # before the receiver, so a receive-first connect cannot skip privacy.
        update = {key: value for key, value in self.session_config.items() if key != "model"}
        await self.live._send_sideband_event(session, {"type": "session.update", "session": update})
        self.live._sideband_tasks[session.id] = asyncio.create_task(self._run_sideband())
        try:
            await asyncio.wait_for(confirmation, timeout=SIDEBAND_SETUP_TIMEOUT_SECONDS)
        finally:
            session._sideband_confirmations[:] = [item for item in session._sideband_confirmations if item[0] is not confirmation]
        await self.live._ensure_cleanup_task()
        self._deadline_task = asyncio.create_task(self._absolute_deadline())

    async def _absolute_deadline(self):
        from .telephony_service import MAX_CALL_LIFETIME_SECONDS
        started = datetime.fromisoformat(self.snapshot["received_at"])
        remaining = max(0, MAX_CALL_LIFETIME_SECONDS - (datetime.now(timezone.utc) - started).total_seconds())
        await asyncio.sleep(remaining)
        # Like the receiver, a deadline task must not await the teardown owner
        # that cancels/awaits it. There are no DB/control awaits on this task.
        self._schedule_close(terminate=True)

    async def _run_sideband(self):
        try:
            await self.live._run_sideband(self.session, self.actor)
        finally:
            # The idle cleanup task may be awaiting this receiver. Never await
            # teardown here: teardown itself must cancel/await that cleanup.
            self._schedule_close(terminate=True)

    async def _permission(self, tool_name, arguments, *, session):
        if session.actor.agent_id != self.principal.agent_id or session.actor.agent_revision_id != self.snapshot["agent_revision_id"]:
            return False
        if tool_name == "transfer_call":
            if set(arguments) != {"destination_key"} or arguments["destination_key"] not in self.snapshot["destination_keys"]:
                return False
        elif tool_name != "hangup_call" or arguments:
            return False
        try:
            async with self.service.session() as db:
                await self.service._current_call(db, self.snapshot["call_id"])
            return True
        except Exception:
            return False

    async def _execute(self, tool_name, arguments, *, session):
        try:
            if not await self._permission(tool_name, arguments, session=session):
                return {"success": False, "reason_code": "telephony_tool_denied"}
            if tool_name == "transfer_call":
                result = await self.service.transfer(self.snapshot["call_id"], arguments["destination_key"])
                return {"status": str(getattr(result, "classification", None) or (result.get("status") if isinstance(result, dict) else "uncertain"))}
            return await self.service.hangup(self.snapshot["call_id"])
        except Exception:
            # Neither provider errors nor model arguments are echoed to speech.
            return {"success": False, "reason_code": "telephony_control_unavailable"}

    async def close_local(self):
        # Used on ambiguous accept; never issue another provider control.
        await asyncio.shield(self._schedule_close(terminate=False))

    def _schedule_close(self, *, terminate):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close(terminate=terminate))
            # Receiver-initiated cleanup may have no waiting HTTP caller.
            # Retrieve failures so asyncio never logs raw DB/provider errors;
            # explicit close_local callers still observe the task's exception.
            self._close_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return self._close_task

    async def _finish_close(self, *, terminate):
        # An independent task owns teardown. Receivers only schedule it;
        # external/deadline callers shield it from cancellation propagation.
        self._closing = True
        try:
            try:
                if terminate:
                    try:
                        await self.service.terminate_runtime(self)
                    except Exception:
                        await self.service._change(self.snapshot["call_id"], "active", "uncertain",
                            safe_error_code="telephony_teardown_uncertain")
            finally:
                try:
                    deadline, self._deadline_task = self._deadline_task, None
                    await self.live._cancel_task(deadline)
                    if self.session:
                        self.session.status = "closed"
                        self.session._sideband_provenance = None
                        call_id, self.session.call_id = self.session.call_id, None
                        if call_id:
                            await self.provider.close_sideband(call_id)
                finally:
                    # Idle expiry may already have removed the session from
                    # LiveVoice's registry before its audit await was cancelled.
                    # The teardown owner must finish that exact run itself.
                    if self.session:
                        await self.live._complete_agent_run(self.session.agent_run_id,
                            result={"source": "telephony", "telephony_call_id": self.snapshot["call_id"]},
                            message="Telephone runtime closed")
                    await self.live.close()
        finally:
            self.provider._api_key = ""
            self.service._runtimes.pop(self.snapshot["call_id"], None)
