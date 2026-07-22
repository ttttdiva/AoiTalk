"""AgentLLMClient のターン実行・agentic completion・provider state 管理 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import asyncio
import inspect
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..native_runtime import (
    AgentDefinition as Agent,
    NativeModelSettings as ModelSettings,
)
from ..conversation_context import PromptMessages, ProviderState, stable_cache_key
from ..generation_policy import get_client_generation_policy
from ..agentic_completion import (
    agentic_completion_enabled,
    agentic_max_rounds,
    build_agentic_continuation_context,
    build_agentic_review_prompt,
    parse_agentic_review_decision,
    run_agentic_completion_loop_async,
)
from ..agent_runtime import build_tool_hint_context_async
from ..tool_policy import command_capability_active
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
from ..runtime_tool_registry import build_runtime_tool_registry

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[List[str]]]


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

    def _agentic_max_rounds(self, user_input: str | None = None) -> int:
        return agentic_max_rounds(self, user_input)

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
    ) -> str:
        from ..context_snapshot import context_bundle_components
        tool_schemas = [
            {
                "type": "function",
                "name": str(getattr(tool, "name", "")),
                "parameters": tool.to_json_schema(),
            }
            for tool in getattr(agent, "tools", [])
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
        state = ProviderState(
            mode=self._provider_state_mode,
            previous_response_id=self._provider_state.get("previous_response_id"),
            fingerprint=self._provider_state.get("fingerprint"),
        )
        if state.fingerprint and state.fingerprint != cache_key:
            state.reset()
        state.fingerprint = cache_key
        self._turn_runner.conversation_state_mode = self._provider_state_mode
        self._turn_runner.provider_state = state
        self._turn_runner.prompt_cache_key = cache_key
        self._turn_runner.prompt_cache_retention = (
            str(_config_get(self.config, "openai.prompt_cache_retention", "") or "").strip()
            or None
        )
        rendered_bundle, bundle_parts = context_bundle_components(getattr(self, "_current_context_bundle", None))
        self._turn_runner.snapshot_rendered_bundle = rendered_bundle
        self._turn_runner.snapshot_bundle_components = bundle_parts
        previous_max_tool_rounds = getattr(self._turn_runner, "max_tool_rounds", None)
        if previous_max_tool_rounds is not None:
            self._turn_runner.max_tool_rounds = max(
                previous_max_tool_rounds,
                self._agentic_max_rounds(context),
            )
        try:
            result = await self._turn_runner.run(
                agent,
                context,
                stream_callback=stream_callback,
            )
        finally:
            if previous_max_tool_rounds is not None:
                self._turn_runner.max_tool_rounds = previous_max_tool_rounds
        state_mode_before = self._provider_state_mode
        if (
            self._provider_state_mode == "provider-managed"
            and getattr(self._turn_runner, "conversation_state_mode", "") == "stateless"
        ):
            # The provider rejected/expired managed state.  Keep subsequent
            # turns stateless until the client/session is explicitly reset.
            self._provider_state_mode = "stateless"
        self._provider_state = {
            "previous_response_id": state.previous_response_id,
            "fingerprint": state.fingerprint,
        }
        state_was_invalidated = (
            state_mode_before == "provider-managed"
            and
            getattr(self._turn_runner, "conversation_state_mode", "") == "stateless"
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
        if self.current_session_id and self.memory_manager and self._provider_state_mode == "provider-managed":
            try:
                await self.memory_manager.repository.update_session_context(
                    self.current_session_id,
                    {"llm_provider_state": dict(self._provider_state)},
                )
            except Exception:
                logger.warning("provider-managed stateの永続化に失敗しました", exc_info=True)
        await self._record_native_usage(result, agent)
        self._last_context_snapshots = list(getattr(result, "context_snapshots", None) or [])[-8:]
        self._last_turn_tool_records = list(result.tool_calls)
        self._last_model_transcript = [
            dict(message)
            for message in (getattr(result, "messages", None) or [])
            if isinstance(message, dict) and message.get("role") in {"user", "assistant", "tool"}
        ]
        self._last_usage_records = [dict(item) for item in (getattr(result, "usage_records", None) or [])]
        if hasattr(self.history_manager, "set_model_messages"):
            self.history_manager.set_model_messages(self._last_model_transcript)
        if result.tool_calls:
            print(f"[AgentLLMClient] Tool messages found: {len(result.tool_calls)}")
        if required_tool_name and not any(
            record.tool == required_tool_name and record.successful
            for record in result.tool_calls
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
        for usage in records:
            await service.record_usage(
                provider=str(getattr(self, "provider_label", "openai") or "openai"),
                model=str(agent.model or self.model_name),
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
                session_id=_uuid_or_none(self.current_session_id),
                user_id=self._get_session_user_id(),
                project_id=_uuid_or_none(self.current_project_id),
                agent_name=str(agent.name or self.character_name),
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
            registry=self._tool_registry,
            policy=get_client_generation_policy(self),
            log_prefix="AgentLLMClient",
        )

    async def _run_agentic_completion_loop(
        self,
        agent: Agent,
        context: Any,
        stream_callback: Optional[StreamCallback] = None,
        user_input: str | None = None,
    ) -> str:
        if not isinstance(context, str) or not self._agentic_completion_enabled(user_input):
            return await self._run_once_with_agent(agent, context, stream_callback)

        async def _run_once(prompt: str) -> str:
            return await self._run_once_with_agent(agent, prompt, None)

        return await run_agentic_completion_loop_async(
            client=self,
            run_once=_run_once,
            context=context,
            stream_callback=stream_callback,
            user_input=user_input,
            tool_evidence_provider=self._format_last_turn_tool_evidence,
        )

    def _format_last_turn_tool_evidence(self) -> str:
        """直前 work ラウンドのツール実行記録をレビュー用証跡テキストに整形する。"""
        import json as _json

        records = getattr(self, "_last_turn_tool_records", None) or []
        lines: list[str] = []
        total = 0
        for record in records:
            try:
                arguments = _json.dumps(
                    dict(record.arguments or {}), ensure_ascii=False
                )
            except Exception:  # noqa: BLE001
                arguments = str(record.arguments)
            if len(arguments) > 200:
                arguments = arguments[:200] + "…"
            result_text = str(record.result or "").strip()
            if len(result_text) > 500:
                result_text = result_text[:500] + "…"
            line = f"- {record.tool}({arguments}) -> {result_text}"
            if total + len(line) > 4000:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    async def _generate_async(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
        image_data: dict | None = None,
    ) -> str:
        """Generate response asynchronously using character agent with tools"""
        self._last_context_snapshots = []
        project_token = None
        specialist_provider_token = set_runtime_specialist_provider(
            self.provider_label
        )
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )

        try:
            project_context = await self._resolve_project_context()
            project_token = set_runtime_project_context(project_context)
            # Project-scoped skills/tools are intentionally discovered per turn so
            # edits made during the previous turn are visible without a restart.
            previous_registry_project = getattr(self, "_runtime_registry_project_id", None)
            registry_project = str((project_context or {}).get("id") or "") or None
            if project_context or previous_registry_project is not None:
                self._tool_registry = build_runtime_tool_registry(
                    self.config,
                    project_context=project_context,
                )
                self.agent = self._create_character_agent()
            self._runtime_registry_project_id = registry_project
            self._current_context_bundle = await self._build_context_bundle_for_prompt(
                user_input, project_context
            )

            if self.memory_manager and not self.memory_manager.is_initialized():
                await self.memory_manager.initialize()

            await self._sync_history_with_current_session()

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

            self.history_manager.add_message("user", user_input)

            # Keep role boundaries intact.  The legacy text rendering remains
            # available for diagnostics and compatibility callers, but is no
            # longer used as the model transcript.
            memory_recall = await self._build_past_conversation_recall(user_input)
            tool_hint_context = await self._build_tool_hint_context(
                user_input
            )
            context = self._build_model_prompt_messages(
                user_input,
                tool_hint_context=tool_hint_context,
                memory_recall=memory_recall,
                project_context=project_context,
            )
            response: Optional[str] = None
            required_tool_name = self._required_command_tool_name(user_input)

            if (
                not required_tool_name
                and self.reasoning_manager
                and self.reasoning_manager.is_reasoning_required(
                    user_input,
                    self._get_available_tools(),
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
                        "available_tools": self._get_available_tools(),
                        "conversation_history": self.history_manager.get_all(),
                        "character_name": self.character_name,
                        "project_context": sanitize_project_context_for_chat(
                            get_runtime_project_context()
                        ),
                        "runtime_context": (
                            "\n\n".join(
                                part
                                for part in [
                                    tool_hint_context,
                                    (
                                        self._current_context_bundle.render_for_prompt()
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
                self.history_manager.add_message("assistant", response)
                self._last_model_transcript = [
                    *self.history_manager.get_model_messages(),
                    {"role": "assistant", "content": response},
                ]
                self.history_manager.set_model_messages(self._last_model_transcript)
                return response

            try:
                scenario_chat_context = self._get_scenario_chat_context_sync()
                effective_agent = Agent(
                    name=(
                        scenario_chat_context.agent_name
                        if scenario_chat_context
                        else self.agent.name
                    ),
                    instructions=self._build_effective_instructions(
                        scenario_chat_context
                    ),
                    tools=self._get_effective_tools_for_current_session(
                        scenario_chat_context
                    ),
                    model=self.model_name,
                    model_settings=self.agent.model_settings,
                )
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
            if scene_description:
                visible_response = self._strip_scene_description_markers(response or "")
                image_event = await self._generate_scene_image_async(scene_description)
                response = visible_response or response
                if image_event and image_event.get("tag"):
                    response = f"{response}\n\n{image_event['tag']}".strip()
                    if stream_callback:
                        await stream_callback("generated_image", image_event)

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

            self.history_manager.add_message("assistant", response)
            if not self._last_model_transcript:
                self._last_model_transcript = [
                    *self.history_manager.get_model_messages(),
                    {"role": "assistant", "content": response},
                ]
                self.history_manager.set_model_messages(self._last_model_transcript)

            self.check_and_summarize_history(self.history_manager)

            return response
        finally:
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            reset_runtime_specialist_provider(specialist_provider_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            self._current_context_bundle = None

    async def _run_streamed_with_callback(
        self, agent: Agent, context: str, callback: StreamCallback
    ) -> str:
        """Native runtime streaming callback bridge."""
        from ..context_snapshot import context_bundle_components
        rendered_bundle, bundle_parts = context_bundle_components(getattr(self, "_current_context_bundle", None))
        self._turn_runner.snapshot_rendered_bundle = rendered_bundle
        self._turn_runner.snapshot_bundle_components = bundle_parts
        result = await self._turn_runner.run(
            agent,
            context,
            stream_callback=callback,
        )
        await self._record_native_usage(result, agent)
        self._last_context_snapshots = list(getattr(result, "context_snapshots", None) or [])[-8:]
        return result.final_output

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        if self._last_context_snapshots:
            latest = dict(self._last_context_snapshots[-1])
            latest["requests"] = [dict(item) for item in self._last_context_snapshots]
            metadata["context_snapshot"] = latest
        if self._last_model_transcript:
            metadata["model_transcript"] = [
                dict(message) for message in self._last_model_transcript
            ]
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
