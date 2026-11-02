"""Session-aware generation dispatch shared by all LLM client types."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import inspect
import logging
from dataclasses import replace
from collections.abc import Mapping
from typing import Any

from .turn_context import get_turn_context

logger = logging.getLogger(__name__)

_SESSION_LLM_DISPATCH_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aoitalk_session_llm_dispatch_depth",
    default=0,
)
_SESSION_GENERATION_ROUTE_METADATA: contextvars.ContextVar[
    tuple[Any, Any, dict[str, str]] | None
] = contextvars.ContextVar(
    "aoitalk_session_generation_route_metadata",
    default=None,
)


def session_generation_route_metadata_for(client: Any) -> dict[str, str] | None:
    """Return successful route evidence only to its originating client task."""
    evidence = _SESSION_GENERATION_ROUTE_METADATA.get()
    if not isinstance(evidence, tuple) or len(evidence) != 3:
        return None
    evidence_client, evidence_task, route = evidence
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        return None
    if evidence_client is not client or evidence_task is not current_task:
        return None
    return dict(route) if isinstance(route, dict) else None


def reset_session_generation_route_metadata_for(client: Any) -> None:
    """Clear a previous turn's route projection before a new run is stamped."""
    evidence = _SESSION_GENERATION_ROUTE_METADATA.get()
    if isinstance(evidence, tuple) and len(evidence) == 3 and evidence[0] is client:
        _SESSION_GENERATION_ROUTE_METADATA.set(None)


async def refresh_session_chat_llm_context(client: Any) -> bool:
    """Reload persisted session context before binding request-time LLM settings."""
    session_id = str(getattr(client, "current_session_id", None) or "").strip()
    if not session_id:
        return False

    from ..memory.conversation_repository import ConversationRepository

    repo = ConversationRepository()
    try:
        row = await repo.get_session_by_id(session_id, with_messages=False)
    except TypeError:
        row = await repo.get_session_by_id(session_id)
    if row is None:
        client._privacy_session_context = {}
        return False

    session_context = row.context if isinstance(row.context, dict) else {}
    client._privacy_session_context = dict(session_context)

    return True


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


async def retire_session_response_model_cache(
    owner_callback: Any,
    session_id: str,
) -> dict[str, int]:
    """Invoke the assistant-owned session cache retire hook at a route boundary.

    The long-lived client invalidator above intentionally handles only client
    history markers.  Response-model cache ownership belongs to the active
    assistant mode, so this adapter keeps the route/service seam async without
    making this service module inspect or release provider leases itself.
    """

    if not callable(owner_callback):
        return {"retired": 0, "deferred": 0, "errors": 0}
    result = owner_callback(session_id)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        return {"retired": 0, "deferred": 0, "errors": 0}
    return {
        "retired": int(result.get("retired") or 0),
        "deferred": int(result.get("deferred") or 0),
        "errors": int(result.get("errors") or 0),
    }


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
    "_last_memory_selection_observation": None,
    "_last_generation_failure": None,
    "_last_turn_tool_rounds_exhausted": False,
    "_last_turn_tool_loop_failed": False,
    "_last_tool_loop_completion_confirmed": False,
    "_last_tool_loop_messages": [],
    "_pending_tool_turn_results": {},
    "_completed_tool_turn_results": {},
    "_last_tool_calls_run_id": None,
    "_last_usage_run_id": None,
    "_native_completed_agent_run_states": {},
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
    executed_route: dict[str, Any],
) -> None:
    """Preference write: last-used includes the route actually bound for generation."""
    try:
        from .user_llm_preference_service import (
            has_explicit_last_used_route,
            record_user_last_used_main_route,
        )

        if not has_explicit_last_used_route(executed_route):
            return
        user_id = str(
            getattr(client, "session_user_id", None)
            or getattr(client, "user_id", None)
            or ""
        ).strip()
        if not user_id:
            return
        await record_user_last_used_main_route(user_id, executed_route)
    except Exception:
        logger.debug(
            "Failed to record last-used LLM route after successful generation",
            exc_info=True,
        )


def _response_model_main_route(client: Any) -> dict[str, str] | None:
    """Project the validated turn selection without changing session settings."""
    selection = getattr(client, "current_response_model", None)
    if not isinstance(selection, dict):
        return None
    provider = str(selection.get("provider") or "").strip().lower()
    model = str(selection.get("model") or "").strip()
    if not provider or not model:
        return None
    route = {"provider": provider, "model": model, "effort": ""}
    if selection.get("reasoning_effort") is not None:
        from ..llm.response_model_effort import validate_response_model_effort

        route["effort"] = validate_response_model_effort(
            provider, model, selection["reasoning_effort"]
        )
    return route


def _session_route_uses_managed_llama_cpp(
    config: Any,
    route_override: Mapping[str, Any] | None = None,
) -> bool:
    """True when session turn construction will ensure a managed local server."""
    from .model_roles_service import resolve_execution_main_route

    route = (
        route_override
        if isinstance(route_override, Mapping)
        else resolve_execution_main_route(config)
    )
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
    from ..llm.frozen_candidate_transport import current_frozen_transport

    execution = getattr(client, "_frozen_candidate_execution", None)
    factory_kwargs = getattr(client, "_frozen_candidate_factory_kwargs", None)
    if (
        execution is not None
        and execution.route.candidate_routes is not None
        and isinstance(factory_kwargs, dict)
        and current_frozen_transport() is None
        and _SESSION_LLM_DISPATCH_DEPTH.get() == 0
    ):
        from .chat_execution_adapter import bind_chat_agent_execution, build_agent_chat_turn_client
        from .scoped_agent_work_turn import execute_frozen_candidates

        start_index = getattr(client, "_frozen_candidate_start_index", 0)
        initial_receipts = getattr(client, "_frozen_candidate_initial_receipts", [])

        async def attempt(index: int, selected: Any) -> str:
            if index != start_index and selected.backend in {"cli", "remote", "pool"}:
                raise NotImplementedError("secondary native, remote or pool workspace admission is not supported")
            selected_execution = replace(execution, route=selected)
            target = client
            turn = get_turn_context()
            async with bind_chat_agent_execution(
                selected_execution,
                user_id=turn.user_id,
                session_id=turn.session_id,
                project_id=turn.project_id,
                config=config,
                workspace=factory_kwargs.get("workspace"),
            ):
                try:
                    if index != start_index:
                        target = await asyncio.to_thread(
                            build_agent_chat_turn_client,
                            client,
                            config,
                            selected_execution,
                            **factory_kwargs,
                        )
                    result = await run_session_aware_generation(target, config, user_input, **kwargs)
                    if target is not client:
                        _copy_generation_result_state(client, target)
                        client._agent_route = target._agent_route
                        client.current_assistant_message_id = getattr(target, "current_assistant_message_id", None)
                    return result
                finally:
                    if target is not client:
                        await cleanup_ephemeral_llm_client(target)

        from .agent_run_service import AgentRunService, get_current_agent_run_id

        async def record_attempts(receipts: list[dict[str, Any]]) -> None:
            client._frozen_candidate_attempts = receipts
            run_id = get_current_agent_run_id()
            if not run_id:
                return
            try:
                service = AgentRunService()
                for receipt in receipts:
                    await service.record_event(run_id, "model.candidate_attempt", payload=receipt)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Failed to record candidate attempts for agent run %s", run_id, exc_info=True)

        try:
            result, receipts, selected = await execute_frozen_candidates(
                execution.route, config, attempt, start_index=start_index
            )
        except Exception as exc:
            receipts = initial_receipts + getattr(exc, "candidate_attempts", [])
            if receipts:
                await record_attempts(receipts)
            raise
        await record_attempts(initial_receipts + receipts)
        client._agent_route = selected.provenance()
        _SESSION_GENERATION_ROUTE_METADATA.set(
            (client, asyncio.current_task(), {
                "provider": selected.provider,
                "model": selected.model,
                "route_source": selected.route_source,
            })
        )
        return result

    depth = _SESSION_LLM_DISPATCH_DEPTH.get()
    if depth > 0:
        return await _direct_generate_response_async(client, user_input, **kwargs)

    # The durable client is reused across turns and its history metadata is
    # requested after this wrapper returns.  Clear the current task's prior
    # projection so a no-ephemeral, failed, or cancelled turn cannot inherit
    # stale execution evidence.  The context value is task-bound below, since
    # child asyncio tasks copy ContextVar values at creation time.
    _SESSION_GENERATION_ROUTE_METADATA.set(None)

    from .session_llm_runtime import (
        bind_session_llm_runtime_from_context,
        build_session_turn_client,
        _client_provider_model,
        _effective_client_effort,
        reset_session_llm_runtime,
        session_route_differs_from_client,
    )
    from .model_roles_service import resolve_execution_main_route
    from .session_llm_runtime_context import (
        bind_session_main_route_override,
        reset_session_main_route_override,
        session_main_route_override,
    )

    depth_token = _SESSION_LLM_DISPATCH_DEPTH.set(depth + 1)
    binding = None
    response_route_token = None
    aoi_route_token = None
    aoi_run_budget_token = None
    ephemeral = None
    aoi_runtime: dict[str, Any] | None = None
    aoi_route_source = ""
    aoi_prompt_snapshot: dict[str, Any] | None = None
    aoi_tool_ids_snapshot: dict[str, Any] | None = None
    execution_route_metadata: dict[str, str] | None = None
    try:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        agent_execution_binding = getattr(
            get_turn_context(), "agent_execution_binding", None
        )
        agent_turn = agent_execution_binding is not None
        # AoiTalk Help is a Guide-only controller turn.  Refreshing the
        # ConversationSession here would read Project/Story/session settings
        # before the provider's own suppression guards run and could also
        # restore Agent Team state from the prior ordinary turn.
        session_context_available = (
            False
            if suppress_automatic_context or agent_turn
            else await refresh_session_chat_llm_context(client)
        )
        session_id = getattr(client, "current_session_id", None)
        session_context = getattr(client, "_privacy_session_context", None)
        # False is authoritative "no durable ConversationSession row".
        # None remains compatible with older test/embedding overrides of the
        # refresh seam which did not return a status.
        if (
            not suppress_automatic_context
            and not agent_turn
            and session_id
            and session_context_available is not False
            and isinstance(session_context, dict)
        ):
            binding = bind_session_llm_runtime_from_context(
                session_context,
                user_id=getattr(client, "session_user_id", None),
                session_id=str(session_id),
                config=config,
            )

        response_route = (
            None
            if suppress_automatic_context or agent_turn
            else _response_model_main_route(client)
        )
        fixed_deployment = False
        if response_route is not None:
            from ..llm.deployment_resolver import resolve_llm_deployment

            deployment = resolve_llm_deployment(config)
            fixed_deployment = deployment is not None and deployment.fixed
            if not fixed_deployment:
                # Keep the session Team/loaded-pack/profile binding, but the
                # selected response model owns Main for this turn only.
                response_route_token = bind_session_main_route_override(response_route)

        # Ordinary Chat keeps its existing request/stream/voice pipeline. Read
        # the owner's migrated Aoi revision only as a request-local overlay;
        # typed Agent/Dialogue turns already carry their immutable execution
        # binding and must not be re-resolved here.
        if not suppress_automatic_context and not agent_turn:
            try:
                from .chat_execution_adapter import (
                    resolve_default_chat_agent_runtime,
                )

                aoi_session_route = session_main_route_override()
                owner_id = str(
                    getattr(client, "session_user_id", None)
                    or getattr(get_turn_context(), "user_id", None)
                    or ""
                ).strip()
                aoi_runtime_value = await resolve_default_chat_agent_runtime(
                    owner_user_id=owner_id,
                    config=config,
                    session_route=(
                        aoi_session_route
                        if isinstance(aoi_session_route, dict)
                        else None
                    ),
                )
                if isinstance(aoi_runtime_value, Mapping):
                    aoi_runtime = dict(aoi_runtime_value)
                    # Ordinary P4 Chat stays on the legacy streaming pipeline
                    # without an immutable agent_execution_binding, so the
                    # AgentRun-wide token/tool limits are off by default. Turn
                    # them on only for automation (background_worker)
                    # conversations, so an endless worker loop ends without
                    # limiting the user's own Chat.
                    if (
                        aoi_runtime.get("agent_id")
                        and aoi_runtime.get("agent_revision_id")
                        and isinstance(session_context, dict)
                        and session_context.get("background_worker") is True
                    ):
                        from ..llm.agent_run_budget import (
                            enable_session_agent_run_budget,
                            get_agent_run_budget,
                        )
                        from .agent_run_service import get_current_agent_run_id

                        aoi_run_budget_token = enable_session_agent_run_budget()
                        current_run_id = get_current_agent_run_id()
                        if current_run_id:
                            get_agent_run_budget(
                                client,
                                run_id=current_run_id,
                                config=config,
                                require_typed_agent=False,
                            )
                    aoi_route_source = str(
                        getattr(aoi_runtime.get("route"), "route_source", "")
                        or ""
                    ).strip()
                    if not isinstance(aoi_session_route, dict):
                        route = aoi_runtime.get("route")
                        provider = str(getattr(route, "provider", "") or "").strip()
                        model = str(getattr(route, "model", "") or "").strip()
                        if provider and model:
                            route_overlay = {
                                "provider": provider,
                                "model": model,
                            }
                            notes = getattr(route, "notes", ())
                            if isinstance(notes, (list, tuple)):
                                for note in notes:
                                    text = str(note or "").strip()
                                    if text.startswith("pool_id:"):
                                        route_overlay["pool_id"] = text[
                                            len("pool_id:") :
                                        ].strip()[:128]
                                        break
                            effort = str(getattr(route, "effort", "") or "").strip()
                            if effort:
                                route_overlay["effort"] = effort
                            aoi_route_token = bind_session_main_route_override(
                                route_overlay
                            )
            except Exception:
                # Aoi is an optional P2 overlay. Its read failure must leave
                # the pre-P2 session route and Character runtime intact.
                logger.debug(
                    "Aoi default Chat overlay unavailable; using legacy Chat runtime",
                    exc_info=True,
                )

        # Resolve and publish an explicit managed-local session route before
        # constructing its request client.  Client construction also performs
        # the managed runtime ensure; if that fails, the selected route still
        # belongs in the run/message audit and must not be replaced by the
        # long-lived client's Main route.
        try:
            resolved_route = resolve_execution_main_route(config)
        except Exception:
            resolved_route = {}
            logger.debug(
                "Failed to resolve session runtime route for metadata stamp",
                exc_info=True,
            )
        session_override = session_main_route_override()
        agent_route_projection = getattr(client, "_agent_route", None)
        agent_route_source = (
            str(agent_route_projection.get("route_source") or "").strip()
            if isinstance(agent_route_projection, dict)
            else ""
        )
        route_source = (
            agent_route_source or "agent_revision"
            if agent_turn
            else "deployment"
            if fixed_deployment
            else "response_model"
            if response_route_token is not None
            else aoi_route_source or "agent_revision"
            if aoi_route_token is not None
            else "session_main_route"
            if isinstance(session_override, dict)
            and str(session_override.get("provider") or "").strip()
            and str(session_override.get("model") or "").strip()
            else "runtime_main"
        )
        from ..llm.openai_compatible_local_profiles import (
            managed_local_runtime_for_model,
        )

        session_override_model = (
            str(session_override.get("model") or "").strip()
            if isinstance(session_override, dict)
            else ""
        )
        selected_runtime = (
            managed_local_runtime_for_model(config, session_override_model)
            if session_override_model
            else None
        )
        selected_session_local_route = bool(
            route_source == "session_main_route"
            and isinstance(session_override, dict)
            and str(session_override.get("provider") or "").strip().lower()
            == "openai_compatible_local"
            and session_override_model
            and selected_runtime in {"llama_cpp", "freetoken", "strata"}
        )
        route_stamped_before_target_build = False
        if selected_session_local_route:
            selected_provider = (
                str(session_override.get("provider") or "").strip().lower()
            )
            selected_model = str(session_override.get("model") or "").strip()
            execution_route_metadata = {
                "provider": selected_provider,
                "model": selected_model,
                "route_source": route_source,
            }
            selected_effort = ""
            if selected_runtime not in {"strata", "freetoken"}:
                selected_effort = str(
                    resolved_route.get("effort")
                    or resolved_route.get("reasoning_effort")
                    or ""
                ).strip().lower()
            await _update_current_agent_run_runtime_route(
                provider=selected_provider,
                model=selected_model,
                route_source=route_source,
                reasoning_effort=selected_effort or None,
            )
            route_stamped_before_target_build = True

        target = client
        if (
            (not agent_turn)
            and (
                binding
                or response_route is not None
                or aoi_route_token is not None
                or aoi_runtime is not None
            )
            and (
                aoi_runtime is not None
                or session_route_differs_from_client(
                    client,
                    config,
                )
            )
        ):
            try:
                ephemeral = await asyncio.to_thread(
                    build_session_turn_client,
                    client,
                    config,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if suppress_automatic_context:
                    # Help must never turn a route-construction failure into
                    # a provider/local-server diagnostic that is not grounded
                    # in the canonical Guide.
                    raise
                if selected_session_local_route or _session_route_uses_managed_llama_cpp(
                    config,
                    session_override if isinstance(session_override, Mapping) else None,
                ):
                    return _local_llm_ensure_user_error(exc)
                raise
            target = ephemeral

        if aoi_runtime is not None and not agent_turn:
            # Apply the Aoi operational text after the request target has been
            # constructed and its Character prompt has been rebuilt. This
            # keeps Character selection/history intact while layering the
            # immutable Agent instructions for this turn.
            instructions = str(
                aoi_runtime.get("operational_instructions") or ""
            ).strip()
            if instructions:
                previous_isolated = getattr(
                    target,
                    "_isolated_system_prompt_override",
                    None,
                )
                previous_system = getattr(target, "system_prompt", None)
                aoi_prompt_snapshot = {
                    "client": target,
                    "had_isolated": hasattr(
                        target,
                        "_isolated_system_prompt_override",
                    ),
                    "isolated": previous_isolated,
                    "had_system": hasattr(target, "system_prompt"),
                    "system": previous_system,
                    "had_agent": hasattr(target, "agent"),
                    "agent": getattr(target, "agent", None),
                }
                effective_prompt_getter = getattr(
                    target,
                    "_build_effective_system_prompt",
                    None,
                ) or getattr(target, "_build_effective_instructions", None)
                try:
                    effective_prompt = (
                        effective_prompt_getter()
                        if callable(effective_prompt_getter)
                        else None
                    )
                except Exception:
                    effective_prompt = None
                base_prompt = str(
                    previous_isolated or effective_prompt or previous_system or ""
                ).strip()
                composed = (
                    f"{base_prompt}\n\n{instructions}"
                    if base_prompt
                    else instructions
                )
                setter = getattr(target, "set_isolated_system_prompt", None)
                if callable(setter):
                    setter(composed)
                else:
                    setter = getattr(target, "set_system_prompt", None)
                    if callable(setter):
                        setter(composed)
                    elif hasattr(target, "system_prompt"):
                        target.system_prompt = composed

            tool_ids = aoi_runtime.get("tool_ids")
            if isinstance(tool_ids, (list, tuple, set, frozenset)):
                aoi_tool_ids_snapshot = {
                    "client": target,
                    "had": hasattr(target, "_agent_tool_ids"),
                    "value": getattr(target, "_agent_tool_ids", None),
                }
                target._agent_tool_ids = list(tool_ids)

        # The target's provider/model is the source of truth after construction.
        # For a managed local session route, fail closed if construction ever
        # returns a different client (including when the base client happened
        # to look reusable); never send that turn through global Main.
        target_provider, target_model = _client_provider_model(target)
        if selected_session_local_route and (
            target_provider
            != str(session_override.get("provider") or "").strip().lower()
            or target_model != str(session_override.get("model") or "").strip()
        ):
            return _local_llm_ensure_user_error(
                RuntimeError(
                    "選択されたセッションローカルモデルを実行クライアントに割り当てられませんでした"
                )
            )
        actual_provider, actual_model = target_provider, target_model
        selected_bound_provider = str(
            session_override.get("provider") or ""
        ).strip().lower() if isinstance(session_override, Mapping) else ""
        selected_bound_model = str(
            session_override.get("model") or ""
        ).strip() if isinstance(session_override, Mapping) else ""
        if (
            route_source in {"session_main_route", "response_model"}
            and selected_bound_provider
            and selected_bound_model
        ):
            # The session/turn picker is authoritative even when a reduced
            # embedding reports that the long-lived client already matches
            # the route and therefore skips ephemeral construction.
            actual_provider = selected_bound_provider
            actual_model = selected_bound_model
        else:
            actual_provider = actual_provider or str(
                resolved_route.get("provider") or ""
            ).strip().lower()
            actual_model = actual_model or str(
                resolved_route.get("model") or ""
            ).strip()
        effort_getter = getattr(target, "_get_reasoning_effort", None)
        selected_bound_effort = str(
            session_override.get("effort") or ""
        ).strip().lower() if isinstance(session_override, Mapping) else ""
        if (
            route_source == "session_main_route"
            and selected_bound_provider
            and selected_bound_model
            and selected_bound_effort
        ):
            reasoning_effort = str(session_override.get("effort") or "").strip().lower()
        elif callable(effort_getter):
            # An omitted turn effort may reuse an already-configured client.
            # Its request getter also authoritatively returns no effort when
            # the provider/model does not send one.
            reasoning_effort = str(effort_getter() or "").strip().lower()
        else:
            reasoning_effort = _effective_client_effort(target) or str(
                resolved_route.get("effort")
                or resolved_route.get("reasoning_effort")
                or ""
            ).strip()

        if not suppress_automatic_context and not route_stamped_before_target_build:
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
        if not suppress_automatic_context and not agent_turn:
            client.current_assistant_message_id = getattr(
                target, "current_assistant_message_id", None
            )
        if (
            not suppress_automatic_context
            and not agent_turn
            and session_id
            and session_context_available is not False
            and isinstance(session_context, dict)
            # Gateway worker conversations must not become the user's default route.
            and session_context.get("background_worker") is not True
        ):
            await _record_generation_last_used_route(
                client,
                {
                    "provider": actual_provider,
                    "model": actual_model,
                    "effort": reasoning_effort,
                },
            )
        if ephemeral is not None and route_source in {
            "session_main_route",
            "agent_revision",
        }:
            execution_route_metadata = {
                "provider": actual_provider,
                "model": actual_model,
                "route_source": route_source,
            }
        return result
    finally:
        if aoi_prompt_snapshot is not None:
            prompt_client = aoi_prompt_snapshot.get("client")
            try:
                if aoi_prompt_snapshot.get("had_isolated"):
                    prompt_client._isolated_system_prompt_override = (
                        aoi_prompt_snapshot.get("isolated")
                    )
                elif hasattr(prompt_client, "_isolated_system_prompt_override"):
                    delattr(prompt_client, "_isolated_system_prompt_override")
                if aoi_prompt_snapshot.get("had_system"):
                    prompt_client.system_prompt = aoi_prompt_snapshot.get("system")
                elif hasattr(prompt_client, "system_prompt"):
                    delattr(prompt_client, "system_prompt")
                if aoi_prompt_snapshot.get("had_agent"):
                    prompt_client.agent = aoi_prompt_snapshot.get("agent")
            except Exception:
                logger.debug("Failed to restore Aoi Chat prompt state", exc_info=True)
        if aoi_tool_ids_snapshot is not None:
            tool_client = aoi_tool_ids_snapshot.get("client")
            try:
                if aoi_tool_ids_snapshot.get("had"):
                    tool_client._agent_tool_ids = aoi_tool_ids_snapshot.get("value")
                elif hasattr(tool_client, "_agent_tool_ids"):
                    delattr(tool_client, "_agent_tool_ids")
            except Exception:
                logger.debug("Failed to restore Aoi Chat tool state", exc_info=True)
        if ephemeral is not None and not suppress_automatic_context:
            _copy_generation_result_state(
                client,
                ephemeral,
            )
            client.current_assistant_message_id = getattr(
                ephemeral, "current_assistant_message_id", None
            )
        if aoi_route_token is not None:
            reset_session_main_route_override(aoi_route_token)
        if response_route_token is not None:
            reset_session_main_route_override(response_route_token)
        if aoi_run_budget_token is not None:
            from ..llm.agent_run_budget import reset_session_agent_run_budget

            reset_session_agent_run_budget(aoi_run_budget_token)
        reset_session_llm_runtime(binding)
        _SESSION_LLM_DISPATCH_DEPTH.reset(depth_token)
        if ephemeral is not None:
            await cleanup_ephemeral_llm_client(ephemeral)
        if execution_route_metadata is not None:
            _SESSION_GENERATION_ROUTE_METADATA.set(
                (client, asyncio.current_task(), execution_route_metadata)
            )


__all__ = [
    "cleanup_ephemeral_llm_client",
    "invalidate_session_llm_client_cache",
    "retire_session_response_model_cache",
    "refresh_session_chat_llm_context",
    "run_session_aware_generation",
]
