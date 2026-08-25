"""Session-aware generation dispatch shared by all LLM client types."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_LLM_DISPATCH_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aoitalk_session_llm_dispatch_depth",
    default=0,
)


async def refresh_session_chat_llm_context(client: Any) -> None:
    """Reload persisted session context before binding request-time LLM settings."""
    session_id = str(getattr(client, "current_session_id", None) or "").strip()
    if not session_id:
        return

    from ..memory.conversation_repository import ConversationRepository

    repo = ConversationRepository()
    try:
        row = await repo.get_session_by_id(session_id, with_messages=False)
    except TypeError:
        row = await repo.get_session_by_id(session_id)
    if row is None:
        client._privacy_session_context = {}
        return

    session_context = row.context if isinstance(row.context, dict) else {}
    client._privacy_session_context = dict(session_context)

    from .conversation_session_selection import read_session_llm_settings
    from .session_llm_runtime import restore_session_agent_team_registry

    settings = read_session_llm_settings(session_context)
    owner_user_id = str(
        getattr(client, "session_user_id", None)
        or getattr(row, "user_id", "")
        or ""
    )
    restore_session_agent_team_registry(owner_user_id, session_id, settings)


def invalidate_session_llm_client_cache(client: Any, session_id: str) -> None:
    """Drop cached session metadata on the long-lived client after settings PUT."""
    clean = str(session_id or "").strip()
    if not clean:
        return
    current = str(getattr(client, "current_session_id", None) or "").strip()
    if current != clean:
        return
    if hasattr(client, "_loaded_history_session_id"):
        client._loaded_history_session_id = None
    if hasattr(client, "_loaded_session_id"):
        client._loaded_session_id = None


async def cleanup_ephemeral_llm_client(client: Any) -> None:
    cleanup = getattr(client, "cleanup", None)
    if not callable(cleanup):
        return
    result = cleanup()
    if inspect.isawaitable(result):
        await result


def _route_enriched_stream_callback(
    callback: Any,
    *,
    provider: str,
    model: str,
    route_source: str,
) -> Any:
    """Attach the selected Main route to live stream payloads.

    Provider clients invoke callbacks from both async and worker-thread code;
    returning the original callback's value (including an awaitable) keeps that
    contract intact for either caller.  A shallow payload copy prevents the
    route stamp from mutating provider-owned dictionaries and ``setdefault``
    preserves explicit Team/subagent route metadata.
    """

    if not callable(callback):
        return callback

    def _callback(event_type: Any, data: Any) -> Any:
        if isinstance(data, dict):
            payload = dict(data)
            payload.setdefault("provider", provider)
            payload.setdefault("model", model)
            payload.setdefault("route_source", route_source)
        else:
            payload = data
        return callback(event_type, payload)

    return _callback


_GENERATION_RESULT_STATE_DEFAULTS: dict[str, Any] = {
    "_last_context_snapshots": [],
    "_last_turn_tool_records": [],
    "_last_tool_calls": [],
    "_last_audit_tool_calls": [],
    "_last_agentic_events": [],
    "_last_model_transcript": [],
    "_last_usage_records": [],
    "_last_usage": {},
    "_last_generation_metrics": None,
    "_last_generation_failure": None,
    "_last_turn_tool_rounds_exhausted": False,
    "_last_turn_tool_loop_failed": False,
    "_last_tool_loop_completion_confirmed": False,
    "_last_tool_loop_messages": [],
    "_pending_tool_turn_results": {},
    "_completed_tool_turn_results": {},
    "_last_tool_calls_run_id": None,
    "_last_usage_run_id": None,
}


def _copy_generation_result_state(
    destination_client: Any,
    executed_client: Any,
) -> None:
    """Project request-local execution evidence back to the durable client."""

    for attribute, default_value in _GENERATION_RESULT_STATE_DEFAULTS.items():
        value = getattr(executed_client, attribute, default_value)
        try:
            projected = copy.deepcopy(value)
        except Exception:
            projected = value
        try:
            setattr(destination_client, attribute, projected)
        except Exception:
            logger.debug(
                "Failed to project generation state attribute %s",
                attribute,
                exc_info=True,
            )

    # Deferred packs are conversation state.  Keep each provider's own
    # ToolPackSession object because load_tool_pack closures capture that
    # provider-local session, but merge loaded pack ids monotonically.
    try:
        from ..llm.tool_packs import tool_pack_session_for_client

        executed_session = tool_pack_session_for_client(executed_client)
        destination_session = tool_pack_session_for_client(destination_client)
        destination_session.load_many(executed_session.loaded)
    except Exception:
        logger.debug(
            "Failed to merge session tool-pack state from ephemeral client",
            exc_info=True,
        )


async def _update_current_agent_run_runtime_route(
    *,
    provider: str,
    model: str,
    route_source: str,
    reasoning_effort: str | None,
) -> None:
    """Best-effort route correction for the current durable AgentRun."""

    from .agent_run_service import AgentRunService, get_current_agent_run_id

    run_id = get_current_agent_run_id()
    if not run_id:
        return
    try:
        await AgentRunService().update_runtime_route(
            run_id,
            provider=provider,
            model=model,
            route_source=route_source,
            reasoning_effort=reasoning_effort,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Route metadata is observability only; never turn a successful
        # generation into a failed turn because the correction write failed.
        logger.warning(
            "Failed to stamp actual runtime route for agent run %s",
            run_id,
            exc_info=True,
        )


def _filter_call_kwargs(
    generate: Any,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        signature = inspect.signature(generate)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    if accepts_kwargs:
        return dict(kwargs)
    return {
        key: value
        for key, value in kwargs.items()
        if key in parameters
    }


async def _direct_generate_response_async(
    client: Any,
    user_input: str,
    **kwargs: Any,
) -> str:
    generate_impl = getattr(client, "_generate_async", None)
    if callable(generate_impl):
        return await generate_impl(
            user_input,
            stream_callback=kwargs.get("stream_callback"),
            steering_callback=kwargs.get("steering_callback"),
            image_data=kwargs.get("image_data"),
        )

    unwrapped = getattr(client, "_generate_response_async_impl", None)
    if unwrapped is None:
        unwrapped = getattr(client, "_generate_async_impl", None)
    if callable(unwrapped):
        return await unwrapped(user_input, **_filter_call_kwargs(unwrapped, kwargs))

    raise TypeError(f"Client does not support async generation: {type(client)!r}")


async def _record_generation_last_used_route(
    client: Any,
    config: Any,
    session_context: dict[str, Any],
) -> None:
    """Preference write: last-used includes the route actually bound for generation."""
    try:
        from .conversation_session_selection import (
            read_session_llm_settings,
            resolve_session_effective_main_route,
            session_main_route_override_for_binding,
        )
        from .user_llm_preference_service import (
            has_explicit_last_used_route,
            record_user_last_used_main_route,
        )

        settings = read_session_llm_settings(session_context)
        route = session_main_route_override_for_binding(settings)
        if not has_explicit_last_used_route(route):
            route = resolve_session_effective_main_route(config, settings)
        if not has_explicit_last_used_route(route):
            return
        user_id = str(
            getattr(client, "session_user_id", None)
            or getattr(client, "user_id", None)
            or ""
        ).strip()
        if not user_id:
            return
        await record_user_last_used_main_route(user_id, route)
    except Exception:
        logger.debug(
            "Failed to record last-used LLM route after successful generation",
            exc_info=True,
        )


def _session_route_uses_managed_llama_cpp(config: Any) -> bool:
    """True when session turn construction will ensure a managed local server."""
    from .execution_profile_service import resolve_execution_main_route

    route = resolve_execution_main_route(config)
    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    if provider != "openai_compatible_local" or not model:
        return False
    return model.casefold() != "local-model"


def _local_llm_ensure_user_error(exc: BaseException) -> str:
    from ..llm.openai_compatible_local_engine import (
        LOCAL_MODEL_SERVER_START_FAILED_RESPONSE,
    )

    detail = str(exc).strip()
    if detail:
        return f"{LOCAL_MODEL_SERVER_START_FAILED_RESPONSE}\n\n詳細: {detail}"
    return LOCAL_MODEL_SERVER_START_FAILED_RESPONSE


async def run_session_aware_generation(
    client: Any,
    config: Any,
    user_input: str,
    **kwargs: Any,
) -> str:
    """Bind session LLM runtime and route generation through the public interface."""
    depth = _SESSION_LLM_DISPATCH_DEPTH.get()
    if depth > 0:
        return await _direct_generate_response_async(client, user_input, **kwargs)

    from .session_llm_runtime import (
        bind_session_llm_runtime_from_context,
        build_session_turn_client,
        _client_provider_model,
        resolve_execution_main_route,
        reset_session_llm_runtime,
        session_route_differs_from_client,
    )
    from .session_llm_runtime_context import session_main_route_override

    depth_token = _SESSION_LLM_DISPATCH_DEPTH.set(depth + 1)
    binding = None
    ephemeral = None
    try:
        await refresh_session_chat_llm_context(client)
        session_id = getattr(client, "current_session_id", None)
        session_context = getattr(client, "_privacy_session_context", None)
        if session_id and isinstance(session_context, dict):
            binding = bind_session_llm_runtime_from_context(
                session_context,
                user_id=getattr(client, "session_user_id", None),
                session_id=str(session_id),
                config=config,
            )

        target = client
        if binding and session_route_differs_from_client(client, config):
            try:
                ephemeral = await asyncio.to_thread(
                    build_session_turn_client,
                    client,
                    config,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _session_route_uses_managed_llama_cpp(config):
                    return _local_llm_ensure_user_error(exc)
                raise
            target = ephemeral

        # Resolve the route only after the session context has been bound and
        # the request target has been selected.  The target's provider/model
        # are the source of truth for the durable run stamp; the resolved route
        # only supplies a safe fallback for lightweight test/dummy clients and
        # the effective reasoning effort.
        try:
            resolved_route = resolve_execution_main_route(config)
        except Exception:
            resolved_route = {}
            logger.debug(
                "Failed to resolve session runtime route for metadata stamp",
                exc_info=True,
            )
        actual_provider, actual_model = _client_provider_model(target)
        actual_provider = actual_provider or str(
            resolved_route.get("provider") or ""
        ).strip().lower()
        actual_model = actual_model or str(
            resolved_route.get("model") or ""
        ).strip()
        session_override = session_main_route_override()
        route_source = (
            "session_main_route"
            if isinstance(session_override, dict)
            and str(session_override.get("provider") or "").strip()
            and str(session_override.get("model") or "").strip()
            else "runtime_main"
        )
        reasoning_effort = str(
            resolved_route.get("effort")
            or resolved_route.get("reasoning_effort")
            or ""
        ).strip()

        await _update_current_agent_run_runtime_route(
            provider=actual_provider,
            model=actual_model,
            route_source=route_source,
            reasoning_effort=reasoning_effort or None,
        )

        generation_kwargs = dict(kwargs)
        stream_callback = generation_kwargs.get("stream_callback")
        if stream_callback is not None:
            generation_kwargs["stream_callback"] = _route_enriched_stream_callback(
                stream_callback,
                provider=actual_provider,
                model=actual_model,
                route_source=route_source,
            )
        result = await _direct_generate_response_async(
            target,
            user_input,
            **generation_kwargs,
        )
        client.current_assistant_message_id = getattr(
            target, "current_assistant_message_id", None
        )
        if session_id and isinstance(session_context, dict):
            await _record_generation_last_used_route(client, config, session_context)
        return result
    finally:
        if ephemeral is not None:
            _copy_generation_result_state(
                client,
                ephemeral,
            )
            client.current_assistant_message_id = getattr(
                ephemeral, "current_assistant_message_id", None
            )
        reset_session_llm_runtime(binding)
        _SESSION_LLM_DISPATCH_DEPTH.reset(depth_token)
        if ephemeral is not None:
            await cleanup_ephemeral_llm_client(ephemeral)


__all__ = [
    "cleanup_ephemeral_llm_client",
    "invalidate_session_llm_client_cache",
    "refresh_session_chat_llm_context",
    "run_session_aware_generation",
]
