"""AgentLLMClient のターン実行・agentic completion・provider state 管理 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import asyncio
import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar
import inspect
import logging
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..native_runtime import (
    AgentDefinition as Agent,
    DEFAULT_MAX_TOOL_ROUNDS,
    NativeModelSettings as ModelSettings,
)
from ..conversation_context import (
    PromptMessages,
    ProviderState,
    compact_model_transcript_for_history,
    stable_cache_key,
)
from ..generation_policy import GenerationProfile, get_client_generation_policy
from ..agentic_completion import (
    agentic_completion_enabled,
    agentic_max_rounds,
    build_agentic_continuation_context,
    build_agentic_review_prompt,
    format_tool_execution_evidence,
    parse_agentic_review_decision,
    render_messages_for_review,
    required_project_mutation_tools_missing,
    run_agentic_completion_loop_async,
    tool_loop_completion_confirmed,
)
from ..agent_runtime import build_tool_hint_context_async
from ..context_snapshot import sanitized_snapshot_series
from ..tool_exposure import filtered_registry_for_client
from ..tool_packs import ensure_load_tool_pack_tool, tool_pack_session_for_client
from ..tool_policy import (
    _docs_agent_delegation_available,
    command_capability_active,
    looks_like_docs_agent_delegation_request,
)
from ..specialist_delegate import (
    reset_runtime_specialist_provider,
    set_runtime_specialist_provider,
)
from ..generation_policy import (
    reset_current_generation_policy,
    set_current_generation_policy,
)
from ..tool_policy import (
    reset_current_user_input,
    set_current_user_input,
)
from ...services.project_context import (
    format_project_context_for_chat_prompt,  # noqa: F401
    get_runtime_project_context,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)
from ...services.agent_run_service import (
    get_current_agent_run_id,
    redact_sensitive_model_transcript,
)
from ...services.agent_team_service import (
    reset_current_continuation_state,
    set_current_continuation_state,
)
from ...services.story_chat_context import StoryChatContextBuildError
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    reset_privacy_policy_context,
    set_privacy_policy_context,
)
from ..generation_cancellation import (
    GenerationInterrupted,
    reset_current_generation_mutation_gate,
    set_current_generation_mutation_gate,
)
from ..runtime_tool_registry import build_runtime_tool_registry

logger = logging.getLogger(__name__)

# Unlike the public generation-policy contextvar, this variable is only set
# by ``_generate_async`` and therefore carries explicit turn provenance.  A
# caller that merely inherited a stale contextvar must not override the
# current client's policy for a new direct/native entry.
_native_generation_policy_snapshot: ContextVar[Any | None] = ContextVar(
    "aoitalk_native_generation_policy_snapshot",
    default=None,
)
# Native completion review callbacks run in the same task as their native
# runner invocation.  Keep the result that belongs to that task here instead
# of making the callback read the client's mutable ``_last_*`` attributes.  A
# ``None`` value means that the caller did not establish a turn scope (for
# example a direct low-level ``_run_once_with_agent`` call).
_native_turn_result_snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
    "aoitalk_native_turn_result_snapshot",
    default=None,
)

_NATIVE_WORK_MAX_TOOL_ROUNDS = 120
_NATIVE_RUNNER_LOCK_INIT = threading.Lock()
_NATIVE_GENERATION_LOCK_INIT = threading.Lock()

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[List[str]]]


def _configure_turn_context_snapshot(client: Any, runner: Any) -> None:
    """Describe turn-local injected blocks separately from the user message."""
    from ..context_snapshot import component, context_bundle_components

    rendered_bundle, bundle_parts = context_bundle_components(
        getattr(client, "_current_context_bundle", None)
    )
    dynamic_context = list(
        getattr(client, "_current_prompt_dynamic_context", []) or []
    )
    exclusions: list[str] = []
    dynamic_parts: list[dict[str, Any]] = []
    includes_bundle = False
    for label, raw_text in dynamic_context:
        text = str(raw_text or "").strip()
        if not text:
            continue
        block = f"[{label}]\n{text}"
        exclusions.append(block)
        normalized_label = str(label or "").casefold()
        if "contextbundle" in normalized_label:
            includes_bundle = True
            dynamic_parts.append(
                component(
                    "prompt_scaffolding",
                    "ContextBundle wrapper",
                    f"[{label}]\n",
                    source="prompt composer",
                    selection_reason="labels the injected ContextBundle",
                    selected_chars=len(f"[{label}]\n"),
                )
            )
        elif "memory" in normalized_label:
            dynamic_parts.append(
                component(
                    "past_conversation_recall",
                    "Past conversation recall",
                    block,
                    source="CrossSessionMemory semantic search",
                    selection_reason="explicit_past_reference_intent",
                    duration_ms=getattr(
                        client,
                        "_current_memory_recall_duration_ms",
                        None,
                    ),
                    selected_chars=len(block),
                )
            )
        elif "tool" in normalized_label:
            dynamic_parts.append(
                component(
                    "tool_hints",
                    "Current tool hints",
                    block,
                    source="runtime tool registry",
                    selection_reason="tools available for current generation purpose",
                    duration_ms=getattr(
                        client,
                        "_current_tool_hint_duration_ms",
                        None,
                    ),
                    selected_chars=len(block),
                )
            )
        elif "project" in normalized_label:
            dynamic_parts.append(
                component(
                    "project_context",
                    "Current Project Context",
                    block,
                    source="runtime project context",
                    selection_reason="selected project for current turn",
                    selected_chars=len(block),
                )
            )

    if exclusions:
        user_input_wrapper = "[Current user input]\nCurrent user request:\n"
        separators = "\n\n" * len(exclusions)
        current_user_scaffolding = f"{separators}{user_input_wrapper}"
        exclusions.append(current_user_scaffolding)
        dynamic_parts.append(
            component(
                "prompt_scaffolding",
                "Current user input wrapper",
                current_user_scaffolding,
                source="prompt composer",
                selection_reason="separates injected context from user input",
                selected_chars=len(current_user_scaffolding),
            )
        )

    runner.snapshot_rendered_bundle = ""
    runner.snapshot_bundle_components = bundle_parts if includes_bundle else []
    runner.snapshot_excluded_texts = exclusions
    runner.snapshot_dynamic_components = dynamic_parts


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        try:
            value = config.get(key, None)
        except TypeError:
            value = config.get(key)
        if value is not None:
            return value
    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


class TurnExecutionMixin:
    """native runtime ターン実行、agentic completion ループ、steering、状態遷移。"""

    def _run_async_safe(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
        image_data: dict | None = None,
    ) -> str:
        """Safely run async code in a new event loop"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            return loop.run_until_complete(
                self._generate_async(
                    user_input,
                    stream_callback=stream_callback,
                    steering_callback=steering_callback,
                    image_data=image_data,
                )
            )
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def _consume_steering_instructions(
        self, steering_callback: Optional[SteeringCallback]
    ) -> List[str]:
        if not steering_callback:
            return []
        try:
            result = steering_callback()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                result = [result]
            if not isinstance(result, list):
                return []
            return [str(item).strip() for item in result if str(item).strip()]
        except Exception as exc:
            print(f"[AgentLLMClient] 追加指示の取得に失敗しました: {exc}")
            return []

    async def _append_pending_steering_to_context(
        self,
        context: str,
        steering_callback: Optional[SteeringCallback],
        stream_callback: Optional[StreamCallback],
    ) -> str:
        instructions = await self._consume_steering_instructions(steering_callback)
        if not instructions:
            return context
        if stream_callback:
            await stream_callback(
                "status_update",
                {
                    "status": "steering_applied",
                    "message": "追加指示を反映します",
                },
            )
        instruction_block = "\n".join(f"- {instruction}" for instruction in instructions)
        if isinstance(context, list):
            updated = PromptMessages(dict(item) for item in context)
            if updated and updated[-1].get("role") == "user":
                updated[-1]["content"] = (
                    f"{updated[-1].get('content') or ''}\n\n追加指示:\n{instruction_block}"
                )
            return updated
        return f"{context}\n\n追加指示:\n{instruction_block}"

    def _agentic_completion_enabled(self, user_input: str | None = None) -> bool:
        if not getattr(self, "_native_tools_enabled", True):
            return False
        return agentic_completion_enabled(self, user_input)

    def _legacy_reasoning_manager_allowed_for_turn(
        self,
        available_tools: List[str],
    ) -> bool:
        """Keep legacy planning out of the native tool-capable Main chat path.

        PlanningPolicy and the native agentic loop already own planning,
        approval, continuation, and tool execution.  The legacy
        ReasoningManager is retained only for compatibility turns where no
        effective native tool is available.
        """

        if not getattr(self, "reasoning_manager", None):
            return False
        policy = self._snapshot_generation_policy()
        return not (
            getattr(self, "_native_tools_enabled", True)
            and bool(available_tools)
            and bool(getattr(policy, "discretionary_tool_loop_enabled", False))
        )

    def _snapshot_generation_policy(self) -> Any:
        """Capture the generation policy consistently for one native entry.

        ``_generate_async`` binds the turn policy in a private context
        variable before any asynchronous work.  Direct/native bridge callers
        may not have that binding, so they use the client's current policy.
        The returned policy is immutable and callers keep it in a local
        variable for the rest of the turn; no later mutation of the shared
        client can change the budget.
        """
        turn_policy = _native_generation_policy_snapshot.get()
        if turn_policy is not None:
            return turn_policy
        return get_client_generation_policy(self)

    def _generation_policy_client(self, generation_policy: Any) -> Any:
        """Build an immutable-shaped client view for policy-only helpers."""
        return SimpleNamespace(
            config=getattr(self, "config", None),
            generation_policy=generation_policy,
        )

    def _agentic_max_rounds(
        self,
        user_input: str | None = None,
        *,
        generation_policy: Any | None = None,
    ) -> int:
        """Resolve the review/work budget from a turn-local policy snapshot.

        ``generation_policy`` is normally captured before entering the native
        runner.  The LLM client is shared by several dispatch paths and its
        ``generation_policy`` attribute is mutable, so consulting it after
        asynchronous setup can accidentally downgrade an autonomous turn to
        the chat budget.  ``agentic_max_rounds`` only needs ``config`` and
        ``generation_policy`` from the client; a tiny immutable-shaped proxy
        keeps that calculation local without mutating the shared client.
        """
        if generation_policy is None:
            return agentic_max_rounds(self, user_input)
        return agentic_max_rounds(
            self._generation_policy_client(generation_policy),
            user_input,
        )

    def _native_tool_round_budget(self, *, generation_policy: Any) -> int:
        """Resolve the native tool-loop budget from profile/config only.

        The outer agentic review loop intentionally treats managed-workspace
        prompts as a short bounded verification pass.  That rule must not
        leak into the native tool loop: an autonomous work turn still needs
        its work budget even when its Japanese prompt contains workspace
        wording.  This resolver therefore never inspects ``user_input``.
        """

        def _config_int(key: str, default: int) -> int:
            raw_value = _config_get(self.config, key, None)
            try:
                return max(1, int(raw_value)) if raw_value is not None else default
            except (TypeError, ValueError):
                return default

        base_budget = _config_int(
            "agentic_completion.max_tool_rounds",
            DEFAULT_MAX_TOOL_ROUNDS,
        )
        profile = getattr(generation_policy, "profile", None)
        if profile in {
            GenerationProfile.ASSISTED_WORK,
            GenerationProfile.AUTONOMOUS_WORK,
        }:
            profile_budget = _config_int(
                f"agentic_completion.{profile.value}_max_rounds",
                _NATIVE_WORK_MAX_TOOL_ROUNDS,
            )
            work_budget = _config_int(
                "agentic_completion.work_max_rounds",
                _NATIVE_WORK_MAX_TOOL_ROUNDS,
            )
            return max(base_budget, work_budget, profile_budget)
        return base_budget

    def _native_runner_thread_lock(self) -> threading.Lock:
        """Return one client-wide lock shared by all event loops/threads."""
        lock = getattr(self, "_native_runner_thread_lock_instance", None)
        if lock is None:
            # The initialization guard matters when sync callers create a new
            # event loop in separate threads at the same time.
            with _NATIVE_RUNNER_LOCK_INIT:
                lock = getattr(self, "_native_runner_thread_lock_instance", None)
                if lock is None:
                    lock = threading.Lock()
                    self._native_runner_thread_lock_instance = lock
        return lock

    def _native_generation_thread_lock(self) -> threading.Lock:
        """Return the client-wide lock for a complete ``_generate_async`` turn."""
        lock = getattr(self, "_native_generation_thread_lock_instance", None)
        if lock is None:
            with _NATIVE_GENERATION_LOCK_INIT:
                lock = getattr(self, "_native_generation_thread_lock_instance", None)
                if lock is None:
                    lock = threading.Lock()
                    self._native_generation_thread_lock_instance = lock
        return lock

    @asynccontextmanager
    async def _native_runner_lock_scope(self):
        """Acquire the client-wide runner lock without blocking an event loop.

        A regular ``threading.Lock`` is intentional here: ``asyncio.Lock`` is
        bound to one event loop, while an AgentLLMClient can be reached from
        sync bridge threads that each create their own loop.  Non-blocking
        polling keeps those loops responsive and is cancellation-safe because
        no worker thread can acquire the lock after the coroutine is gone.
        """
        lock = self._native_runner_thread_lock()
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.005)
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def _native_generation_lock_scope(self):
        """Serialize complete client turns without blocking event-loop threads.

        The native runner lock above protects the mutable runner object.  The
        broader generation lock additionally covers history/provider/session
        attributes that are read and written around the runner.  It is a
        separate lock so callers that only use the low-level native bridge do
        not form a lock-order cycle with ``_generate_async``.
        """
        lock = self._native_generation_thread_lock()
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.005)
        try:
            yield
        finally:
            lock.release()

    def _parse_agentic_review_decision(self, content: str) -> dict[str, str]:
        return parse_agentic_review_decision(content)

    def _build_agentic_review_prompt(
        self,
        *,
        original_context: str,
        latest_response: str,
        round_index: int,
        user_input: str | None = None,
    ) -> str:
        return build_agentic_review_prompt(
            original_context=original_context,
            latest_response=latest_response,
            round_index=round_index,
            user_input=user_input,
        )

    def _build_agentic_continuation_context(
        self,
        *,
        original_context: str,
        latest_response: str,
        decision: dict[str, str],
    ) -> str:
        return build_agentic_continuation_context(
            original_context=original_context,
            latest_response=latest_response,
            decision=decision,
        )

    async def _run_once_with_agent(
        self,
        agent: Agent,
        context: str,
        stream_callback: Optional[StreamCallback] = None,
        required_tool_name: Optional[str] = None,
        generation_policy: Any | None = None,
    ) -> str:
        # Snapshot this immutable policy before any await.  Do not read the
        # shared client attribute again while the runner is active.
        run_generation_policy = (
            generation_policy
            if generation_policy is not None
            else self._snapshot_generation_policy()
        )
        native_tool_round_budget = self._native_tool_round_budget(
            generation_policy=run_generation_policy,
        )
        # Cache fingerprints must describe the same effective schemas sent to
        # the provider, not the full registry held by ``agent.tools``.  The
        # latter includes deferred Apps/Spotify/media definitions and can
        # retain stale contextual entries when a fixed provider registry is
        # reused across turns.  Required-tool turns intentionally keep their
        # already narrow allowlist.
        cache_tools = list(getattr(agent, "tools", []))
        if required_tool_name is None:
            try:
                cache_tools = list(self._get_effective_tools_for_current_session())
            except Exception:  # noqa: BLE001 - preserve legacy lightweight clients
                cache_tools = list(getattr(agent, "tools", []))
        tool_schemas = [
            {
                "type": "function",
                "name": str(getattr(tool, "name", "")),
                # Request-local contextual delegates keep the same function
                # name/JSON parameters while their roster description changes
                # with General/App/Story activation.  Include that text in
                # the cache fingerprint so a prior turn cannot reuse a stale
                # Team roster prompt.
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": tool.to_json_schema(),
            }
            for tool in cache_tools
        ]
        cache_key = stable_cache_key(
            user_id=self._get_session_user_id(),
            session_id=self.current_session_id,
            project_id=self.current_project_id,
            character=self.character_name,
            model=str(agent.model or self.model_name),
            system_prompt=str(agent.instructions or ""),
            tool_schemas=tool_schemas,
            provider=self.provider_label,
            branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
            summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
            server_instance=str(getattr(self, "session_metadata", {}).get("server_instance") or "default-instance"),
        )
        state: ProviderState
        state_mode_before: str
        state_was_invalidated: bool
        provider_state_mode_after: str
        provider_state_to_persist: dict[str, Any]
        # Capture the history checkpoint while the runner lock is held.  The
        # usage persistence below intentionally stays outside that lock, so a
        # concurrent turn must not change the base transcript used to merge
        # this result.
        authoritative_before: list[dict[str, Any]]
        active_before: list[dict[str, Any]]
        async with self._native_runner_lock_scope():
            # All mutable runner/provider state belongs inside the same
            # client-wide scope as max_tool_rounds.  A waiting turn must not
            # overwrite the active turn's context snapshot or provider state
            # before the active request has finished.
            state_mode_before = self._provider_state_mode
            # Re-scope reversible aliases to the active conversation.  The
            # gateway never persists raw alias mappings and must not leak one
            # user's/session's aliases into the next turn of a shared client.
            privacy_gateway = getattr(self._turn_runner, "privacy_gateway", None)
            expected_session_id = str(self.current_session_id or "")
            try:
                expected_user_id = str(self._get_session_user_id() or "")
            except Exception:  # noqa: BLE001
                expected_user_id = ""
            if not isinstance(privacy_gateway, OutboundPrivacyGateway) or (
                privacy_gateway.session_id != expected_session_id
                or privacy_gateway.user_id != expected_user_id
            ):
                self._turn_runner.privacy_gateway = OutboundPrivacyGateway(
                    self.config,
                    session_id=expected_session_id,
                    user_id=expected_user_id,
                    session_context=getattr(self, "_privacy_session_context", None),
                    project_metadata=getattr(self, "_privacy_project_metadata", None),
                )
            else:
                privacy_gateway.update_policy_context(
                    session_context=getattr(self, "_privacy_session_context", None),
                    project_metadata=getattr(self, "_privacy_project_metadata", None),
                )
            state = ProviderState(
                mode=state_mode_before,
                previous_response_id=self._provider_state.get("previous_response_id"),
                fingerprint=self._provider_state.get("fingerprint"),
            )
            if state.fingerprint and state.fingerprint != cache_key:
                state.reset()
            state.fingerprint = cache_key
            self._turn_runner.conversation_state_mode = state_mode_before
            self._turn_runner.provider_state = state
            self._turn_runner.prompt_cache_key = cache_key
            self._turn_runner.prompt_cache_retention = (
                str(_config_get(self.config, "openai.prompt_cache_retention", "") or "").strip()
                or None
            )
            _configure_turn_context_snapshot(self, self._turn_runner)
            turn_snapshot = _native_turn_result_snapshot.get()
            if turn_snapshot is not None and "model_transcript" in turn_snapshot:
                # A continuation/review belongs to the same logical turn even
                # when another task has already written the client's shared
                # history fields.  Prefer the task-local checkpoint so the
                # provider result cannot merge against that other turn.
                authoritative_source = turn_snapshot.get("model_transcript") or []
                active_source = turn_snapshot.get("active_model_transcript") or []
            else:
                authoritative_source = (
                    getattr(self, "_history_authoritative_model_transcript", None)
                    or []
                )
                active_source = (
                    getattr(self, "_history_active_model_transcript", None) or []
                )
            authoritative_before = [
                dict(message)
                for message in authoritative_source
                if isinstance(message, dict)
            ]
            active_before = [
                dict(message)
                for message in active_source
                if isinstance(message, dict)
            ]
            if turn_snapshot is not None:
                turn_snapshot["usage_metadata"] = {
                    "provider_label": str(
                        getattr(self, "provider_label", "openai") or "openai"
                    ),
                    "requested_model": str(agent.model or self.model_name),
                    "session_id": getattr(self, "current_session_id", None),
                    "user_id": self._get_session_user_id(),
                    "project_id": getattr(self, "current_project_id", None),
                    "agent_name": str(agent.name or self.character_name),
                }
            previous_max_tool_rounds = getattr(
                self._turn_runner,
                "max_tool_rounds",
                None,
            )
            if previous_max_tool_rounds is not None:
                self._turn_runner.max_tool_rounds = max(
                    previous_max_tool_rounds,
                    native_tool_round_budget,
                )
            # ``load_tool_pack`` mutates the client session while the native
            # runner is in its tool loop.  Resolve the effective registry on
            # every provider round so a newly loaded pack (for example the
            # Agent Team delegate) is exposed as an actual function schema.
            # Required-tool turns intentionally keep their narrow allowlist.
            dynamic_story_context = (
                self._get_story_chat_context_sync()
                if required_tool_name is None
                else None
            )

            def _native_tools_provider(_agent: Agent):
                return self._get_effective_tools_for_current_session(
                    dynamic_story_context
                )

            run_kwargs: dict[str, Any] = {
                "stream_callback": stream_callback,
            }
            # Keep lightweight compatibility runners (used by integrations and
            # older tests) working while the native runner gains the optional
            # dynamic resolver argument.
            try:
                runner_parameters = inspect.signature(
                    self._turn_runner.run
                ).parameters
            except (TypeError, ValueError):
                runner_parameters = {}
            if (
                required_tool_name is None
                and (
                    "tools_provider" in runner_parameters
                    or any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in runner_parameters.values()
                    )
                )
            ):
                run_kwargs["tools_provider"] = _native_tools_provider
            # NativeRunResult can classify an empty final response without
            # raising. Reset the attempt-local marker before running so a
            # previous turn cannot leak its failure into this one.
            self._last_generation_failure = None
            try:
                result = await self._turn_runner.run(
                    agent,
                    context,
                    **run_kwargs,
                )
            finally:
                if previous_max_tool_rounds is not None:
                    self._turn_runner.max_tool_rounds = previous_max_tool_rounds
            runner_state_mode = getattr(
                self._turn_runner,
                "conversation_state_mode",
                "",
            )
            if state_mode_before == "provider-managed" and runner_state_mode == "stateless":
                # The provider rejected/expired managed state.  Keep subsequent
                # turns stateless until the client/session is explicitly reset.
                self._provider_state_mode = "stateless"
            self._provider_state = {
                "previous_response_id": state.previous_response_id,
                "fingerprint": state.fingerprint,
            }
            provider_state_mode_after = self._provider_state_mode
            provider_state_to_persist = dict(self._provider_state)
            state_was_invalidated = (
                state_mode_before == "provider-managed"
                and runner_state_mode == "stateless"
                and state.previous_response_id is None
            )
        if state_was_invalidated and self.current_session_id and self.memory_manager:
            try:
                await self.memory_manager.repository.update_session_context(
                    self.current_session_id,
                    {"llm_provider_state": {}},
                )
            except Exception:
                logger.warning("無効化したprovider-managed stateの消去に失敗しました", exc_info=True)
        if (
            self.current_session_id
            and self.memory_manager
            and provider_state_mode_after == "provider-managed"
        ):
            try:
                await self.memory_manager.repository.update_session_context(
                    self.current_session_id,
                    {"llm_provider_state": provider_state_to_persist},
                )
            except Exception:
                logger.warning("provider-managed stateの永続化に失敗しました", exc_info=True)
        result_context_snapshots = list(
            getattr(result, "context_snapshots", None) or []
        )
        self._last_generation_failure = getattr(
            result,
            "generation_failure",
            None,
        )
        result_tool_records = list(getattr(result, "tool_calls", None) or [])
        result_tool_rounds_exhausted = bool(
            getattr(result, "tool_rounds_exhausted", False)
        )
        result_tool_loop_completion_confirmed = tool_loop_completion_confirmed(
            result_tool_records,
            getattr(result, "final_output", None),
            stopped_reason=(
                "final"
                if (
                    not result_tool_rounds_exhausted
                    and self._last_generation_failure is None
                )
                else None
            ),
        )
        result_model_transcript = [
            dict(message)
            for message in (getattr(result, "messages", None) or [])
            if isinstance(message, dict) and message.get("role") in {"user", "assistant", "tool"}
        ]
        # Publish a per-turn immutable-ish result before the usage repository
        # await.  The outer review callback can then continue to use this
        # turn's evidence even when another task completes and updates the
        # client's shared ``_last_*`` fields in the meantime.
        turn_snapshot = _native_turn_result_snapshot.get()
        if turn_snapshot is not None:
            turn_snapshot.update(
                {
                    "context_snapshots": list(result_context_snapshots),
                    "tool_records": list(result_tool_records),
                    "tool_rounds_exhausted": result_tool_rounds_exhausted,
                    "tool_loop_completion_confirmed": (
                        result_tool_loop_completion_confirmed
                    ),
                    "usage_records": [
                        dict(item)
                        for item in (getattr(result, "usage_records", None) or [])
                        if isinstance(item, dict)
                    ],
                    "result_model_transcript": [
                        dict(message) for message in result_model_transcript
                    ],
                    "authoritative_before": [
                        dict(message) for message in authoritative_before
                    ],
                    "active_before": [dict(message) for message in active_before],
                }
            )
        await self._record_native_usage(result, agent)
        if (
            authoritative_before
            and active_before
            and result_model_transcript[: len(active_before)] == active_before
        ):
            # The provider result starts with the compact active history.  Add
            # only the new suffix to the full checkpoint so older tool payloads
            # are not replaced by their compact prompt representation.
            last_model_transcript = [
                *authoritative_before,
                *result_model_transcript[len(active_before) :],
            ]
        else:
            # Branch/provider resets can legitimately produce a disjoint
            # transcript; in that case the result itself is the new checkpoint.
            last_model_transcript = result_model_transcript
        active_model_transcript = compact_model_transcript_for_history(
            last_model_transcript,
            getattr(self, "config", None),
        )
        if turn_snapshot is not None:
            turn_snapshot.update(
                {
                    "model_transcript": [
                        dict(message) for message in last_model_transcript
                    ],
                    "active_model_transcript": [
                        dict(message)
                        for message in active_model_transcript
                        if isinstance(message, dict)
                    ],
                }
            )
        # Keep the shared compatibility fields and HistoryManager update
        # coherent, but do not hold this client-wide lock over usage/database
        # awaits above.
        async with self._native_runner_lock_scope():
            self._last_context_snapshots = list(result_context_snapshots)
            self._last_turn_tool_records = list(result_tool_records)
            self._last_turn_tool_rounds_exhausted = result_tool_rounds_exhausted
            self._last_tool_loop_completion_confirmed = (
                result_tool_loop_completion_confirmed
            )
            self._last_model_transcript = [
                dict(message) for message in last_model_transcript
            ]
            self._history_authoritative_model_transcript = [
                dict(message) for message in self._last_model_transcript
            ]
            self._history_active_model_transcript = [
                dict(message)
                for message in active_model_transcript
                if isinstance(message, dict)
                and message.get("role") in {"user", "assistant", "tool"}
            ]
            self._last_usage_records = [
                dict(item)
                for item in (getattr(result, "usage_records", None) or [])
                if isinstance(item, dict)
            ]
            if hasattr(self.history_manager, "set_model_messages"):
                self.history_manager.set_model_messages(active_model_transcript)
        if result_tool_records:
            print(f"[AgentLLMClient] Tool messages found: {len(result_tool_records)}")
        if required_tool_name and not any(
            record.tool == required_tool_name and record.successful
            for record in result_tool_records
        ):
            logger.warning(
                "[AgentLLMClient] required tool %s was not called",
                required_tool_name,
            )
            return (
                "ツール実行の検証に失敗しました: "
                f"必須ツール `{required_tool_name}` が実行されませんでした。"
            )
        return str(result.final_output or "")

    async def _record_native_usage(self, result: Any, agent: Agent) -> None:
        """Native runtimeの各provider request usageを永続化する。"""
        records = list(getattr(result, "usage_records", None) or [])
        if not records:
            return

        def _uuid_or_none(value: Any):
            try:
                return uuid.UUID(str(value)) if value else None
            except (TypeError, ValueError, AttributeError):
                return None

        from ...services.token_tracking_service import get_token_tracking_service

        service = get_token_tracking_service()
        # Snapshot mutable client/session metadata before the first await.  A
        # concurrent turn may switch sessions, providers, or character agents
        # while usage rows are being written; those rows still belong to this
        # native result.
        turn_snapshot = _native_turn_result_snapshot.get()
        usage_metadata = (
            turn_snapshot.get("usage_metadata")
            if turn_snapshot is not None
            else None
        )
        if not isinstance(usage_metadata, dict):
            usage_metadata = {}
        provider_label = str(
            usage_metadata.get("provider_label")
            or getattr(self, "provider_label", "openai")
            or "openai"
        )
        requested_model = str(
            usage_metadata.get("requested_model")
            or agent.model
            or self.model_name
        )
        session_id = _uuid_or_none(
            usage_metadata.get("session_id", self.current_session_id)
        )
        user_id = str(
            usage_metadata.get("user_id") or self._get_session_user_id()
        )
        project_id = _uuid_or_none(
            usage_metadata.get("project_id", self.current_project_id)
        )
        agent_name = str(
            usage_metadata.get("agent_name")
            or agent.name
            or self.character_name
        )
        for usage in records:
            await service.record_usage(
                provider=provider_label,
                model=requested_model,
                requested_model=requested_model,
                resolved_model=usage.get("resolved_model"),
                provider_reported_cost=usage.get("provider_reported_cost"),
                provider_reported_cost_details=usage.get(
                    "provider_reported_cost_details"
                ),
                tool_invocations=usage.get("tool_invocations"),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=int(usage.get("cached_tokens") or 0),
                cache_read_tokens=int(
                    usage.get("cache_read_tokens")
                    or usage.get("cached_tokens")
                    or 0
                ),
                cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
                prompt_eval_tokens=int(usage.get("prompt_eval_tokens") or 0),
                prompt_eval_ms=int(usage.get("prompt_eval_ms") or 0),
                cache_hit_rate=usage.get("cache_hit_rate"),
                cache_evictions=int(usage.get("cache_evictions") or 0),
                cache_provider=usage.get("cache_provider"),
                cache_mode=usage.get("cache_mode"),
                cache_key=usage.get("cache_key"),
                cache_supported=usage.get("cache_supported"),
                cache_active=usage.get("cache_active"),
                metrics_source=usage.get("metrics_source"),
                session_id=session_id,
                user_id=user_id,
                project_id=project_id,
                agent_name=agent_name,
                request_type="chat",
            )

    def _required_command_tool_name(self, user_input: str) -> Optional[str]:
        if "web_search" in set(
            getattr(self, "current_command_capabilities", ()) or ()
        ):
            return "web_search"
        if command_capability_active(user_input, "web_search"):
            return "web_search"
        return None

    def _agent_requiring_tool(self, agent: Agent, required_tool_name: str) -> Agent:
        required_tools = [
            tool
            for tool in agent.tools
            if str(getattr(tool, "name", "")) == required_tool_name
        ]
        return Agent(
            name=agent.name,
            instructions=agent.instructions,
            tools=required_tools,
            model=agent.model,
            model_settings=ModelSettings(
                tool_choice="required",
                reasoning=agent.model_settings.reasoning,
            ),
        )

    async def _build_tool_hint_context(self, user_input: str) -> str:
        if not getattr(self, "_native_tools_enabled", True):
            return ""
        return await build_tool_hint_context_async(
            user_input=user_input,
            registry=filtered_registry_for_client(self, self._tool_registry),
            policy=self._snapshot_generation_policy(),
            log_prefix="AgentLLMClient",
        )

    async def _run_agentic_completion_loop(
        self,
        agent: Agent,
        context: Any,
        stream_callback: Optional[StreamCallback] = None,
        user_input: str | None = None,
    ) -> str:
        """Run the review loop with task-local native result provenance."""
        snapshot_token = None
        if _native_turn_result_snapshot.get() is None:
            snapshot_token = _native_turn_result_snapshot.set({})
        try:
            return await self._run_agentic_completion_loop_impl(
                agent,
                context,
                stream_callback,
                user_input,
            )
        finally:
            if snapshot_token is not None:
                _native_turn_result_snapshot.reset(snapshot_token)

    def _last_turn_tool_records_for_review(self) -> list[Any]:
        turn_snapshot = _native_turn_result_snapshot.get()
        if turn_snapshot is not None and "tool_records" in turn_snapshot:
            return list(turn_snapshot.get("tool_records") or [])
        return list(getattr(self, "_last_turn_tool_records", None) or [])

    async def _run_agentic_completion_loop_impl(
        self,
        agent: Agent,
        context: Any,
        stream_callback: Optional[StreamCallback] = None,
        user_input: str | None = None,
    ) -> str:
        run_generation_policy = self._snapshot_generation_policy()
        policy_client = self._generation_policy_client(run_generation_policy)
        if not getattr(self, "_native_tools_enabled", True):
            return await self._run_once_with_agent(
                agent,
                context,
                stream_callback,
                generation_policy=run_generation_policy,
            )
        if (
            not agentic_completion_enabled(
                policy_client,
                user_input,
            )
            or (not isinstance(context, str) and not user_input)
        ):
            return await self._run_once_with_agent(
                agent,
                context,
                stream_callback,
                generation_policy=run_generation_policy,
            )

        review_context = (
            context
            if isinstance(context, str)
            else render_messages_for_review(
                [
                    dict(message)
                    for message in context
                    if isinstance(message, dict)
                ]
            )
            if isinstance(context, list)
            else str(context)
        )
        initial_context_pending = not isinstance(context, str)
        latest_work_tool_records: list[Any] = []
        latest_work_completion_confirmed = False

        def capture_latest_work_tool_records() -> None:
            nonlocal latest_work_tool_records, latest_work_completion_confirmed
            latest_work_tool_records.extend(
                self._last_turn_tool_records_for_review()
            )
            # Completion provenance must come from this logical turn's
            # ContextVar snapshot, never from the shared client's mutable
            # compatibility fields.
            turn_snapshot = _native_turn_result_snapshot.get()
            latest_work_completion_confirmed = bool(
                turn_snapshot is not None
                and turn_snapshot.get(
                    "tool_loop_completion_confirmed",
                    False,
                )
            )

        review_state_attributes = (
            "_last_turn_tool_records",
            "_last_tool_calls",
            "_last_audit_tool_calls",
            "_last_context_snapshots",
            "_last_usage_records",
            "_last_usage",
            "_last_agentic_events",
            "_last_model_transcript",
            "_last_tool_loop_messages",
            "_last_tool_loop_completion_confirmed",
        )

        async def _run_once(prompt: str) -> str:
            nonlocal initial_context_pending
            run_context: Any
            if initial_context_pending:
                run_context = context
                initial_context_pending = False
            else:
                run_context = prompt
            response = await self._run_once_with_agent(
                agent,
                run_context,
                None,
                generation_policy=run_generation_policy,
            )
            capture_latest_work_tool_records()
            return response

        async def _run_review_once(prompt: str) -> str:
            agent_model_settings = getattr(agent, "model_settings", None)
            review_agent = Agent(
                name=f"{getattr(agent, 'name', 'MainAssistant')}CompletionVerifier",
                instructions=getattr(agent, "instructions", ""),
                tools=[],
                model=getattr(agent, "model", getattr(self, "model_name", "")),
                model_settings=ModelSettings(
                    reasoning=getattr(agent_model_settings, "reasoning", None),
                ),
            )
            saved_state = {
                attribute: copy.deepcopy(getattr(self, attribute))
                for attribute in review_state_attributes
                if hasattr(self, attribute)
            }
            # The verifier runs inside the same task-local native turn
            # ContextVar as the work response.  _run_once_with_agent updates
            # that snapshot with its own transcript/tool metadata, so preserve
            # the work snapshot just like the shared compatibility fields.
            turn_snapshot = _native_turn_result_snapshot.get()
            saved_turn_snapshot = (
                dict(turn_snapshot) if turn_snapshot is not None else None
            )
            try:
                return await self._run_once_with_agent(
                    review_agent,
                    prompt,
                    None,
                    generation_policy=run_generation_policy,
                )
            finally:
                for attribute, value in saved_state.items():
                    setattr(self, attribute, value)
                if (
                    turn_snapshot is not None
                    and saved_turn_snapshot is not None
                ):
                    turn_snapshot.clear()
                    turn_snapshot.update(saved_turn_snapshot)

        async def _run_continuation_once(prompt: str) -> str:
            missing_mutations = required_project_mutation_tools_missing(
                user_input,
                latest_work_tool_records,
            )
            available_names = {
                str(getattr(tool, "name", "") or "")
                for tool in getattr(agent, "tools", ())
            }
            continuation_agent = agent
            required_tool_name = None
            if (
                len(missing_mutations) == 1
                and missing_mutations[0] in available_names
            ):
                required_tool_name = missing_mutations[0]
                continuation_agent = self._agent_requiring_tool(
                    agent,
                    required_tool_name,
                )

            if required_tool_name is None:
                response = await self._run_once_with_agent(
                    continuation_agent,
                    prompt,
                    None,
                    generation_policy=run_generation_policy,
                )
            else:
                response = await self._run_once_with_agent(
                    continuation_agent,
                    prompt,
                    None,
                    required_tool_name=required_tool_name,
                    generation_policy=run_generation_policy,
                )
            capture_latest_work_tool_records()
            return response

        response = await run_agentic_completion_loop_async(
            # The outer review loop reads profile/config after awaits.  Pass a
            # turn-local immutable-shaped view so another concurrent turn
            # cannot downgrade this run's work budget to chat.
            client=policy_client,
            run_once=_run_once,
            run_review_once=_run_review_once,
            run_continuation_once=_run_continuation_once,
            context=review_context,
            stream_callback=stream_callback,
            user_input=user_input,
            tool_evidence_provider=lambda: format_tool_execution_evidence(
                latest_work_tool_records
            ),
            audit_tool_calls_provider=lambda: list(
                latest_work_tool_records
            ),
            completion_confirmed_provider=lambda: (
                latest_work_completion_confirmed
            ),
        )
        self._last_turn_tool_records = list(latest_work_tool_records)
        turn_snapshot = _native_turn_result_snapshot.get()
        if turn_snapshot is not None:
            turn_snapshot["tool_records"] = list(latest_work_tool_records)
            turn_snapshot["tool_loop_completion_confirmed"] = (
                latest_work_completion_confirmed
            )
        return response

    def _format_last_turn_tool_evidence(self) -> str:
        """直前 work ラウンドのツール実行記録をレビュー用証跡テキストに整形する。"""
        turn_snapshot = _native_turn_result_snapshot.get()
        if turn_snapshot is not None and "tool_records" in turn_snapshot:
            # The review loop and the runner share this task-local context, so
            # another concurrent turn cannot replace the evidence between the
            # runner result and this read.
            records = turn_snapshot.get("tool_records") or []
        else:
            # Preserve compatibility for callers that invoke this formatter
            # outside a native turn scope.
            records = getattr(self, "_last_turn_tool_records", None) or []
        return format_tool_execution_evidence(records)

    async def _generate_async(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
        image_data: dict | None = None,
    ) -> str:
        """Capture turn policy before waiting, then serialize the full turn."""
        generation_policy = get_client_generation_policy(self)
        policy_token = _native_generation_policy_snapshot.set(generation_policy)
        try:
            async with self._native_generation_lock_scope():
                return await self._generate_async_impl(
                    user_input,
                    stream_callback=stream_callback,
                    steering_callback=steering_callback,
                    image_data=image_data,
                )
        finally:
            _native_generation_policy_snapshot.reset(policy_token)

    async def _generate_async_impl(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
        image_data: dict | None = None,
    ) -> str:
        """Generate response asynchronously using character agent with tools"""
        self.current_assistant_message_id = None
        native_turn_snapshot_token = _native_turn_result_snapshot.set({})
        # Agent Team delegates bind attempt-local ContextVars.  Establish a
        # clean baseline here so the finally block can restore the parent
        # generation context even when a provider worker outlives cancellation.
        # The old worker keeps its copied gate object (which is blocked on an
        # interrupt), while the parent task no longer carries that gate into a
        # retry/new continuation.
        generation_mutation_gate_token = set_current_generation_mutation_gate(
            None
        )
        continuation_state_token = set_current_continuation_state(None)
        project_token = None
        privacy_policy_token = None
        specialist_provider_token = set_runtime_specialist_provider(
            self.provider_label
        )
        tool_policy_token = set_current_user_input(user_input)
        # The public wrapper captures this before its first await and binds it
        # in a private ContextVar while waiting for the generation lock.
        generation_policy = self._snapshot_generation_policy()
        generation_policy_token = set_current_generation_policy(
            generation_policy
        )
        native_generation_policy_token = _native_generation_policy_snapshot.set(
            generation_policy
        )

        try:
            # Capture the profile above before this first await.  A turn that
            # waits for another native runner must keep its original policy
            # even if the shared client is switched to chat meanwhile.
            async with self._native_runner_lock_scope():
                # Do not let a new turn clear another task's compatibility
                # metadata while that task is still in its runner/review loop.
                self._last_context_snapshots = []
            project_context = await self._resolve_project_context()
            project_token = set_runtime_project_context(project_context)
            # Project-scoped skills/tools are intentionally discovered per turn so
            # edits made during the previous turn are visible without a restart.
            previous_registry_project = getattr(self, "_runtime_registry_project_id", None)
            registry_project = str((project_context or {}).get("id") or "") or None
            if project_context or previous_registry_project is not None:
                # ロード済み pack はレジストリ再構築後も同じ session オブジェクトを
                # 渡すことで維持される。
                trusted_parent_context = None
                if isinstance(project_context, dict):
                    try:
                        from ...services.agent_run_scope_service import (
                            TRUSTED_PARENT_CONTEXT_KEY,
                            resolve_trusted_parent_run_context,
                        )

                        trusted_parent_context = resolve_trusted_parent_run_context(
                            project_context.get(TRUSTED_PARENT_CONTEXT_KEY),
                            parent_run_id=get_current_agent_run_id(),
                        )
                    except Exception:
                        # Raw/model-provided scope fields are never promoted
                        # into a runtime authority.  The registry's own
                        # fail-closed guard handles write workers when the
                        # trusted marker is absent or mismatched.
                        trusted_parent_context = None
                self._tool_registry = build_runtime_tool_registry(
                    self.config,
                    project_context=project_context,
                    client=self,
                    trusted_parent_context=trusted_parent_context,
                )
                ensure_load_tool_pack_tool(self._tool_registry, self)
                self.agent = self._create_character_agent()
            self._runtime_registry_project_id = registry_project
            self._current_context_bundle = await self._build_context_bundle_for_prompt(
                user_input, project_context
            )

            if self.memory_manager and not self.memory_manager.is_initialized():
                await self.memory_manager.initialize()

            await self._sync_history_with_current_session()

            # Bind the effective policy before any reasoning/specialist/tool
            # child is spawned.  Contextvars make this inherited by nested
            # Agent Team calls while keeping concurrent sessions isolated.
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            # Lightweight compatibility callers (and a few integrations that
            # invoke ``_generate_async`` with a stub client) may not construct
            # the native runner at all.  The native runner is only needed by
            # the eventual provider call, so keep policy binding resilient and
            # skip runner-specific updates when it is absent.
            turn_runner = getattr(self, "_turn_runner", None)
            privacy_gateway = getattr(turn_runner, "privacy_gateway", None)
            if isinstance(privacy_gateway, OutboundPrivacyGateway):
                privacy_gateway.update_policy_context(
                    session_context=getattr(self, "_privacy_session_context", None),
                    project_metadata=self._privacy_project_metadata,
                )
            privacy_policy_token = set_privacy_policy_context(
                session_context=getattr(self, "_privacy_session_context", None),
                project_metadata=self._privacy_project_metadata,
            )

            external_persistence = bool(
                getattr(self, "external_persistence_enabled", False)
            )

            if self.memory_manager and not external_persistence:
                try:
                    if self.current_session_id:
                        await self.memory_manager.add_message_to_session(
                            session_id=self.current_session_id,
                            role="user",
                            content=user_input,
                            metadata=self._get_memory_metadata(),
                            branch_from_message_id=getattr(
                                self, "current_edit_message_id", None
                            ),
                        )
                except Exception as e:
                    print(
                        f"[AgentLLMClient] Failed to save user message to memory: {e}"
                    )

            async with self._native_runner_lock_scope():
                self.history_manager.add_message("user", user_input)

            # Keep role boundaries intact.  The legacy text rendering remains
            # available for diagnostics and compatibility callers, but is no
            # longer used as the model transcript.
            memory_recall_started = time.perf_counter()
            memory_recall = await self._build_past_conversation_recall(user_input)
            self._current_memory_recall_duration_ms = (
                time.perf_counter() - memory_recall_started
            ) * 1000
            tool_hint_started = time.perf_counter()
            tool_hint_context = await self._build_tool_hint_context(
                user_input
            )
            self._current_tool_hint_duration_ms = (
                time.perf_counter() - tool_hint_started
            ) * 1000
            context = self._build_model_prompt_messages(
                user_input,
                tool_hint_context=tool_hint_context,
                memory_recall=memory_recall,
                project_context=project_context,
            )
            response: Optional[str] = None
            required_tool_name = self._required_command_tool_name(user_input)
            try:
                available_tools = self._get_available_tools()
            except (AttributeError, TypeError):
                available_tools = []

            if (
                not required_tool_name
                and self.reasoning_manager
                and self._legacy_reasoning_manager_allowed_for_turn(
                    available_tools
                )
                and self.reasoning_manager.is_reasoning_required(
                    user_input,
                    available_tools,
                )
            ):
                print("[AgentLLMClient] Entering reasoning mode")

                progress_callback = stream_callback
                if stream_callback:
                    await stream_callback(
                        "stream_start",
                        {
                            "status": "reasoning",
                            "message": "方針を整理しています",
                        },
                    )

                response = await self.reasoning_manager.execute_reasoning_mode(
                    user_input=user_input,
                    context={
                        "available_tools": available_tools,
                        "conversation_history": self.history_manager.get_all(),
                        "character_name": self.character_name,
                        "project_context": (
                            sanitize_project_context_for_chat(
                                get_runtime_project_context()
                            )
                            if self._project_context_enabled_for_turn()
                            else None
                        ),
                        "runtime_context": (
                            "\n\n".join(
                                part
                                for part in [
                                    tool_hint_context,
                                    (
                                        self._context_bundle_for_turn(
                                            self._project_context_enabled_for_turn()
                                        ).render_for_prompt()
                                        if self._current_context_bundle
                                        else ""
                                    ),
                                ]
                                if part
                            )
                        ),
                    },
                    progress_callback=progress_callback,
                    steering_callback=steering_callback,
                )

                if stream_callback:
                    await stream_callback("stream_end", {"content": response})

                if self.memory_manager and not external_persistence:
                    try:
                        if self.current_session_id:
                            await self.memory_manager.add_message_to_session(
                                session_id=self.current_session_id,
                                role="assistant",
                                content=response,
                                metadata=self._get_memory_metadata(),
                            )
                    except Exception as e:
                        print(
                            f"[AgentLLMClient] Failed to save assistant message to memory: {e}"
                        )

            if response is not None:
                async with self._native_runner_lock_scope():
                    self.history_manager.add_message("assistant", response)
                    self._last_model_transcript = [
                        *self.history_manager.get_model_messages(),
                        {"role": "assistant", "content": response},
                    ]
                    self.history_manager.set_model_messages(
                        self._last_model_transcript
                    )
                return response

            try:
                try:
                    story_chat_context = self._get_story_chat_context_sync()
                except StoryChatContextBuildError as story_context_error:
                    error_message = (
                        "執筆文脈の構築に失敗しました。Story Studio の設定や章データを確認してから再試行してください。"
                        f"（{story_context_error}）"
                    )
                    if stream_callback:
                        await stream_callback(
                            "stream_start",
                            {"status": "error", "message": error_message},
                        )
                        await stream_callback(
                            "stream_end",
                            {"content": error_message},
                        )
                    async with self._native_runner_lock_scope():
                        self.history_manager.add_message("assistant", error_message)
                    return error_message
                # An explicit specialist-role request is a routing contract,
                # not merely a hint for the model.  In particular an
                # ambiguous Docs mutation must still run through the
                # docs_operator child so that the child can apply its
                # no-guess/no-write policy.  Ensure the Agent Team
                # pack for this generic role signal, expose only the delegate
                # function, and require that function on the root turn.  This
                # prevents a model from answering (or asking clarification)
                # directly while still preserving the normal dynamic
                # load/registry/dispatch path for ordinary turns.
                force_docs_specialist = bool(
                    looks_like_docs_agent_delegation_request(user_input)
                    and _docs_agent_delegation_available(self.config)
                )
                if force_docs_specialist:
                    pack_session = tool_pack_session_for_client(self)
                    pack_session.load("agent_team")
                effective_agent = Agent(
                    name=(
                        story_chat_context.agent_name
                        if story_chat_context
                        else self.agent.name
                    ),
                    instructions=self._build_effective_instructions(
                        story_chat_context
                    ),
                    tools=self._get_effective_tools_for_current_session(
                        story_chat_context
                    ),
                    model=self.model_name,
                    model_settings=self.agent.model_settings,
                )
                required_specialist_tool = None
                if force_docs_specialist:
                    delegate_tool = next(
                        (
                            tool
                            for tool in effective_agent.tools
                            if str(getattr(tool, "name", ""))
                            == "agent_team_delegate"
                        ),
                        None,
                    )
                    if delegate_tool is not None:
                        # Keep the manual team loader alongside the delegate
                        # function.  The pack session controls schema
                        # tool exposure, while the team loader owns the separate
                        # per-turn team activation state checked by the
                        # delegate implementation.  Requiring the delegate
                        # (rather than the loader) lets the native loop call
                        # load_agent_team first and then continue to the
                        # actual child run.
                        team_loader = next(
                            (
                                tool
                                for tool in effective_agent.tools
                                if str(getattr(tool, "name", ""))
                                == "load_agent_team"
                            ),
                            None,
                        )
                        # Keep the declared reasoning preference while making
                        # the Subagent delegate (and its manual team loader) the
                        # only callable surface on this turn.
                        effective_agent = Agent(
                            name=effective_agent.name,
                            instructions=effective_agent.instructions,
                            tools=[
                                tool
                                for tool in (team_loader, delegate_tool)
                                if tool is not None
                            ],
                            model=effective_agent.model,
                            model_settings=ModelSettings(
                                tool_choice="required",
                                reasoning=getattr(
                                    effective_agent.model_settings,
                                    "reasoning",
                                    None,
                                ),
                            ),
                        )
                        required_specialist_tool = "agent_team_delegate"
                if required_tool_name:
                    effective_agent = self._agent_requiring_tool(
                        effective_agent,
                        required_tool_name,
                    )
                context = await self._append_pending_steering_to_context(
                    context, steering_callback, stream_callback
                )
                if required_tool_name:
                    response = await self._run_once_with_agent(
                        effective_agent,
                        self._add_image_to_prompt_messages(context, image_data),
                        stream_callback,
                        required_tool_name=required_tool_name,
                    )
                elif required_specialist_tool:
                    response = await self._run_once_with_agent(
                        effective_agent,
                        self._add_image_to_prompt_messages(context, image_data),
                        stream_callback,
                        required_tool_name=required_specialist_tool,
                    )
                else:
                    response = await self._run_agentic_completion_loop(
                        effective_agent,
                        self._add_image_to_prompt_messages(context, image_data),
                        stream_callback,
                        user_input=user_input,
                    )
            except Exception as e:
                print(f"[AgentLLMClient] native runtime実行エラー: {e}")
                import traceback

                traceback.print_exc()
                raise

            image_event = None
            scene_description = self._extract_scene_description(response or "")
            existing = getattr(self, "current_assistant_message_id", None)
            if not existing:
                self.current_assistant_message_id = str(uuid.uuid4())
            assistant_message_id = self.current_assistant_message_id
            if scene_description:
                visible_response = self._strip_scene_description_markers(response or "")
                image_event = await self._generate_scene_image_async(
                    scene_description,
                    message_id=assistant_message_id,
                )
                response = visible_response or response
                if image_event and image_event.get("tag"):
                    response = f"{response}\n\n{image_event['tag']}".strip()
                    if stream_callback:
                        payload = {
                            **image_event,
                            "session_id": getattr(self, "current_session_id", None),
                            "message_id": assistant_message_id,
                            "agent_run_id": get_current_agent_run_id(),
                        }
                        await stream_callback("generated_image", payload)

            if self.memory_manager and not external_persistence:
                try:
                    if self.current_session_id:
                        await self.memory_manager.add_message_to_session(
                            session_id=self.current_session_id,
                            role="assistant",
                            content=response,
                            metadata=self._get_memory_metadata(),
                            message_id=assistant_message_id,
                        )
                except Exception as e:
                    print(
                        f"[AgentLLMClient] Failed to save assistant message to memory: {e}"
                    )

            turn_snapshot = _native_turn_result_snapshot.get()
            async with self._native_runner_lock_scope():
                self.history_manager.add_message("assistant", response)
                # A native result already carries its model transcript.  Use
                # the task-local copy when available rather than whatever a
                # concurrent turn most recently stored in ``_last_*``.
                turn_model_transcript = (
                    turn_snapshot.get("model_transcript")
                    if turn_snapshot is not None
                    else None
                )
                if turn_model_transcript:
                    self._last_model_transcript = [
                        dict(message) for message in turn_model_transcript
                    ]
                    active_model_transcript = turn_snapshot.get(
                        "active_model_transcript"
                    ) or compact_model_transcript_for_history(
                        self._last_model_transcript,
                        getattr(self, "config", None),
                    )
                    self.history_manager.set_model_messages(
                        active_model_transcript
                    )
                elif not self._last_model_transcript:
                    self._last_model_transcript = [
                        *self.history_manager.get_model_messages(),
                        {"role": "assistant", "content": response},
                    ]
                    self.history_manager.set_model_messages(
                        self._last_model_transcript
                    )

                self.check_and_summarize_history(self.history_manager)

            return response
        except GenerationInterrupted:
            # The bounded continuation snapshot is attached to the exception
            # by the Agent Team delegate before the attempt-local state is
            # reset below.  Keep the exception on the original path while the
            # response handler builds the retry prompt from that snapshot.
            raise
        finally:
            _native_turn_result_snapshot.reset(native_turn_snapshot_token)
            _native_generation_policy_snapshot.reset(native_generation_policy_token)
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            reset_runtime_specialist_provider(specialist_provider_token)
            reset_current_generation_mutation_gate(generation_mutation_gate_token)
            reset_current_continuation_state(continuation_state_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            if privacy_policy_token is not None:
                reset_privacy_policy_context(privacy_policy_token)
            self._current_context_bundle = None

    async def _run_streamed_with_callback(
        self, agent: Agent, context: str, callback: StreamCallback
    ) -> str:
        """Native runtime streaming callback bridge."""
        # This bridge is a separate native-runner entry used by capability
        # based callers.  Keep its budget semantics identical to
        # ``_run_once_with_agent`` and always restore the runner default after
        # the call, including cancellation/error paths.
        run_generation_policy = self._snapshot_generation_policy()
        native_tool_round_budget = self._native_tool_round_budget(
            generation_policy=run_generation_policy,
        )
        privacy_policy_token = set_privacy_policy_context(
            session_context=getattr(self, "_privacy_session_context", None),
            project_metadata=getattr(self, "_privacy_project_metadata", None),
        )
        privacy_gateway = getattr(self._turn_runner, "privacy_gateway", None)
        if isinstance(privacy_gateway, OutboundPrivacyGateway):
            privacy_gateway.update_policy_context(
                session_context=getattr(self, "_privacy_session_context", None),
                project_metadata=getattr(self, "_privacy_project_metadata", None),
            )
        snapshot_token = None
        if _native_turn_result_snapshot.get() is None:
            snapshot_token = _native_turn_result_snapshot.set({})
        try:
            async with self._native_runner_lock_scope():
                _configure_turn_context_snapshot(self, self._turn_runner)
                turn_snapshot = _native_turn_result_snapshot.get()
                if turn_snapshot is not None:
                    turn_snapshot["usage_metadata"] = {
                        "provider_label": str(
                            getattr(self, "provider_label", "openai") or "openai"
                        ),
                        "requested_model": str(agent.model or self.model_name),
                        "session_id": getattr(self, "current_session_id", None),
                        "user_id": self._get_session_user_id(),
                        "project_id": getattr(self, "current_project_id", None),
                        "agent_name": str(agent.name or self.character_name),
                    }
                previous_max_tool_rounds = getattr(
                    self._turn_runner,
                    "max_tool_rounds",
                    None,
                )
                if previous_max_tool_rounds is not None:
                    self._turn_runner.max_tool_rounds = max(
                        previous_max_tool_rounds,
                        native_tool_round_budget,
                    )
                self._last_generation_failure = None
                try:
                    result = await self._turn_runner.run(
                        agent,
                        context,
                        stream_callback=callback,
                    )
                finally:
                    if previous_max_tool_rounds is not None:
                        self._turn_runner.max_tool_rounds = previous_max_tool_rounds
            result_context_snapshots = list(
                getattr(result, "context_snapshots", None) or []
            )
            self._last_generation_failure = getattr(
                result,
                "generation_failure",
                None,
            )
            result_tool_records = list(getattr(result, "tool_calls", None) or [])
            turn_snapshot = _native_turn_result_snapshot.get()
            if turn_snapshot is not None:
                turn_snapshot.update(
                    {
                        "context_snapshots": list(result_context_snapshots),
                        "tool_records": list(result_tool_records),
                        "tool_rounds_exhausted": bool(
                            getattr(result, "tool_rounds_exhausted", False)
                        ),
                        "usage_records": [
                            dict(item)
                            for item in (getattr(result, "usage_records", None) or [])
                            if isinstance(item, dict)
                        ],
                    }
                )
            await self._record_native_usage(result, agent)
            async with self._native_runner_lock_scope():
                self._last_context_snapshots = list(result_context_snapshots)
                self._last_turn_tool_records = list(result_tool_records)
                self._last_turn_tool_rounds_exhausted = bool(
                    getattr(result, "tool_rounds_exhausted", False)
                )
            return result.final_output
        finally:
            if snapshot_token is not None:
                _native_turn_result_snapshot.reset(snapshot_token)
            reset_privacy_policy_context(privacy_policy_token)

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        # Keep the effective Main route alongside existing turn metadata.  The
        # value is additive (no DB schema change) and lets title/memory/chat
        # records be audited without consulting provider-specific defaults.
        metadata["route"] = {
            "provider": str(getattr(self, "provider_label", "") or "").strip().lower() or None,
            "model": str(getattr(self, "model_name", "") or "").strip() or None,
            "route_source": str(
                getattr(self, "_route_source", "main_inherit") or "main_inherit"
            ).strip(),
        }
        if self._last_context_snapshots:
            bounded_snapshot = sanitized_snapshot_series(
                self._last_context_snapshots
            )
            if bounded_snapshot:
                metadata["context_snapshot"] = bounded_snapshot
        if self._last_model_transcript:
            metadata["model_transcript"] = redact_sensitive_model_transcript(
                [
                    dict(message)
                    for message in self._last_model_transcript
                    if isinstance(message, dict)
                ]
            )
        metadata["conversation_state"] = {
            "mode": self._provider_state_mode,
            "provider_managed": self._provider_state_mode == "provider-managed",
            "active": bool(self._provider_state.get("previous_response_id")),
            "previous_response_id_valid": bool(
                self._provider_state.get("previous_response_id")
            ),
        }
        if getattr(self, "_last_usage_records", None):
            totals: Dict[str, float] = {}
            for record in self._last_usage_records:
                for key, value in record.items():
                    if isinstance(value, (int, float)):
                        totals[key] = totals.get(key, 0) + value
            totals["requests"] = len(self._last_usage_records)
            metadata["cache_usage"] = totals
        return metadata
