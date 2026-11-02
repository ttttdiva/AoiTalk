"""Owner-scoped fixed-workflow bridge to the existing remote LLM services."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class CanonicalMuseBinding:
    agent_id: str
    agent_revision_id: str | None = None


class ConsumerUnavailable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ConsumerOperationNotFound(Exception):
    def __init__(self, code: str = 'consumer_operation_not_found'):
        self.code = code
        super().__init__(code)


def _key(owner_id: str, app: str, operation_id: str) -> str:
    data = json.dumps([owner_id, app, operation_id], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def _content(system: str, user: str) -> str:
    # The exact serialized text participates in the existing send fingerprint.
    return json.dumps({'system': system, 'user': user}, ensure_ascii=False, separators=(',', ':'))


def _call_admission(service: Any, *, owner_id: str, agent_id: str, snapshot: Any) -> Any:
    fn = getattr(service, '_assert_remote_admission', None)
    if fn is None:
        return None
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        params = {}
    if '_remote_config_snapshot' in params:
        return fn(owner_id=owner_id, agent_id=agent_id, _remote_config_snapshot=snapshot)
    return fn(owner_id=owner_id, agent_id=agent_id)


async def _call_create_session(service: Any, request: dict[str, Any], *, snapshot: Any) -> Any:
    fn = getattr(service, 'create_session', None)
    if fn is None:
        raise ConsumerUnavailable('muse_session_unavailable')
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        params = {}
    if '_remote_config_snapshot' in params:
        return await fn(request, _remote_config_snapshot=snapshot)
    return await fn(request)


async def _call_send(service: Any, sid: str, payload: dict[str, Any], *, snapshot: Any) -> Any:
    fn = getattr(service, 'send', None)
    if fn is None:
        raise ConsumerUnavailable('muse_session_unavailable')
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        params = {}
    if '_remote_config_snapshot' in params:
        return await fn(sid, payload, _remote_config_snapshot=snapshot)
    return await fn(sid, payload)


class RemoteLLMConsumers:
    def __init__(self, *, config: Any = None, get_config: Callable[[], Any] | None = None,
                 muse_binding: CanonicalMuseBinding | Callable[[], CanonicalMuseBinding | None] | None,
                 muse_service_factory: Callable[[Any], Any],
                 bot_service_factory: Callable[[Any], Any],
                 muse_binding_resolver: Callable[[Any], CanonicalMuseBinding | None] | None = None):
        self.config = config
        self.get_config = get_config
        self.muse_binding = muse_binding
        self.muse_binding_resolver = muse_binding_resolver
        self.muse_service_factory = muse_service_factory
        self.bot_service_factory = bot_service_factory

    async def _current_config(self) -> Any:
        value = self.get_config() if self.get_config is not None else self.config
        return await value if inspect.isawaitable(value) else value

    async def _resolve_muse_binding(self, snapshot: Any) -> CanonicalMuseBinding | None:
        if self.muse_binding_resolver is not None:
            value = self.muse_binding_resolver(snapshot)
            return await value if inspect.isawaitable(value) else value
        if callable(self.muse_binding):
            value = self.muse_binding()
            return await value if inspect.isawaitable(value) else value
        return self.muse_binding

    async def turn(self, *, owner_id: str, role: str, consumer_app: str,
                   provider: str, operation_id: str, system: str, user: str) -> dict[str, Any]:
        key = _key(owner_id, consumer_app, operation_id)
        content = _content(system, user)
        config = await self._current_config()
        if provider == 'muse':
            binding = await self._resolve_muse_binding(config)
            if binding is None or not binding.agent_id:
                raise ConsumerUnavailable('muse_canonical_binding_not_configured')
            service = self.muse_service_factory(config)
            if service is None:
                raise ConsumerUnavailable('muse_service_unavailable')
            # Admission is repeated on replay, and does not expose the returned target.
            _call_admission(service, owner_id=owner_id, agent_id=binding.agent_id, snapshot=config)
            request = {'owner_id': owner_id, 'actor_role': role, 'agent_id': binding.agent_id,
                       'provider': 'muse-remote', 'idempotency_key': 'consumer:v1:' + key,
                       'title': 'Remote LLM consumer', 'role_context_mode': 'raw'}
            if binding.agent_revision_id:
                request['agent_revision_id'] = binding.agent_revision_id
            session = await _call_create_session(service, request, snapshot=config)
            sid = str(session.get('session_id') or '')
            if not sid:
                raise ConsumerUnavailable('muse_session_unavailable')
            receipt = await _call_send(service, sid, {'owner_id': owner_id, 'content': content,
                'send_key': 'consumer:v1:' + key, 'muse_tool_loop_opt_in': False}, snapshot=config)
            run_id = str(receipt.get('run_id') or '')
            if not run_id:
                return {'state': 'uncertain', 'text': None}
            from .muse_chat_service import CLISideSessionPort
            state, text = await CLISideSessionPort(config, service=service).run_state(
                side_sid=sid, owner_id=owner_id, run_id=run_id)
            return {'state': state, 'text': text if state == 'replied' else None}
        if provider == 'grok_bot':
            service = self.bot_service_factory(config)
            if service is None:
                raise ConsumerUnavailable('grok_bot_service_unavailable')
            # A separate durable chat per operation prevents unrelated workflows
            # from sharing context. Existing receipt logic reconciles replays.
            def send() -> dict[str, Any]:
                try:
                    result = service.send_turn(owner_id, 'consumer-' + key, 'turn', content)
                    return {'state': result.state, 'text': result.text or None}
                finally:
                    close = getattr(service, 'close', None)
                    if callable(close):
                        close()
            return await asyncio.to_thread(send)
        raise ConsumerUnavailable('provider_unavailable')

    async def get_turn(self, *, owner_id: str, role: str, consumer_app: str,
                       provider: str, operation_id: str,
                       after: str | None = None) -> dict[str, Any]:
        key = _key(owner_id, consumer_app, operation_id)
        config = await self._current_config()
        if provider == 'muse':
            binding = await self._resolve_muse_binding(config)
            if binding is None or not binding.agent_id:
                raise ConsumerUnavailable('muse_canonical_binding_not_configured')
            service = self.muse_service_factory(config)
            if service is None:
                raise ConsumerUnavailable('muse_service_unavailable')
            _call_admission(service, owner_id=owner_id, agent_id=binding.agent_id, snapshot=config)
            idempotency_key = 'consumer:v1:' + key
            identity_fn = getattr(service, '_session_identity', None)
            if callable(identity_fn):
                sid = str(identity_fn(owner_id, idempotency_key))
            else:
                sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:cli-development-session:v1:{owner_id}:{idempotency_key}"))
            try:
                session = await service.get_session(sid, owner_id=owner_id)
            except Exception as exc:
                from .cli_development_sessions import CLIDevelopmentSessionNotFound
                if isinstance(exc, CLIDevelopmentSessionNotFound):
                    raise ConsumerOperationNotFound('consumer_operation_not_found') from exc
                raise
            if session is None:
                raise ConsumerOperationNotFound('consumer_operation_not_found')
            run = session.get('run') if isinstance(session, Mapping) else None
            run_id = str((session.get('run_id') or (run.get('id') if isinstance(run, Mapping) else None)) or '')
            if not run_id:
                return {'state': 'pending', 'text': None}
            from .muse_chat_service import CLISideSessionPort
            state, text = await CLISideSessionPort(config, service=service).run_state(
                side_sid=sid, owner_id=owner_id, run_id=run_id)
            return {'state': state, 'text': text if state == 'replied' else None}
        if provider == 'grok_bot':
            service = self.bot_service_factory(config)
            if service is None:
                raise ConsumerUnavailable('grok_bot_service_unavailable')
            def poll() -> dict[str, Any]:
                try:
                    chat_id = 'consumer-' + key
                    history = service.history(owner_id, chat_id)
                    if not history:
                        raise ConsumerOperationNotFound('consumer_operation_not_found')
                    matching = [r for r in history if r.get('turn_key') == 'turn']
                    if not matching:
                        raise ConsumerOperationNotFound('consumer_operation_not_found')
                    record_op = matching[-1]
                    existing_op = record_op.get('operation_id')
                    if not existing_op:
                        raise ConsumerOperationNotFound('consumer_operation_not_found')
                    refreshed = service.refresh(owner_id, chat_id, existing_op)
                    _result_fn = getattr(service, '_result', None)
                    if callable(_result_fn):
                        result = _result_fn(refreshed)
                        state = result.state
                        text = result.text or None
                    else:
                        state = refreshed.get('state') or 'unknown'
                        text = refreshed.get('reply_text') or None
                    return {'state': state, 'text': text if state == 'replied' else None}
                finally:
                    close = getattr(service, 'close', None)
                    if callable(close):
                        close()
            return await asyncio.to_thread(poll)
        raise ConsumerUnavailable('provider_unavailable')


def resolve_canonical_muse_binding(config: Any) -> CanonicalMuseBinding | None:
    """Resolve only an explicitly pinned agent and revision from the snapshot."""
    from .muse_chat_service import _config_value

    target = _config_value(config, 'remote_targets.muse', {})
    if not isinstance(target, Mapping):
        return None

    def valid_id(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = uuid.UUID(value.strip())
        except ValueError:
            return None
        return str(parsed) if parsed.int else None

    agent_id = valid_id(target.get('chat_agent_id'))
    revisions = [valid_id(target[key]) for key in
                 ('chat_agent_revision_id', 'agent_revision_id', 'revision_id') if key in target]
    if not agent_id or not revisions or None in revisions or len(set(revisions)) != 1:
        return None
    return CanonicalMuseBinding(agent_id, revisions[0])


def normal_muse_service_factory(config: Any) -> Any:
    from .cli_development_sessions import CLIDevelopmentSessionService
    return CLIDevelopmentSessionService(config=config)


def normal_bot_service_factory(config: Any) -> Any:
    from .grok_bot_chat_service import GrokBotChatService, require_grok_bot_chat_settings
    return GrokBotChatService(require_grok_bot_chat_settings(config))
