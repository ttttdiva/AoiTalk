"""
Terminal mode for AoiTalk Voice Assistant Framework
"""

import asyncio
import base64
import copy
import hashlib
import inspect
import json
import re
import time
import tempfile
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Dict, Optional
from uuid import UUID, uuid4
from ..base import BaseAssistant
from ..response_handler import ResponseHandler
from ..chat_turn_persistence import (
    ChatTurnPersistence,
    apply_turn_user_context_to_client,
    reset_turn_generation_metadata,
    restore_turn_user_context_on_client,
    without_cached_generation_metadata,
)
from ..chat_attachment_utils import (
    build_message_with_attachment_context,
    sanitize_chat_attachments,
)
from ..conversation_title_events import maybe_generate_and_broadcast_session_title
from ...llm.context_budget import clip_text
from ...llm.agentic_completion import response_looks_like_unfinished_work
from ...llm.generation_policy import (
    generation_policy_for_profile,
    resolve_generation_profile,
)
from ...llm.generation_error import empty_response_failure
from ...llm.generation_cancellation import (
    get_current_generation_cancellation,
    PlanningInteractionTerminated,
)
from ...llm.tool_policy import (
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    protect_untrusted_command_context,
    sanitize_command_capabilities,
)
from ...runtime_features import runtime_feature_manager
from ...services.agent_run_service import (
    AgentRunService,
    get_current_agent_run_id,
    redact_sensitive_chat_metadata,
    sanitize_assistant_display_text,
    sanitize_stream_display_payload,
    reset_current_agent_run_id,
    set_current_agent_run_id,
)
from ...services.agent_team_service import agent_team_orchestration_mode
from ...services.outbound_privacy_service import OutboundPrivacyGateway
from ...services.turn_context import (
    override_turn_context,
    reset_turn_context,
    set_turn_context,
)
from ...utils.logging_config import FILE_ONLY_LOG_EXTRA


logger = logging.getLogger(__name__)


# 以下の AgentRun 系ヘルパ/定数と AgentRunEventEmitter は agent_run_events.py へ
# 挙動不変で移設した。外部(テスト含む)からの `terminal_mode.<name>` 参照を
# 維持するため、ここで再 export する。
from .agent_run_events import (  # noqa: F401
    AgentRunEventEmitter,
    AGENT_RUN_DELEGATION_TOOL_SUBAGENTS,
    _DOCS_SEARCH_HIT_RE,
    _PROVIDER_MODEL_KEYS,
    _SEARCH_TOOL_URL_RE,
    _SEARCH_URL_LIMIT,
    _agent_run_completion_failure_message,
    _agent_run_completion_result,
    _agent_run_tool_call_payload,
    _agent_run_tool_context,
    _agent_run_tool_operation_signature,
    _client_tool_calls,
    _config_text,
    _enrich_agent_run_event_payload,
    _extract_search_tool_urls,
    _looks_like_cli_execution_error,
    _main_agent_run_model,
    _main_agent_run_provider,
    _should_fail_agent_run_completion,
)
from .terminal_commands import TerminalCommandsMixin, WorkIntakeHandledError


# A trusted controller turn (for example ``/help``) still has to present an
# empty provider-local transcript to the model.  Long-lived legacy clients,
# however, may keep the ordinary transcript only in memory when no
# ``session_id`` is available.  Keep a data-only snapshot around the isolated
# turn so priming/cleanup cannot erase that transcript or provider continuation
# state.  Do not deepcopy client objects themselves: provider clients contain
# locks, threads, transports, and (for Free Team) an active target that must
# retain its identity.
_ISOLATED_PROVIDER_STATE_FIELDS = (
    "conversation_history",
    # Per-turn routing/policy fields are written after the snapshot is taken.
    # Restore them as well as provider transcript state when setup or
    # generation fails, otherwise a following ordinary turn can inherit the
    # Help scope/capability/session from the isolated request.
    "current_session_id",
    "current_project_id",
    "current_command_capabilities",
    "current_tool_required",
    "generation_policy",
    "planning_policy",
    "current_include_project_context",
    "current_edit_message_id",
    "current_response_model",
    "external_persistence_enabled",
    # ``apply_turn_user_context_to_client`` runs after the snapshot and can
    # change the provider actor, metadata, and effective system prompt.  Keep
    # those identity-bearing fields isolated too, including setup failures
    # that occur before its inner generation ``finally`` executes.
    "session_user_id",
    "session_metadata",
    "system_prompt",
    "_isolated_system_prompt_override",
    "_privacy_session_context",
    "_privacy_project_metadata",
    "_loaded_session_id",
    "_loaded_history_session_id",
    "_history_session_id",
    "_context_window_override_tokens",
    "_provider_state",
    "_provider_state_mode",
    "_model_transcript",
    "_last_model_transcript",
    "_history_authoritative_model_transcript",
    "_history_active_model_transcript",
    "_cli_native_session_reset_requested",
    "_cli_native_session_info",
    "_last_client",
    "_active_client",
    # Provider privacy gateways retain reversible alias maps.  Help gets a
    # fresh gateway for the duration of the isolated turn so a prior
    # ordinary request cannot rehydrate a secret-shaped alias in the Help
    # response; the original gateway object is restored afterwards.
    "_privacy_gateway",
    "_last_route_metadata",
    "_last_generation_metadata",
    # Local OpenAI-compatible/Ollama/SGLang providers derive this key from
    # the effective prompt and tool schemas.  Help must not leave its
    # Guide-only cache identity on a reused client after the outer snapshot is
    # restored.
    "_cache_key",
    # Provider-local turn ledgers/diagnostics.  SGLang and compatible local
    # clients keep retry evidence outside the generic history fields; an
    # isolated Help request must not consume or overwrite that state on a
    # reused client (including a direct low-level caller).
    "_last_usage",
    "_last_usage_run_id",
    # AgentLLMClient publishes these compatibility ledgers after the native
    # runner returns.  Help is a controller turn and must not replace the
    # previous ordinary turn's review/usage evidence on a reused client.
    "_last_turn_tool_records",
    "_last_usage_records",
    "_current_context_bundle",
    "_current_context_budget",
    "_current_tool_hint_context",
    "_current_memory_recall_duration_ms",
    "_current_tool_hint_duration_ms",
    "_context_request_index",
    "_last_generation_failure",
    "_last_tool_calls",
    "_last_tool_calls_run_id",
    "_pending_tool_turn_results",
    "_completed_tool_turn_results",
    "_steering_callback_run_ids",
    "_last_tool_loop_messages",
    "_last_context_snapshots",
    "_current_dynamic_context",
    "_current_dynamic_context_metadata",
    "_last_generation_metrics",
    "_last_agentic_events",
    "_last_audit_tool_calls",
    "_last_tool_loop_completion_confirmed",
    "_last_turn_tool_rounds_exhausted",
    "_last_turn_tool_loop_failed",
    "_last_cli_usage",
    "_agent_run_usage",
)
_ISOLATED_HISTORY_MANAGER_FIELDS = (
    "history",
    "model_history",
    "summary",
    "summary_version",
    "summary_checkpoint",
)


def _copy_isolated_provider_value(value: Any) -> Any:
    """Copy mutable transcript state while tolerating provider-owned values."""

    try:
        return copy.deepcopy(value)
    except Exception:
        # A provider may expose a non-copyable diagnostic object.  The fields
        # above are all replaced only for the duration of the isolated turn;
        # retaining the reference is safer than failing the user request.
        return value


def _snapshot_isolated_provider_state(llm_client: Any) -> dict[str, Any] | None:
    """Capture the provider-local state that an isolated turn may prime."""

    if llm_client is None:
        return None
    snapshot: dict[str, Any] = {"client": llm_client, "attributes": {}}
    attributes: dict[str, Any] = snapshot["attributes"]
    for name in _ISOLATED_PROVIDER_STATE_FIELDS:
        if not hasattr(llm_client, name):
            continue
        value = getattr(llm_client, name)
        # Client references are identity-bearing (not transcript data).
        attributes[name] = (
            value
            if name in {"_last_client", "_active_client", "_privacy_gateway"}
            else _copy_isolated_provider_value(value)
        )

    # Privacy gateways are intentionally session-scoped and keep a reversible
    # alias table in memory.  Reusing that table for a trusted Help turn would
    # allow a provider response containing an old marker to be restored to a
    # prior user's/session's secret.  Swap in an empty gateway for this
    # one-turn request and put the exact original object back in the finally
    # block.  The provider-specific ``_sync_privacy_gateway`` methods will
    # still refresh identity/policy from the request context as needed.
    privacy_gateways: list[tuple[Any, str, OutboundPrivacyGateway]] = []
    gateway_owners: list[tuple[Any, str]] = [(llm_client, "_privacy_gateway")]
    turn_runner = getattr(llm_client, "_turn_runner", None)
    if turn_runner is not None:
        gateway_owners.append((turn_runner, "privacy_gateway"))
    seen_gateway_owners: set[tuple[int, str]] = set()
    for owner, attribute_name in gateway_owners:
        if owner is None or not hasattr(owner, attribute_name):
            continue
        owner_key = (id(owner), attribute_name)
        if owner_key in seen_gateway_owners:
            continue
        seen_gateway_owners.add(owner_key)
        gateway = getattr(owner, attribute_name, None)
        if not isinstance(gateway, OutboundPrivacyGateway):
            continue
        privacy_gateways.append((owner, attribute_name, gateway))
        try:
            isolated_gateway = OutboundPrivacyGateway(
                getattr(owner, "config", None)
                or getattr(llm_client, "config", None),
                user_id=str(getattr(llm_client, "session_user_id", None) or ""),
                session_id=str(getattr(llm_client, "current_session_id", None) or ""),
                session_context={},
                project_metadata={},
            )
            setattr(owner, attribute_name, isolated_gateway)
        except Exception as exc:
            # Help is a privacy boundary.  If an isolated gateway cannot be
            # constructed (or installed), continuing with the ordinary
            # gateway would allow aliases from a prior user/session to leak.
            # Restore any gateways already swapped before failing closed.
            for restore_owner, restore_attribute, original_gateway in privacy_gateways:
                try:
                    setattr(restore_owner, restore_attribute, original_gateway)
                except Exception:
                    pass
            raise RuntimeError(
                "AoiTalk Help isolated privacy gateway is unavailable"
            ) from exc
    if privacy_gateways:
        snapshot["privacy_gateways"] = privacy_gateways

    # AgentTurnRunner owns provider-managed continuation state separately from
    # the client compatibility fields above.  A Help turn forces that runner
    # stateless, so retain the exact pre-turn values and restore them even
    # when setup or generation fails before the runner's own cleanup path.
    turn_runner = getattr(llm_client, "_turn_runner", None)
    if turn_runner is not None:
        runner_state: dict[str, Any] = {}
        for name in (
            "conversation_state_mode",
            "provider_state",
            "prompt_cache_key",
            "prompt_cache_retention",
        ):
            if hasattr(turn_runner, name):
                runner_state[name] = (
                    getattr(turn_runner, name)
                    if name == "provider_state"
                    else _copy_isolated_provider_value(getattr(turn_runner, name))
                )
        if runner_state:
            snapshot["turn_runner"] = (turn_runner, runner_state)

    history_manager = getattr(llm_client, "history_manager", None)
    if history_manager is not None:
        history_state: dict[str, Any] = {}
        for name in _ISOLATED_HISTORY_MANAGER_FIELDS:
            if hasattr(history_manager, name):
                history_state[name] = _copy_isolated_provider_value(
                    getattr(history_manager, name)
                )
        snapshot["history_manager"] = (history_manager, history_state)
    return snapshot


def _restore_isolated_provider_state(snapshot: dict[str, Any] | None) -> None:
    """Restore a snapshot created by :func:`_snapshot_isolated_provider_state`."""

    if not snapshot:
        return
    llm_client = snapshot.get("client")
    if llm_client is None:
        return
    history_snapshot = snapshot.get("history_manager")
    if isinstance(history_snapshot, tuple) and len(history_snapshot) == 2:
        history_manager, history_state = history_snapshot
        try:
            # Free Team may have projected its ephemeral target's manager onto
            # the proxy.  Put the original manager object back before applying
            # the copied fields so ordinary state remains authoritative.
            llm_client.history_manager = history_manager
        except Exception:
            history_manager = getattr(llm_client, "history_manager", history_manager)
        if isinstance(history_state, dict):
            for name, value in history_state.items():
                try:
                    setattr(history_manager, name, _copy_isolated_provider_value(value))
                except Exception:
                    continue

    attributes = snapshot.get("attributes")
    if not isinstance(attributes, dict):
        return
    for name, value in attributes.items():
        try:
            restored = (
                value
                if name in {"_last_client", "_active_client", "_privacy_gateway"}
                else _copy_isolated_provider_value(value)
            )
            setattr(llm_client, name, restored)
        except Exception:
            continue

    for owner, attribute_name, gateway in snapshot.get("privacy_gateways", ()):
        try:
            setattr(owner, attribute_name, gateway)
        except Exception:
            continue

    runner_snapshot = snapshot.get("turn_runner")
    if isinstance(runner_snapshot, tuple) and len(runner_snapshot) == 2:
        turn_runner, runner_state = runner_snapshot
        if turn_runner is not None and isinstance(runner_state, dict):
            for name, value in runner_state.items():
                try:
                    setattr(turn_runner, name, value)
                except Exception:
                    continue


class TerminalMode(TerminalCommandsMixin, BaseAssistant):
    """Terminal mode assistant - text chat only"""
    
    def __init__(self, config):
        """Initialize terminal mode assistant
        
        Args:
            config: Configuration object
        """
        super().__init__(config, 'terminal')
        
        # Terminal mode doesn't use voice components
        self.response_handler = ResponseHandler(
            self.llm_client,
            character_name=self.character_name
        )
        self._chat_turn_lock = asyncio.Lock()
        self._chat_turn_persistence: Optional[ChatTurnPersistence] = None
        self._response_model_clients: dict[tuple[str, ...], Any] = {}

    def _get_active_llm_client(self):
        handler = getattr(self, "response_handler", None)
        handler_client = getattr(handler, "llm_client", None)
        return handler_client or getattr(self, "llm_client", None)

    def _response_model_identity(
        self, response_model: Optional[Dict[str, str]]
    ) -> Optional[tuple[str, str]]:
        if not isinstance(response_model, dict):
            return None
        provider = str(response_model.get("provider") or "").strip()
        model = str(response_model.get("model") or "").strip()
        if not provider or not model:
            return None
        return provider, model

    def _provider_model_config_keys(self, provider: str) -> tuple[str, ...]:
        return {
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "grok-cli": ("grok_cli.model",),
            "ollama": ("ollama_model", "ollama.model"),
            "sglang": ("sglang_model", "sglang.model"),
            "openai_compatible_local": ("openai_compatible_local.model",),
        }.get(provider, (f"{provider}.model",))

    def _clone_config_for_response_model(self, provider: str, model: str):
        cloned = copy.copy(self.config)
        cloned.config = copy.deepcopy(self.config.config)
        cloned.set("llm_provider", provider)
        cloned.set("llm_model", model)
        cloned.set("response_model_selection_active", True)
        for key in self._provider_model_config_keys(provider):
            cloned.set(key, model)
        return cloned

    def _active_client_matches_response_model(
        self,
        llm_client: Any,
        provider: str,
        model: str,
    ) -> bool:
        current_provider = str(
            getattr(llm_client, "provider_label", None)
            or self.config.get("llm_provider", "")
        ).strip()
        current_model = str(
            getattr(llm_client, "model_name", None)
            or self.config.get("llm_model", "")
        ).strip()
        return current_provider == provider and current_model == model

    def _get_response_model_client(
        self,
        response_model: Optional[Dict[str, str]],
        base_llm_client: Any,
    ):
        identity = self._response_model_identity(response_model)
        if identity is None:
            return base_llm_client

        provider, model = identity
        effort = response_model.get("reasoning_effort") if response_model else None
        # An explicit turn effort must not reuse/mutate the shared active client.
        cache_key = (*identity, str(effort)) if effort is not None else identity
        if effort is None and base_llm_client and self._active_client_matches_response_model(
            base_llm_client,
            provider,
            model,
        ):
            return base_llm_client

        cached = self._response_model_clients.get(cache_key)
        if cached is not None:
            return cached

        from ...llm.manager import create_llm_client

        config = self._clone_config_for_response_model(provider, model)
        if effort is not None:
            from ...llm.response_model_effort import apply_response_model_effort

            effort = apply_response_model_effort(config, provider, model, effort)
        client = create_llm_client(config)
        if effort is not None and hasattr(client, "set_llm_mode"):
            client.set_llm_mode(effort)
        personality = self.character_config.get("personality", {})
        system_prompt = personality.get(
            "details",
            "あなたは親切なAIアシスタントです。",
        )
        if hasattr(client, "set_system_prompt"):
            client.set_system_prompt(system_prompt)
        self._response_model_clients[cache_key] = client
        return client

    def _get_chat_turn_persistence(self, llm_client=None) -> ChatTurnPersistence:
        memory_manager = getattr(llm_client, "memory_manager", None)
        if (
            self._chat_turn_persistence is None
            or (
                memory_manager is not None
                and self._chat_turn_persistence.memory_manager is not memory_manager
            )
        ):
            self._chat_turn_persistence = ChatTurnPersistence(memory_manager)
        return self._chat_turn_persistence

    def _get_chat_turn_metadata(
        self,
        llm_client=None,
        image_data=None,
        attachments=None,
        client_message_id=None,
        include_generation_metrics: bool = False,
        media_recognition_metadata=None,
        command_capabilities=None,
        generation_profile=None,
    ) -> dict:
        metadata = {}
        # Direct WebSocket/terminal turns may persist the user row here
        # instead of through REST dispatch.  Persist the same server-resolved
        # profile so branch/rerun cannot silently fall back to chat (or invent
        # autonomous_work) after a reload.
        raw_profile = getattr(generation_profile, "value", generation_profile)
        if raw_profile in (None, "") and llm_client is not None:
            policy = getattr(llm_client, "generation_policy", None)
            raw_profile = getattr(getattr(policy, "profile", None), "value", None)
        try:
            canonical_generation_profile = resolve_generation_profile(raw_profile).value
        except ValueError:
            canonical_generation_profile = resolve_generation_profile(None).value
        metadata["generation_profile"] = canonical_generation_profile
        if include_generation_metrics:
            try:
                from ...services.turn_context import get_turn_context

                # AoiTalk Help is a Guide-only controller turn.  Provider
                # diagnostics belong to the ordinary conversation and must
                # not be copied into the Help assistant row from a reused
                # client (usage, cache keys, or prior context snapshots).
                if bool(get_turn_context().suppress_automatic_context):
                    include_generation_metrics = False
            except Exception:
                pass
        if llm_client and hasattr(llm_client, "_get_memory_metadata"):
            try:
                metadata.update(llm_client._get_memory_metadata() or {})
            except Exception:
                pass
        if (
            include_generation_metrics
            and llm_client
            and hasattr(llm_client, "get_generation_metadata")
        ):
            try:
                metadata.update(llm_client.get_generation_metadata() or {})
            except Exception:
                pass
        sanitized_attachments = sanitize_chat_attachments(
            attachments,
            include_binary=False,
        )
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        if sanitized_attachments:
            metadata["attachments"] = sanitized_attachments
        if media_recognition_metadata:
            metadata["media_recognition"] = list(media_recognition_metadata)
        if command_capabilities:
            metadata["command_capabilities"] = list(command_capabilities)
        if image_data:
            metadata.update(
                {
                    "has_image": True,
                    "image_mime_type": image_data.get("mimeType"),
                    "image_name": image_data.get("name"),
                }
            )
        if not include_generation_metrics:
            metadata = without_cached_generation_metadata(metadata)
        metadata["generation_profile"] = canonical_generation_profile
        return redact_sensitive_chat_metadata(metadata)

    async def _broadcast_conversation_persisted(
        self,
        *,
        session_id: Optional[str],
        role: str,
        message_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
    ) -> None:
        if not self.web_interface or not session_id:
            return
        broadcaster = getattr(self.web_interface, "broadcast_stream_event", None)
        if not broadcaster:
            return
        effective_run_id = agent_run_id or get_current_agent_run_id()
        result = broadcaster(
            "conversation_persisted",
            {
                "session_id": session_id,
                "role": role,
                "message_id": message_id,
                "agent_run_id": effective_run_id,
            },
        )
        if inspect.isawaitable(result):
            await result

    def _setup_keyword_detection(self):
        """キーワード検出システムをセットアップ"""
        try:
            from ...tools.keyword.initializer import setup_keyword_detection
            setup_keyword_detection(self.config)
        except Exception as e:
            logger.warning(
                "[TerminalMode] キーワード検出システムの初期化に失敗: %s",
                e,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            # エラーが発生してもターミナルモードは動作を続行
        
    async def _initialize_mode_specific(self) -> bool:
        """Initialize terminal mode specific components"""
        logger.info(
            "[ターミナルモード] テキストチャットモードで開始",
            extra=FILE_ONLY_LOG_EXTRA,
        )
        logger.info(
            "[ターミナルモード] TTS初期化をスキップ",
            extra=FILE_ONLY_LOG_EXTRA,
        )
        
        # Initialize keyword detection system after LLM client is ready
        self._setup_keyword_detection()
        
        return True
        
    async def run(self):
        """Run terminal mode"""
        # Initialize
        if not await self.initialize():
            return
        
        # Get greeting
        personality = self.character_config.get('personality', {})
        greeting = personality.get('greeting', 'こんにちは！')
        
        print(f"\n💬 ターミナルモード開始")
        print(f"{self.character_name}: {greeting}")
        print("💡 テキストで対話してください")
        print("📝 'quit' または 'exit' で終了します\n")

        # Optionally start web UI for text chat convenience
        web_host, web_port, auto_open = self._get_web_interface_settings()
        server_url = self._start_web_interface(
            self._process_user_message_web,
            host=web_host,
            port=web_port,
            auto_open_browser=auto_open
        )
        if server_url:
            logger.info(
                "Webチャットインターフェースを開始しました (テキスト専用): %s",
                server_url,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            if self.web_interface:
                self.web_interface.set_voice_recognition_ready(False)
                self.web_interface.set_recording_state(False)
                self.web_interface.update_rms(0.0)
                self.web_interface.add_system_message("🖥️ ターミナルモード: 音声なしでチャットできます")
                self.web_interface.add_assistant_message(greeting)
        else:
            logger.warning("Webインターフェースは利用できません（ターミナルのみ）")

        if runtime_feature_manager.feature_enabled("console_input"):
            await self._run_interactive_mode()
        else:
            print("💡 コンソール入力はOFFです。WebUI/Discordから操作してください。")
            self.running = True
            try:
                while self.running:
                    await asyncio.sleep(0.5)
            except KeyboardInterrupt:
                print("\n\n終了します...")

        # Cleanup
        await self.cleanup()
    
    
    async def _run_interactive_mode(self):
        """Run interactive mode with user input"""
        self.running = True
        
        try:
            while self.running:
                try:
                    raw = await asyncio.to_thread(input, "あなた: ")
                    message = raw.strip()
                    if message.lower() in ['quit', 'exit', '終了', 'やめる']:
                        break
                    if message:
                        await self._process_chat_message(message)
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print("\n\n終了します...")
                    break
        except Exception as e:
            print(f"ターミナルモードエラー: {e}")
    
    async def _process_chat_message(self, message: str, source: str = 'terminal', image_data: dict = None):
        """Process chat message

        Args:
            message: User message
            source: Message source ('terminal' or 'web')
            image_data: Optional image data for multimodal input {data: base64, mimeType: str, name: str}
        """
        try:
            if source != 'web' and self.web_interface:
                self.web_interface.add_user_message(message)
            # Check for keywords using universal keyword detection system
            try:
                from ...tools.keyword import process_keywords
                keyword_result = process_keywords(message)
                if keyword_result and keyword_result.detected:
                    # メッセージが辞書形式の場合（キャラクター切り替え）
                    if isinstance(keyword_result.message, dict):
                        msg_data = keyword_result.message
                        mode = msg_data.get('mode', '')

                        # 選択モードに入る時
                        if mode == 'selection_mode' and 'goodbye_reply' in msg_data:
                            # goodbyeReplyを表示
                            print(f"{self.character_name}: {msg_data['goodbye_reply']}")
                            print(f"\n{msg_data['message']}")
                            if self.web_interface:
                                self.web_interface.add_assistant_message(msg_data['goodbye_reply'])
                                self.web_interface.add_system_message(msg_data['message'])

                        # キャラクター切り替え完了時
                        elif mode == 'character_switched' and 'greeting' in msg_data:
                            print(f"\n{msg_data['message']}")
                            # キャラクター名を更新（コールバックが呼ばれるまでの一時的な対応）
                            from ...tools.keyword.character_manager import get_character_manager
                            manager = get_character_manager()
                            self.character_name = manager.get_current_character()
                            # greetingを表示
                            print(f"{self.character_name}: {msg_data['greeting']}")
                            if self.web_interface:
                                self.web_interface.add_system_message(msg_data['message'])
                                self.web_interface.add_assistant_message(msg_data['greeting'])

                        else:
                            print(f"{msg_data.get('message', '')}")
                            if self.web_interface and msg_data.get('message'):
                                self.web_interface.add_assistant_message(msg_data['message'])

                    # 通常のメッセージの場合
                    elif keyword_result.message:
                        print(f"{keyword_result.message}")
                        if self.web_interface:
                            self.web_interface.add_assistant_message(keyword_result.message)

                    # Skip normal processing if keyword was handled and LLM bypass is requested
                    if keyword_result.bypass_llm:
                        return
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")

            # Generate response
            response = await self.response_handler.handle_new_input(message, "chat", image_data=image_data)

            if response:
                print(f"{self.character_name}: {response}")
                if self.web_interface:
                    self.web_interface.add_assistant_message(response)
            else:
                print("応答の生成に失敗しました")
                    
        except Exception as e:
            print(f"チャットメッセージ処理エラー: {e}")

    def _extract_command_current_request(self, text: str) -> str:
        raw = str(text or "")
        for marker in (
            "\nCurrent user request:\n",
            "\r\nCurrent user request:\r\n",
            "Current user request:\n",
        ):
            if marker in raw:
                raw = raw.split(marker, 1)[-1]
                break
        lines = raw.strip().splitlines()
        if lines and lines[0].strip().casefold() == "/inbox":
            return "\n".join(lines[1:]).strip()
        return raw.strip()

    def _format_command_prompt_history(
        self,
        prompt_history: list[dict[str, str]],
        *,
        max_messages: int = 12,
    ) -> str:
        lines: list[str] = []
        for message in prompt_history[-max_messages:]:
            role = str(message.get("role") or "").strip() or "message"
            content = clip_text(str(message.get("content") or "").strip(), 1200)
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _sanitize_generated_search_query(
        self,
        query: str,
        *,
        fallback_request: str,
        prompt_history: list[dict[str, str]],
    ) -> str:
        cleaned = str(query or "").strip().strip("`'\" \t\r\n")
        for prefix in ("検索クエリ:", "Search query:", "Query:", "検索語:"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix) :].strip()
        if cleaned:
            cleaned = cleaned.splitlines()[0].strip("`'\" \t\r\n")
        compact = cleaned.replace(" ", "").replace("　", "")
        generic_queries = {
            "?",
            "？",
            "検索",
            "検索して",
            "search",
            "lookitup",
            fallback_request.replace(" ", "").replace("　", ""),
        }
        question_marks = sum(1 for char in compact if char in {"?", "？"})
        looks_garbled = question_marks >= 3 and question_marks >= max(
            1,
            len(compact) // 2,
        )
        if (
            not cleaned
            or "Tool Hints" in cleaned
            or len(cleaned) > 160
            or compact.lower() in generic_queries
            or looks_garbled
        ):
            for message in reversed(prompt_history):
                if message.get("role") == "user":
                    candidate = str(message.get("content") or "").strip()
                    if candidate and candidate != fallback_request:
                        return self._fallback_search_query_from_text(candidate)
            return clip_text(fallback_request, 120)
        if (
            "意味" not in cleaned
            and self._history_requests_meaning_lookup(
                fallback_request=fallback_request,
                prompt_history=prompt_history,
            )
        ):
            cleaned = f"{cleaned} 意味"
        return cleaned

    def _history_requests_meaning_lookup(
        self,
        *,
        fallback_request: str,
        prompt_history: list[dict[str, str]],
    ) -> bool:
        texts = [fallback_request]
        texts.extend(
            str(message.get("content") or "")
            for message in prompt_history[-6:]
            if message.get("role") == "user"
        )
        for text in texts:
            value = str(text or "").strip()
            if "意味" in value:
                return True
            if re.search(r"(って)?何[？?。.!！\s]*$", value):
                return True
            if re.search(r"(とは|について)[？?。.!！\s]*$", value):
                return True
        return False

    def _fallback_search_query_from_text(self, text: str) -> str:
        raw = str(text or "").strip()
        quoted = re.search(r"[「『\"]([^」』\"]{2,100})[」』\"]", raw)
        if quoted:
            base = quoted.group(1).strip()
        else:
            base = raw
        base = re.sub(r"(って)?何[？?。.!！]*$", "", base).strip()
        base = re.sub(r"(とは|について)[？?。.!！]*$", "", base).strip()
        if not base:
            return clip_text(raw, 120)
        if any(marker in raw for marker in ("何", "とは", "意味")) and "意味" not in base:
            base = f"{base} 意味"
        return clip_text(base, 120)

    async def _plain_llm_response_for_command(
        self,
        llm_client: Any,
        prompt: str,
    ) -> str:
        if llm_client is None:
            return ""
        # provider 分岐（openai=Responses / openrouter=chat.completions）は
        # AgentLLMClient.generate_plain_text_async に集約している。
        if hasattr(llm_client, "generate_plain_text_async"):
            return str(await llm_client.generate_plain_text_async(prompt))
        if hasattr(llm_client, "chat"):
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a concise Japanese assistant. Follow the user "
                        "instruction exactly. Do not call tools and do not output "
                        "tool hints."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            had_native_tools = hasattr(llm_client, "_native_tools_enabled")
            previous_native_tools = getattr(llm_client, "_native_tools_enabled", None)
            had_caps = hasattr(llm_client, "current_command_capabilities")
            previous_caps = getattr(llm_client, "current_command_capabilities", None)
            lock = getattr(llm_client, "_plain_command_llm_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                llm_client._plain_command_llm_lock = lock
            async with lock:
                try:
                    if had_native_tools:
                        llm_client._native_tools_enabled = False
                    if had_caps:
                        llm_client.current_command_capabilities = ()
                    return str(await asyncio.to_thread(llm_client.chat, messages))
                finally:
                    if had_native_tools:
                        llm_client._native_tools_enabled = previous_native_tools
                    if had_caps:
                        llm_client.current_command_capabilities = previous_caps
        if hasattr(llm_client, "generate_response_async"):
            return str(await llm_client.generate_response_async(prompt))
        if hasattr(llm_client, "generate_response"):
            return str(
                await asyncio.to_thread(
                    llm_client.generate_response,
                    prompt,
                    stream=False,
                )
            )
        return ""

    async def _process_user_message_web(
        self,
        message: str,
        persist_content=None,
        image_data=None,
        session_id=None,
        project_id=None,
        generation_profile=None,
        planning_policy=None,
        include_project_context=False,
        edit_message_id=None,
        response_model=None,
        client_message_id=None,
        attachments=None,
        attachment_context=None,
        skip_user_persistence=False,
        persisted_user_message_id=None,
        agent_run_id=None,
        assistant_sender_type=None,
        assistant_sender_id=None,
        assistant_sender_display_name=None,
        sender_user_id=None,
        sender_display_name=None,
        response_started_at_monotonic=None,
        command_capabilities=None,
        tools_required=None,
        media_recognition_metadata=None,
        docs_reference_ids=None,
        task_id=None,
        explicit_references=None,
        cloud_advisor_origin=None,
        cloud_advisor_assessment=None,
        verified_project_attachment=False,
        suppress_automatic_context=False,
        strict_project_scope=False,
    ):
        """Process user message sent from the WebUI

        Args:
            message: User message text（LLM へ渡す展開済み本文）
            persist_content: DB へ保存する生入力。未指定時は message を使う。
            image_data: Optional image data {data: base64, mimeType: str, name: str}
            session_id: Optional conversation session ID from frontend
            project_id: Optional project ID from frontend
        """
        # Defense in depth: the server request boundary normally intercepts
        # ``/masking`` before this callback is scheduled.  If an old queued
        # callback or a direct embedding reaches TerminalMode anyway, route it
        # back through the trusted local helper and never instantiate a
        # provider/persist an unmarked user row here.
        masking_input = (
            persist_content if isinstance(persist_content, str) else message
        )
        masking_command = None
        try:
            from ...services.masking_service import parse_masking_command

            masking_command = parse_masking_command(masking_input)
        except Exception:
            masking_command = None
        if masking_command is None:
            # If a stale/partial worker cannot import the canonical parser,
            # an exact built-in token must still fail closed rather than being
            # sent to the ordinary provider callback.
            parts = (
                masking_input.strip().split(None, 1)
                if isinstance(masking_input, str)
                else []
            )
            if parts and parts[0].casefold() == "/masking":
                print(
                    "[TerminalMode] Masking parser unavailable; refusing provider dispatch"
                )
                return
        if masking_command is not None:
            masking_handler = getattr(
                getattr(self, "web_interface", None),
                "_execute_builtin_masking_turn",
                None,
            )
            if callable(masking_handler):
                await masking_handler(
                    {
                        "message": (
                            persist_content
                            if isinstance(persist_content, str)
                            else message
                        ),
                        "session_id": session_id,
                        "project_id": project_id,
                        "_sender_user_id": sender_user_id,
                        "_sender_display_name": sender_display_name,
                        "client_message_id": client_message_id,
                        "attachments": attachments or [],
                        "skip_user_persistence": skip_user_persistence,
                        "persisted_user_message_id": persisted_user_message_id,
                        "edit_message_id": edit_message_id,
                    },
                    masking_command,
                )
            else:
                print(
                    "[TerminalMode] Masking helper unavailable; refusing provider dispatch"
                )
            return
        if persist_content is None:
            persist_content = message
        llm_client = self._get_active_llm_client()
        llm_message = build_message_with_attachment_context(
            message,
            attachment_context,
        )
        normalized_command_capabilities = sanitize_command_capabilities(
            command_capabilities
        )
        llm_message = protect_untrusted_command_context(
            llm_message,
            normalized_command_capabilities,
        )
        chat_persistence = self._get_chat_turn_persistence(llm_client)
        user_message = None
        search_tool_results: list[dict[str, Any]] = []
        agent_run_service = AgentRunService() if agent_run_id else None
        agent_run_context_token = (
            set_current_agent_run_id(agent_run_id) if agent_run_id else None
        )
        turn_context_token = set_turn_context(
            user_id=sender_user_id,
            project_id=project_id,
            include_project_context=bool(include_project_context),
            session_id=session_id,
            message_id=persisted_user_message_id,
            client_message_id=client_message_id,
            # These values come from the authenticated server boundary.  Do
            # not rediscover UUIDs or attachment paths from prompt text here.
            docs_reference_ids=docs_reference_ids,
            verified_project_attachment=bool(verified_project_attachment),
            # These are server-authorized values carried across the callback
            # boundary.  Keep them on the generation TurnContext instead of
            # inferring scope from model/user prompt IDs.
            task_id=task_id,
            explicit_references=explicit_references,
            cloud_advisor_origin=cloud_advisor_origin,
            cloud_advisor_assessment=cloud_advisor_assessment,
            suppress_automatic_context=bool(suppress_automatic_context),
            strict_project_scope=bool(strict_project_scope),
        )
        persisted_message_context_token = None
        planning_scope_cm = None
        stream_callback = None
        used_streaming = False
        isolated_provider_state_snapshot = None
        original_handler_client = None
        handler_client_assigned = False

        emitter = AgentRunEventEmitter(
            agent_run_service=agent_run_service,
            agent_run_id=agent_run_id,
            session_id=session_id,
            project_id=project_id,
            generation_profile=generation_profile,
            include_project_context=include_project_context,
            command_capabilities=normalized_command_capabilities,
            search_tool_results=search_tool_results,
            user_input=message,
        )

        if session_id and not skip_user_persistence:
            try:
                user_turn_metadata = self._get_chat_turn_metadata(
                    llm_client,
                    image_data,
                    attachments,
                    client_message_id,
                    media_recognition_metadata=media_recognition_metadata,
                    command_capabilities=normalized_command_capabilities,
                    generation_profile=generation_profile,
                )
                if suppress_automatic_context:
                    user_turn_metadata["aoitalk_help"] = {"grounding": "ready"}
                user_message = await chat_persistence.save_user_message(
                    session_id=session_id,
                    content=persist_content,
                    metadata=user_turn_metadata,
                    branch_from_message_id=edit_message_id,
                    sender_type="user" if sender_user_id else None,
                    sender_id=sender_user_id,
                    sender_display_name=sender_display_name,
                )
                if user_message is not None and getattr(user_message, "id", None):
                    # The persistence layer owns the canonical Conversation-
                    # Message UUID.  Rebind the turn after the row exists so
                    # background Dreaming cannot fall back to a client UUID.
                    persisted_message_context_token = override_turn_context(
                        message_id=str(user_message.id)
                    )
                if (
                    user_message
                    and agent_run_id
                    and sender_user_id
                    and not suppress_automatic_context
                ):
                    from ...services.learning_capture_router import (
                        capture_direct_websocket_learning_best_effort,
                    )

                    await capture_direct_websocket_learning_best_effort(
                        actor_id=str(sender_user_id),
                        raw_text=str(persist_content or ""),
                        session_id=str(session_id),
                        project_id=(
                            str(project_id) if project_id else None
                        ),
                        message_id=str(user_message.id),
                        agent_run_id=str(agent_run_id),
                        client_message_id=client_message_id,
                    )
                await self._broadcast_conversation_persisted(
                    session_id=session_id,
                    role="user",
                    message_id=str(user_message.id) if user_message else None,
                )
            except Exception as e:
                print(f"[TerminalMode] ユーザーメッセージ保存エラー: {e}")

        async def persist_assistant_reply(
            reply: Optional[str],
            *,
            include_generation_metrics: bool = True,
            # Reserved Help turns are intentionally one-turn and must not
            # trigger title generation.  Title generation reads the active
            # conversation branch through a separate provider path; using a
            # context-free Help turn there would otherwise leak prior chat
            # history into that request.  Ordinary turns retain the existing
            # best-effort title enhancement.
            generate_title: bool = not suppress_automatic_context,
            metadata_extra: Mapping[str, Any] | None = None,
        ) -> str:
            safe_reply = sanitize_assistant_display_text(reply)
            if not safe_reply or not session_id:
                return safe_reply
            if agent_run_id:
                fence_checker = getattr(
                    self.web_interface, "_is_fenced_generation_run", None
                )
                if callable(fence_checker) and fence_checker(session_id, agent_run_id):
                    print(
                        f"[TerminalMode] fenced late assistant persistence skipped: {agent_run_id}"
                    )
                    return safe_reply
            try:
                metadata = self._get_chat_turn_metadata(
                    llm_client,
                    include_generation_metrics=include_generation_metrics,
                    generation_profile=generation_profile,
                )
                if suppress_automatic_context:
                    metadata["aoitalk_help"] = {"grounding": "ready"}
                if isinstance(response_started_at_monotonic, (int, float)):
                    elapsed_ms = int(
                        max(
                            0,
                            round(
                                (time.monotonic() - response_started_at_monotonic)
                                * 1000
                            ),
                        )
                    )
                    metadata["response_elapsed_ms"] = elapsed_ms
                if agent_run_id:
                    metadata["agent_run_id"] = agent_run_id
                if search_tool_results:
                    metadata["tool_results"] = list(search_tool_results)
                if isinstance(metadata_extra, Mapping):
                    # Callers supply only already-projected workflow metadata;
                    # keep a shallow copy so no mutable/raw object is stored
                    # by reference in the persistence layer.
                    metadata.update(dict(metadata_extra))
                assistant_message = await chat_persistence.save_assistant_message(
                    session_id=session_id,
                    content=safe_reply,
                    metadata=metadata,
                    sender_type=assistant_sender_type,
                    sender_id=assistant_sender_id,
                    sender_display_name=assistant_sender_display_name,
                    message_id=getattr(llm_client, "current_assistant_message_id", None),
                )
                await self._broadcast_conversation_persisted(
                    session_id=session_id,
                    role="assistant",
                    message_id=str(assistant_message.id) if assistant_message else None,
                    agent_run_id=agent_run_id,
                )
                # A rejected/failed turn must not start a second external
                # title-generation request after the user cancelled an
                # outbound transaction.  Successful assistant replies keep
                # the existing best-effort title enhancement.
                if generate_title:
                    await maybe_generate_and_broadcast_session_title(
                        web_interface=self.web_interface,
                        session_id=session_id,
                        chat_persistence=chat_persistence,
                        config=getattr(self, "config", None),
                        log_prefix="TerminalMode",
                    )
            except Exception as e:
                print(f"[TerminalMode] アシスタントメッセージ保存エラー: {e}")
            return safe_reply

        try:
            try:
                from ...tools.keyword import process_keywords

                # Reserved Help turns are deliberately isolated from the
                # legacy keyword/character shortcut layer.  That layer may
                # mutate session character state or return canned replies
                # before the Guide-grounded generation path gets a chance to
                # run.
                keyword_result = (
                    None if suppress_automatic_context else process_keywords(message)
                )
                if keyword_result and keyword_result.detected:
                    if isinstance(keyword_result.message, dict):
                        msg_data = keyword_result.message
                        mode = msg_data.get("mode", "")

                        if mode == "selection_mode" and "goodbye_reply" in msg_data:
                            reply = msg_data["goodbye_reply"]
                            safe_reply = sanitize_assistant_display_text(reply)
                            print(f"{self.character_name}: {reply}")
                            print(f"\n{msg_data.get('message', '')}")
                            if self.web_interface:
                                self.web_interface.add_assistant_message(
                                    safe_reply, session_id=session_id
                                )
                                self.web_interface.add_system_message(msg_data.get("message", ""))
                            await persist_assistant_reply(
                                safe_reply,
                                include_generation_metrics=False,
                            )
                            await emitter.complete(reply)
                            return

                        if mode == "character_switched" and "greeting" in msg_data:
                            from ...tools.keyword.character_manager import get_character_manager

                            manager = get_character_manager()
                            self.character_name = manager.get_current_character()
                            reply = msg_data["greeting"]
                            safe_reply = sanitize_assistant_display_text(reply)
                            print(f"\n{msg_data.get('message', '')}")
                            print(f"{self.character_name}: {reply}")
                            if self.web_interface:
                                self.web_interface.add_system_message(msg_data.get("message", ""))
                                self.web_interface.add_assistant_message(
                                    safe_reply, session_id=session_id
                                )
                            await persist_assistant_reply(
                                safe_reply,
                                include_generation_metrics=False,
                            )
                            await emitter.complete(reply)
                            return

                        reply = msg_data.get("message", "")
                        if reply:
                            safe_reply = sanitize_assistant_display_text(reply)
                            print(reply)
                            if self.web_interface:
                                self.web_interface.add_assistant_message(
                                    safe_reply, session_id=session_id
                                )
                            await persist_assistant_reply(
                                safe_reply,
                                include_generation_metrics=False,
                            )

                    elif keyword_result.message:
                        reply = keyword_result.message
                        safe_reply = sanitize_assistant_display_text(reply)
                        print(reply)
                        if self.web_interface:
                            self.web_interface.add_assistant_message(
                                safe_reply, session_id=session_id
                            )
                        await persist_assistant_reply(
                            safe_reply,
                            include_generation_metrics=False,
                        )

                    if keyword_result.bypass_llm:
                        await emitter.complete(locals().get("reply"))
                        return
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")

            session_character_name = None

            @asynccontextmanager
            async def bind_session_memory_character():
                nonlocal session_character_name
                token = None
                if suppress_automatic_context:
                    # Help is grounded only in the canonical Guide.  Avoid
                    # resolving or binding a conversation character because it
                    # would reintroduce session state into the provider turn.
                    yield
                    return
                try:
                    session_character_name = (
                        await chat_persistence.resolve_session_character_name(
                            session_id
                        )
                        if session_id
                        else None
                    )
                except Exception as exc:
                    print(
                        "[TerminalMode] セッションの記憶キャラクター解決を"
                        f"スキップしました: {exc}"
                    )
                    raise RuntimeError(
                        "セッションのキャラクターを解決できないため、"
                        "別キャラクターで応答せず処理を中断しました"
                    ) from exc
                try:
                    fallback_character_name = str(
                        getattr(self._get_active_llm_client(), "character_name", "")
                        or ""
                    ).strip()
                    character_name = (
                        str(session_character_name or "").strip()
                        or fallback_character_name
                        or None
                    )
                    if character_name:
                        from ...tools.memory.memory_tools import (
                            set_current_memory_character_name,
                        )

                        token = set_current_memory_character_name(character_name)
                except Exception as exc:
                    print(
                        "[TerminalMode] 記憶キャラクターのbindを"
                        f"スキップしました: {exc}"
                    )
                try:
                    yield
                finally:
                    if token is not None:
                        from ...tools.memory.memory_tools import (
                            reset_current_memory_character_name,
                        )

                        reset_current_memory_character_name(token)

            async with self._chat_turn_lock, bind_session_memory_character():
                base_llm_client = self._get_active_llm_client()
                llm_client = self._get_response_model_client(
                    response_model,
                    base_llm_client,
                )
                if suppress_automatic_context:
                    isolated_provider_state_snapshot = _snapshot_isolated_provider_state(
                        llm_client
                    )
                reset_turn_generation_metadata(llm_client)
                await emitter.mark_running(llm_client)
                chat_persistence = self._get_chat_turn_persistence(base_llm_client)
                original_handler_client = getattr(
                    self.response_handler,
                    "llm_client",
                    None,
                )
                if self.response_handler:
                    self.response_handler.llm_client = llm_client
                    handler_client_assigned = True
                turn_user_context_snapshot = None
                prompt_history: list[dict[str, str]] = []
                orchestration_mode = agent_team_orchestration_mode(
                    getattr(self, "config", None)
                )
                if llm_client:
                    if suppress_automatic_context:
                        # Clear any provider-local history left by a preceding
                        # ordinary turn even for the legacy no-session path;
                        # Help must not inherit conversation messages or
                        # summaries.
                        prompt_history = []
                    elif session_id:
                        exclude_message_id = (
                            str(user_message.id)
                            if user_message
                            else persisted_user_message_id
                        )
                        prompt_history = await chat_persistence.load_prompt_history(
                            session_id=session_id,
                            exclude_message_id=exclude_message_id,
                        )
                    if session_character_name and session_id:
                        current_character_name = str(
                            getattr(llm_client, "character_name", "") or ""
                        ).strip()
                        if current_character_name != session_character_name:
                            try:
                                if hasattr(llm_client, "set_character"):
                                    llm_client.set_character(session_character_name)
                                elif hasattr(llm_client, "update_character"):
                                    llm_client.update_character(session_character_name)
                                else:
                                    raise RuntimeError(
                                        "LLMクライアントがキャラクター切替に対応していません"
                                    )
                            except Exception as exc:
                                if self.response_handler:
                                    self.response_handler.llm_client = (
                                        original_handler_client
                                    )
                                raise RuntimeError(
                                    "セッションのキャラクターを適用できないため、"
                                    f"別キャラクターで応答せず処理を中断しました: "
                                    f"{session_character_name}: {exc}"
                                )
                        if str(
                            getattr(llm_client, "character_name", "") or ""
                        ).strip() != session_character_name:
                            if self.response_handler:
                                self.response_handler.llm_client = original_handler_client
                            raise RuntimeError(
                                "セッションのキャラクター設定が反映されませんでした: "
                                f"{session_character_name}"
                            )
                        if self.response_handler:
                            self.response_handler.character_name = session_character_name
                    if (
                        orchestration_mode != "director"
                        or suppress_automatic_context
                    ) and (session_id or suppress_automatic_context):
                        chat_persistence.apply_prompt_history_to_client(
                            llm_client,
                            session_id=session_id,
                            prompt_history=prompt_history,
                        )

                if llm_client:
                    turn_user_context_snapshot = apply_turn_user_context_to_client(
                        llm_client,
                        sender_user_id=sender_user_id,
                        sender_display_name=sender_display_name,
                    )
                    if suppress_automatic_context:
                        # Help is independent of the selected Project and may
                        # also be invoked without a conversation session. Do
                        # not leave a stale provider field from a preceding
                        # ordinary turn for prompt/cache/tool setup to read.
                        llm_client.current_session_id = (
                            session_id if session_id else None
                        )
                        llm_client.current_project_id = None
                        # The ordinary session's privacy policy is another
                        # request-scoped input, not part of the Guide.  Clear
                        # it before any provider gateway is synchronized so a
                        # shared client cannot carry a prior user's
                        # ``direct``/``local_only``/``protected`` mode into
                        # this isolated Help request.  The provider snapshot
                        # restores the previous policy after the turn.
                        if hasattr(llm_client, "_privacy_session_context"):
                            llm_client._privacy_session_context = {}
                        if hasattr(llm_client, "_privacy_project_metadata"):
                            llm_client._privacy_project_metadata = {}
                    else:
                        if session_id:
                            llm_client.current_session_id = session_id
                            print(f"[TerminalMode] Set session_id for message storage: {session_id}")
                        if project_id:
                            llm_client.current_project_id = project_id
                            print(f"[TerminalMode] Set project_id for session creation: {project_id}")
                    llm_client.current_command_capabilities = (
                        normalized_command_capabilities
                    )
                    llm_client.current_tool_required = (
                        tools_required if isinstance(tools_required, bool) else None
                    )
                    llm_client.generation_policy = generation_policy_for_profile(
                        generation_profile
                    )
                    from ...llm.planning_policy import resolve_planning_policy

                    llm_client.planning_policy = resolve_planning_policy(planning_policy)
                    llm_client.current_include_project_context = bool(
                        include_project_context
                    )
                    llm_client.current_edit_message_id = edit_message_id
                    llm_client.current_response_model = response_model
                    llm_client.external_persistence_enabled = bool(
                        user_message
                        or (skip_user_persistence and session_id)
                        or suppress_automatic_context
                    )
                    from ...services.planning_runtime import planning_turn_scope

                    planning_scope_cm = planning_turn_scope(
                        user_input=str(persist_content or message or ""),
                        generation_policy=generation_policy_for_profile(
                            generation_profile
                        ),
                        planning_policy=planning_policy,
                    )
                    planning_scope_cm.__enter__()

                if orchestration_mode == "director" and not suppress_automatic_context:
                    from ...llm.director_controller import DirectorTurnController

                    director_streaming = bool(
                        self.web_interface
                        and hasattr(self.web_interface, "broadcast_stream_event")
                    )

                    async def _director_stream_callback(
                        event_type: str,
                        data: dict,
                    ) -> None:
                        if not director_streaming:
                            return
                        event_data = _enrich_agent_run_event_payload(
                            getattr(self, "config", None),
                            dict(data),
                        )
                        if session_id:
                            event_data["session_id"] = session_id
                        if agent_run_id:
                            event_data["agent_run_id"] = agent_run_id
                        safe_event_data = sanitize_stream_display_payload(
                            event_type,
                            event_data,
                        )
                        try:
                            result = self.web_interface.broadcast_stream_event(
                                event_type,
                                safe_event_data,
                            )
                            if inspect.isawaitable(result):
                                await result
                        except Exception as exc:
                            print(
                                "[TerminalMode] Director進捗イベント送信エラー: "
                                f"{exc}"
                            )

                    project_name = str(project_id or "").strip() or "未選択"
                    director_project_metadata: dict[str, Any] = {}
                    trusted_parent_context = None
                    qa_browser_coordinator = None
                    qa_playwright = None
                    try:
                        from ...services.agent_run_scope_service import (
                            TRUSTED_PARENT_CONTEXT_KEY,
                            create_parent_run_context_from_config,
                            resolve_trusted_parent_run_context,
                        )
                        from ...services.project_context import get_runtime_project_context

                        runtime_context = get_runtime_project_context()
                        candidate_context = (
                            runtime_context
                            if isinstance(runtime_context, dict)
                            else director_project_metadata
                        )
                        if isinstance(candidate_context, dict):
                            trusted_parent_context = resolve_trusted_parent_run_context(
                                candidate_context.get(TRUSTED_PARENT_CONTEXT_KEY),
                                parent_run_id=agent_run_id,
                            )
                    except Exception as exc:
                        # A malformed/untrusted marker must never widen the
                        # Director run.  It simply leaves publication/scope
                        # integration disabled for this legacy turn.
                        print(
                            "[TerminalMode] Director parent scope markerを"
                            f"無視しました: {exc}"
                        )
                    if trusted_parent_context is None and agent_run_id:
                        try:
                            # The production Director entrypoint may create a
                            # parent scope only from its dedicated trusted
                            # setting.  Project ``workspace_root`` and model
                            # text are intentionally never considered here.
                            trusted_parent_context = (
                                create_parent_run_context_from_config(
                                    getattr(self, "config", None),
                                    parent_run_id=agent_run_id,
                                )
                            )
                        except Exception as exc:
                            # An absent/invalid explicit repository setting
                            # keeps ordinary Director reads available; any
                            # write-capable worker will fail closed in the
                            # runtime registry instead of widening scope.
                            print(
                                "[TerminalMode] Director親run scope factoryを"
                                f"利用できません: {exc}"
                            )
                    resolver = getattr(llm_client, "_resolve_project_context", None)
                    if callable(resolver):
                        try:
                            resolved_project = resolver()
                            if inspect.isawaitable(resolved_project):
                                resolved_project = await resolved_project
                            if isinstance(resolved_project, dict):
                                # Director's graph resolver consumes the
                                # existing project context as read-only data;
                                # do not infer context from Team IDs.
                                director_project_metadata = dict(resolved_project)
                                project_name = str(
                                    resolved_project.get("name")
                                    or resolved_project.get("title")
                                    or resolved_project.get("id")
                                    or project_name
                                )
                                if trusted_parent_context is None:
                                    trusted_parent_context = resolve_trusted_parent_run_context(
                                        resolved_project.get(TRUSTED_PARENT_CONTEXT_KEY),
                                        parent_run_id=agent_run_id,
                                    )
                        except Exception as exc:
                            print(
                                "[TerminalMode] Director用プロジェクト名の解決を"
                                f"スキップしました: {exc}"
                            )
                    # QA Browser is an explicit parent setting.  It is never
                    # inferred from Project/model text and is not started for
                    # ordinary Director conversations.  When enabled, the
                    # parent owns Playwright/profile/transport and injects
                    # only the opaque capability facade into the Operator.
                    try:
                        config_value = getattr(getattr(self, "config", None), "get", None)
                        config_obj = getattr(self, "config", None)

                        def _qa_config_value(key: str, default: Any = None) -> Any:
                            value = None
                            if callable(config_value):
                                try:
                                    value = config_value(key, None)
                                except TypeError:
                                    value = config_value(key)
                            if value is not None:
                                return value
                            raw = (
                                config_obj
                                if isinstance(config_obj, dict)
                                else getattr(config_obj, "config", None)
                            )
                            current = raw
                            for part in key.split("."):
                                if not isinstance(current, dict) or part not in current:
                                    return default
                                current = current[part]
                            return current

                        qa_enabled = _qa_config_value(
                            "agent_operator.qa_browser_enabled",
                            False,
                        )
                        qa_origins = _qa_config_value(
                            "agent_operator.qa_allowed_origins",
                            None,
                        )
                        if isinstance(qa_origins, str) and "," in qa_origins:
                            qa_origins = [
                                item.strip() for item in qa_origins.split(",") if item.strip()
                            ]
                        if (
                            str(qa_enabled).strip().lower() in {"1", "true", "yes", "on"}
                            and trusted_parent_context is not None
                            and qa_origins
                        ):
                            from playwright.async_api import async_playwright

                            from ...services.qa_browser_coordinator import (
                                create_qa_browser_coordinator,
                            )

                            qa_playwright = await async_playwright().start()
                            qa_browser_coordinator = (
                                await create_qa_browser_coordinator(
                                    allowed_origins=qa_origins,
                                    trusted_parent_context=trusted_parent_context,
                                    playwright=qa_playwright,
                                    role="ui_qa_worker",
                                )
                            )
                    except Exception as exc:
                        if qa_browser_coordinator is not None:
                            try:
                                await qa_browser_coordinator.close(
                                    "QA Browser setup failed"
                                )
                            except Exception:
                                pass
                            qa_browser_coordinator = None
                        if qa_playwright is not None:
                            try:
                                await qa_playwright.stop()
                            except Exception:
                                pass
                            qa_playwright = None
                        print(
                            "[TerminalMode] QA Browser parent laneを"
                            f"開始できません: {exc}"
                        )
                    try:
                        controller = DirectorTurnController(
                            config=getattr(self, "config", {}),
                            session_id=session_id,
                            user_id=sender_user_id,
                            project_id=project_id,
                            project_name=project_name,
                            parent_run_id=agent_run_id,
                            generation_profile=generation_profile,
                            chat_persistence=chat_persistence,
                            agent_run_service=agent_run_service,
                            progress_callback=_director_stream_callback,
                            session_context={
                                "session_id": session_id,
                                "generation_profile": generation_profile,
                            },
                            project_metadata=director_project_metadata,
                            trusted_parent_context=trusted_parent_context,
                            qa_browser_coordinator=qa_browser_coordinator,
                            require_parent_scope=True,
                        )
                        if director_streaming:
                            await _director_stream_callback(
                                "stream_start",
                                {
                                    "status": "director",
                                    "message": "Directorが依頼を確認しています",
                                },
                            )
                        response = await controller.run(
                            llm_message,
                            attachments=attachments,
                            history=prompt_history,
                        )
                        safe_response = await persist_assistant_reply(
                            response,
                            include_generation_metrics=False,
                        )
                        await emitter.complete(response, llm_client)
                        if director_streaming:
                            await _director_stream_callback(
                                "stream_end",
                                {"content": safe_response},
                            )
                        print(f"{self.character_name}: {response}")
                        if self.web_interface and not director_streaming:
                            self.web_interface.add_assistant_message(
                                safe_response,
                                session_id=session_id,
                            )
                        return response
                    except asyncio.CancelledError:
                        if director_streaming:
                            await _director_stream_callback(
                                "stream_cancelled",
                                {
                                    "status": "cancelled",
                                    "message": "Director処理を停止しました",
                                },
                            )
                        raise
                    except Exception:
                        if director_streaming:
                            await _director_stream_callback(
                                "stream_end",
                                {
                                    "status": "failed",
                                    "message": "Director処理中にエラーが発生しました",
                                },
                            )
                        raise
                    finally:
                        if qa_browser_coordinator is not None:
                            try:
                                await qa_browser_coordinator.close(
                                    "Director parent turn finished"
                                )
                            except Exception as exc:
                                print(
                                    "[TerminalMode] QA Browser cleanup error: "
                                    f"{exc}"
                                )
                        if qa_playwright is not None:
                            try:
                                await qa_playwright.stop()
                            except Exception as exc:
                                print(
                                    "[TerminalMode] QA Browser Playwright cleanup error: "
                                    f"{exc}"
                                )
                        if self.response_handler:
                            self.response_handler.llm_client = original_handler_client
                        if llm_client:
                            llm_client.current_session_id = None
                            llm_client.current_project_id = None
                            llm_client.generation_policy = generation_policy_for_profile(
                                None
                            )
                            llm_client.current_include_project_context = None
                            llm_client.current_edit_message_id = None
                            llm_client.current_response_model = None
                            llm_client.current_command_capabilities = ()
                            llm_client.current_tool_required = None
                            llm_client.external_persistence_enabled = False
                            restore_turn_user_context_on_client(
                                llm_client,
                                turn_user_context_snapshot,
                            )

                stream_callback = None
                steering_callback = None
                used_streaming = False
                supports_streaming = bool(
                    llm_client and hasattr(llm_client, "_run_streamed_with_callback")
                )
                active_tool_operations: dict[
                    str,
                    list[tuple[str, float, str]],
                ] = {}
                if (
                    session_id
                    and self.web_interface
                    and not suppress_automatic_context
                    and hasattr(self.web_interface, "consume_generation_steering")
                ):
                    web_iface = self.web_interface

                    async def _steering_callback():
                        result = web_iface.consume_generation_steering(session_id)
                        if inspect.isawaitable(result):
                            result = await result
                        return result or []

                    steering_callback = _steering_callback

                if (
                    self.web_interface
                    and hasattr(self.web_interface, "broadcast_stream_event")
                ):
                    web_iface = self.web_interface
                    # 最終応答の描画経路を運ぶイベント群。非ストリーミングクライアント
                    # では add_assistant_message が最終応答を届けるため、これらを流すと
                    # 二重描画になる。ツール・途中経過・思考イベントだけを通す。
                    content_stream_events = {
                        "stream_start",
                        "stream_token",
                        "stream_end",
                        "stream_cancelled",
                    }

                    async def _stream_callback(event_type: str, data: dict):
                        nonlocal used_streaming
                        cancellation_handle = (
                            get_current_generation_cancellation()
                        )
                        if (
                            cancellation_handle is not None
                            and cancellation_handle.cancel_requested.is_set()
                        ):
                            return
                        if not supports_streaming:
                            if event_type in content_stream_events:
                                return
                        else:
                            used_streaming = True
                        try:
                            event_data = _enrich_agent_run_event_payload(
                                getattr(self, "config", None),
                                dict(data),
                            )
                            tool_name = str(
                                event_data.get("tool")
                                or event_data.get("tool_name")
                                or ""
                            ).strip()
                            tool_result = event_data.get("tool_result")
                            if not tool_name and isinstance(tool_result, dict):
                                tool_name = str(
                                    tool_result.get("tool")
                                    or tool_result.get("name")
                                    or ""
                                ).strip()
                            operation_id = str(
                                event_data.get("operation_id")
                                or event_data.get("tool_call_id")
                                or (
                                    tool_result.get("tool_call_id")
                                    if isinstance(tool_result, dict)
                                    else ""
                                )
                                or ""
                            ).strip()
                            operation_started_at = None
                            operation_signature = _agent_run_tool_operation_signature(
                                event_data
                            )
                            if event_type == "tool_start" and tool_name:
                                operation_id = operation_id or str(uuid4())
                                operation_started_at = time.monotonic()
                                active_tool_operations.setdefault(tool_name, []).append(
                                    (
                                        operation_id,
                                        operation_started_at,
                                        operation_signature,
                                    )
                                )
                            elif event_type == "tool_end" and tool_name:
                                queue = active_tool_operations.get(tool_name, [])
                                if not operation_id and queue:
                                    matching_index = next(
                                        (
                                            index
                                            for index, (
                                                _candidate,
                                                _started,
                                                signature,
                                            ) in enumerate(queue)
                                            if signature == operation_signature
                                        ),
                                        None,
                                    )
                                    if matching_index is not None:
                                        operation_id, operation_started_at, _ = queue.pop(
                                            matching_index
                                        )
                                    elif len(queue) == 1:
                                        operation_id, operation_started_at, _ = queue.pop(0)
                                elif operation_id:
                                    matching_index = next(
                                        (
                                            index
                                            for index, (
                                                candidate,
                                                _started,
                                                _signature,
                                            ) in enumerate(queue)
                                            if candidate == operation_id
                                        ),
                                        None,
                                    )
                                    if matching_index is not None:
                                        _, operation_started_at, _ = queue.pop(
                                            matching_index
                                        )
                                if not queue:
                                    active_tool_operations.pop(tool_name, None)
                                operation_id = operation_id or str(uuid4())
                            if operation_id:
                                event_data["operation_id"] = operation_id
                                if isinstance(tool_result, dict):
                                    tool_result = dict(tool_result)
                                    tool_result.setdefault("tool_call_id", operation_id)
                                    event_data["tool_result"] = tool_result
                            if session_id:
                                event_data["session_id"] = session_id
                            if agent_run_id:
                                event_data["agent_run_id"] = agent_run_id
                            # Construct one immutable safe projection after
                            # correlation enrichment.  The exact same copy is
                            # sent to durable audit and the WebSocket; raw
                            # provider text never crosses either boundary.
                            safe_event_data = sanitize_stream_display_payload(
                                event_type,
                                event_data,
                            )
                            await emitter.record_event(
                                f"stream.{event_type}",
                                safe_event_data,
                                status=safe_event_data.get("status"),
                                message_text=safe_event_data.get("message"),
                            )
                            tool_result = safe_event_data.get("tool_result")
                            if (
                                event_type == "tool_end"
                                and isinstance(tool_result, dict)
                                and not safe_event_data.get(
                                    "tool_result_already_recorded"
                                )
                            ):
                                search_tool_results.append(
                                    {
                                        key: value
                                        for key, value in tool_result.items()
                                        if key != "tool_call_id"
                                    }
                                )
                                if agent_run_service:
                                    tool_name = (
                                        tool_result.get("tool")
                                        or tool_result.get("name")
                                        or safe_event_data.get("tool")
                                        or safe_event_data.get("tool_name")
                                    )
                                    if tool_name:
                                        try:
                                            await agent_run_service.record_tool_call(
                                                agent_run_id,
                                                tool_name=str(tool_name),
                                                arguments=tool_result.get("arguments")
                                                or tool_result.get("args")
                                                or {},
                                                result=tool_result.get("output")
                                                or tool_result.get("result")
                                                or tool_result,
                                                success=(
                                                    not bool(tool_result.get("error"))
                                                    and (
                                                        tool_result.get("exit_code")
                                                        is None
                                                        or str(
                                                            tool_result.get("exit_code")
                                                        ).strip()
                                                        in {"0", "0.0"}
                                                    )
                                                ),
                                                mutation_confirmed=bool(
                                                    tool_result.get("mutation_confirmed")
                                                ),
                                                tool_call_id=(
                                                    operation_id
                                                    or (
                                                        str(
                                                            tool_result.get(
                                                                "tool_call_id"
                                                            )
                                                        )
                                                        if tool_result.get(
                                                            "tool_call_id"
                                                        )
                                                        else None
                                                    )
                                                ),
                                                duration_ms=(
                                                    max(
                                                        0,
                                                        int(
                                                            (
                                                                time.monotonic()
                                                                - operation_started_at
                                                            )
                                                            * 1000
                                                        ),
                                                    )
                                                    if operation_started_at is not None
                                                    else None
                                                ),
                                                metadata={
                                                    key: value
                                                    for key, value in tool_result.items()
                                                    if key
                                                    not in {
                                                        "tool",
                                                        "name",
                                                        "arguments",
                                                        "args",
                                                        "output",
                                                        "result",
                                                    }
                                                },
                                            )
                                        except Exception as record_error:
                                            print(
                                                "[TerminalMode] AgentRun tool record "
                                                f"failed: {record_error}"
                                            )
                            result = web_iface.broadcast_stream_event(
                                event_type, safe_event_data
                            )
                            if inspect.isawaitable(result):
                                await result
                        except Exception as e:
                            print(f"[TerminalMode] ストリーミングイベント送信エラー: {e}")

                    stream_callback = _stream_callback

                # System-owned Document/App workflows are resolved before
                # generic tool/Skill dispatch.  The workflow receives the
                # persisted raw request only for local processing; it builds
                # its own bounded protected projection before any Cloud
                # Advisor consultation.  Ordinary prose that does not meet
                # the conservative local intent detector stays on the normal
                # Main-model path below.
                try:
                    from ...services.workflow_controller import (
                        WorkflowController,
                        workflow_route_for_turn,
                    )

                    workflow_source = (
                        persist_content
                        if isinstance(persist_content, str)
                        else message
                    )
                    workflow_route = workflow_route_for_turn(
                        workflow_source,
                        attachments,
                    )
                except Exception:
                    # A missing/partial deployment must not turn an ordinary
                    # message into an unsafe fallback. Explicit workflow
                    # tokens are handled fail-closed by the controller's
                    # caller; here we simply leave normal generation intact.
                    workflow_route = None
                    first_workflow_token = (
                        workflow_source.strip().split(None, 1)[0].casefold()
                        if isinstance(workflow_source, str) and workflow_source.strip()
                        else ""
                    )
                    if first_workflow_token in {
                        "/document",
                        "/template",
                        "/app",
                        "/macro",
                    }:
                        raise RuntimeError("system workflow is not ready")

                if workflow_route is not None:
                    workflow_workspace_root = (
                        self.web_interface.server._resolve_workspace_root()
                        if self.web_interface
                        and callable(
                            getattr(
                                getattr(self.web_interface, "server", None),
                                "_resolve_workspace_root",
                                None,
                            )
                        )
                        else None
                    )
                    verified_workflow_paths: list[str] = []
                    try:
                        from ...api.server_parts.chat_message_mixin import (
                            _server_verified_project_attachment_items,
                        )

                        workflow_server = getattr(self.web_interface, "server", None)
                        if workflow_server is not None:
                            verified_workflow_paths = [
                                path
                                for _item, path in _server_verified_project_attachment_items(
                                    workflow_server,
                                    attachments,
                                    project_id,
                                    sender_user_id,
                                )
                            ]
                    except Exception:
                        # Attachment authorization is fail-closed in the
                        # controller when the server verification helper is
                        # unavailable; no path is treated as trusted here.
                        verified_workflow_paths = []

                    workflow_app_context_callback = None
                    if (
                        workflow_route.kind.value == "app"
                        and sender_user_id
                        and sender_user_id != "default_user"
                    ):
                        from ...services.workflow_controller import (
                            create_persistent_app_context,
                        )

                        async def _create_workflow_app_context(**_context_kwargs: Any):
                            # Keep raw source text out of App name/description
                            # metadata; the source remains local in the input
                            # problem IR and workspace.
                            return await create_persistent_app_context(
                                user_id=sender_user_id,
                                project_id=str(project_id) if project_id else None,
                                name=(
                                    "AoiTalk Macro"
                                    if workflow_route.command == "/macro"
                                    else "AoiTalk App"
                                ),
                                slug=(
                                    "aoitalk-macro-workflow-"
                                    + str(agent_run_id or uuid4().hex)[:12]
                                    if workflow_route.command == "/macro"
                                    else "aoitalk-app-workflow-"
                                    + str(agent_run_id or uuid4().hex)[:12]
                                ),
                                description="System-owned AoiTalk workflow App",
                                workspace_root=workflow_workspace_root,
                            )

                        workflow_app_context_callback = _create_workflow_app_context

                    async def _workflow_progress(
                        event_type: str,
                        payload: Mapping[str, Any],
                    ) -> None:
                        if not stream_callback:
                            return
                        safe_payload = {
                            "workflow": workflow_route.kind.value,
                            "stage": str(event_type or "progress")[:80],
                            "status": str(
                                payload.get("status")
                                if isinstance(payload, Mapping)
                                else "running"
                            )[:40],
                            "message": sanitize_assistant_display_text(
                                str(
                                    payload.get("message")
                                    if isinstance(payload, Mapping)
                                    else "ワークフローを処理しています"
                                )
                            )[:500],
                        }
                        await stream_callback("workflow_progress", safe_payload)

                    try:
                        controller = WorkflowController(
                            config=getattr(self, "config", None),
                            app_context_callback=workflow_app_context_callback,
                        )
                        workflow_kwargs: dict[str, Any] = {}
                        explicit_cloud_consent = (
                            getattr(cloud_advisor_origin, "value", cloud_advisor_origin)
                            == "user_explicit"
                        )
                        workflow_kwargs["cloud_consent"] = explicit_cloud_consent
                        if workflow_route.kind.value == "app":
                            workflow_kwargs.update(
                                {
                                    "run_tests": True,
                                    "run_app": True,
                                }
                            )
                        workflow_main_model = llm_client
                        # The long-lived terminal handler can still hold the
                        # startup/default Ollama client while a session has a
                        # different DB-backed Main route.  Workflows must use
                        # the effective configured local Main model rather
                        # than silently invoking a stale model (or a missing
                        # default) for design refinement.
                        configured_provider = str(
                            getattr(self.config, "get", lambda *_a: "")(
                                "llm_provider", ""
                            )
                            or ""
                        ).strip()
                        configured_model = str(
                            getattr(self.config, "get", lambda *_a: "")(
                                "llm_model", ""
                            )
                            or ""
                        ).strip()
                        if configured_provider and configured_model:
                            if not self._active_client_matches_response_model(
                                llm_client,
                                configured_provider,
                                configured_model,
                            ):
                                workflow_main_model = self._get_response_model_client(
                                    {
                                        "provider": configured_provider,
                                        "model": configured_model,
                                    },
                                    llm_client,
                                )
                        workflow_result = await controller.execute(
                            workflow_source,
                            attachments=attachments,
                            verified_attachment_paths=verified_workflow_paths,
                            session_id=session_id,
                            user_id=sender_user_id,
                            project_id=project_id,
                            main_model=workflow_main_model,
                            progress_callback=_workflow_progress,
                            route=workflow_route,
                            workspace_root=workflow_workspace_root,
                            **workflow_kwargs,
                        )
                        safe_workflow_result: dict[str, Any] = {}
                        if stream_callback:
                            to_dict = getattr(workflow_result, "to_dict", None)
                            if callable(to_dict):
                                try:
                                    candidate_result = to_dict()
                                    if isinstance(candidate_result, Mapping):
                                        safe_workflow_result = dict(candidate_result)
                                except Exception:
                                    safe_workflow_result = {}
                            elif isinstance(workflow_result, Mapping):
                                safe_workflow_result = dict(workflow_result)
                            # Result serializers intentionally expose only
                            # hashed/basename artifact metadata and masked
                            # diagnostics. Do not attach raw workflow state.
                            await stream_callback(
                                "workflow_result",
                                {
                                    "workflow": workflow_route.kind.value,
                                    "status": str(
                                        safe_workflow_result.get("status") or "completed"
                                    )[:40],
                                    "result": safe_workflow_result,
                                },
                            )
                        workflow_metadata: dict[str, Any] = {
                            "workflow": workflow_route.metadata,
                            "workflow_result": safe_workflow_result,
                        }
                        workflow_artifacts: list[dict[str, Any]] = []
                        if workflow_route.kind.value == "document":
                            try:
                                output_value = getattr(workflow_result, "output_path", None)
                                workspace_value = (
                                    Path(workflow_workspace_root).expanduser().resolve(strict=False)
                                    if workflow_workspace_root
                                    else None
                                )
                                if output_value is not None and workspace_value is not None:
                                    output_file = Path(output_value).expanduser().resolve(strict=True)
                                    relative = output_file.relative_to(workspace_value)
                                    if (
                                        output_file.is_file()
                                        and relative.parts
                                        and not any(part in {"", ".", ".."} for part in relative.parts)
                                        and output_file.suffix.casefold() in {".xlsx", ".xlsm", ".xltx", ".xltm"}
                                    ):
                                        safe_output_name = str(
                                            safe_workflow_result.get("output_name")
                                            or f"document_{hashlib.sha256(output_file.name.encode('utf-8', 'replace')).hexdigest()[:10]}_updated{output_file.suffix.casefold()}"
                                        )[:255]
                                        workflow_artifacts.append(
                                            {
                                                "name": safe_output_name,
                                                "filename": safe_output_name,
                                                "path": relative.as_posix(),
                                                "kind": "attachment",
                                                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                                "size": max(0, int(output_file.stat().st_size)),
                                                "sha256": str(
                                                    safe_workflow_result.get("artifact_sha256") or ""
                                                )[:64],
                                            }
                                        )
                            except (OSError, RuntimeError, ValueError):
                                # An artifact that cannot be proven to live in
                                # this authenticated workspace is not exposed
                                # as a downloadable Chat attachment.
                                workflow_artifacts = []
                        if workflow_artifacts:
                            workflow_metadata["attachments"] = workflow_artifacts[:8]
                            workflow_metadata["workflow_artifacts"] = workflow_artifacts[:8]
                        workflow_reply = (
                            workflow_result.get("user_message")
                            if isinstance(workflow_result, Mapping)
                            else getattr(workflow_result, "user_message", None)
                        )
                        if not isinstance(workflow_reply, str) or not workflow_reply.strip():
                            workflow_reply = (
                                workflow_result.get("message")
                                if isinstance(workflow_result, Mapping)
                                else getattr(workflow_result, "message", None)
                            )
                        workflow_reply = str(
                            workflow_reply or "ワークフローを完了しました。"
                        ).strip()
                        safe_response = await persist_assistant_reply(
                            workflow_reply,
                            include_generation_metrics=False,
                            metadata_extra=workflow_metadata,
                        )
                        workflow_status = (
                            workflow_result.get("status")
                            if isinstance(workflow_result, Mapping)
                            else getattr(workflow_result, "status", None)
                        )
                        workflow_ok = bool(
                            getattr(workflow_result, "ok", False)
                            or str(workflow_status or "").casefold()
                            in {"completed", "created", "local_fallback", "succeeded"}
                        )
                        if workflow_ok:
                            await emitter.complete(
                                workflow_reply,
                                llm_client,
                                completion_confirmed=True,
                            )
                        else:
                            await emitter.fail(
                                "system workflow returned a failed result",
                                safe_response,
                                llm_client,
                            )
                        if stream_callback:
                            await stream_callback(
                                "stream_end",
                                {
                                    "content": safe_response,
                                    "status": "completed" if workflow_ok else "failed",
                                },
                            )
                        if self.web_interface and not used_streaming:
                            self.web_interface.add_assistant_message(
                                safe_response,
                                session_id=session_id,
                                attachments=workflow_artifacts[:8],
                            )
                        return workflow_reply
                    except Exception as exc:
                        logger = logging.getLogger(__name__)
                        # Workflow inputs are untrusted local evidence; do
                        # not dump exception text/tracebacks into normal
                        # runtime logs where paths or source literals could
                        # escape the bounded workflow projection.
                        logger.error(
                            "System workflow failed kind=%s exception_type=%s",
                            workflow_route.kind.value,
                            type(exc).__name__,
                        )
                        workflow_failure = (
                            "ワークフローを完了できませんでした。入力ファイルと権限を確認して再試行してください。"
                        )
                        safe_failure = await persist_assistant_reply(
                            workflow_failure,
                            include_generation_metrics=False,
                            generate_title=False,
                        )
                        await emitter.fail(
                            "system workflow failed",
                            safe_failure,
                            llm_client,
                        )
                        if stream_callback:
                            await stream_callback(
                                "stream_end",
                                {
                                    "content": safe_failure,
                                    "status": "failed",
                                },
                            )
                        if self.web_interface and not used_streaming:
                            self.web_interface.add_assistant_message(
                                safe_failure,
                                session_id=session_id,
                            )
                        return workflow_failure

                if "work_intake" in normalized_command_capabilities:
                    work_intake_succeeded = True
                    try:
                        response = await self._run_work_intake_command(
                            llm_client=llm_client,
                            current_request=self._extract_command_current_request(
                                persist_content
                            ),
                            project_id=project_id,
                            sender_user_id=sender_user_id,
                            attachments=attachments,
                            stream_callback=stream_callback,
                            agent_run_service=agent_run_service,
                            agent_run_id=agent_run_id,
                            session_id=session_id,
                            source_message_id=(
                                str(user_message.id)
                                if user_message is not None
                                else persisted_user_message_id
                            ),
                            client_message_id=client_message_id,
                        )
                    except WorkIntakeHandledError as exc:
                        work_intake_succeeded = False
                        response = exc.user_response
                    except Exception as exc:
                        work_intake_succeeded = False
                        print(f"[TerminalMode] Work Inbox処理エラー: {exc}")
                        try:
                            from ...services.failure_recorder import record_failure_event

                            await record_failure_event(
                                source="backend",
                                operation="work_intake",
                                error=exc,
                                project_id=(str(project_id) if project_id else None),
                                conversation_id=session_id,
                                run_id=agent_run_id,
                                input_summary={
                                    "attachment_count": len(attachments or []),
                                    "has_text": bool(
                                        self._extract_command_current_request(
                                            persist_content
                                        ).strip()
                                    ),
                                },
                            )
                        except Exception as record_error:
                            print(
                                "[TerminalMode] Work Inboxエラー記録失敗: "
                                f"{record_error}"
                            )
                        response = (
                            "Work Inbox処理を完了できませんでした。"
                            "内部エラーを記録しました。"
                        )
                    safe_response = await persist_assistant_reply(
                        response,
                        generate_title=not suppress_automatic_context,
                    )
                    if work_intake_succeeded:
                        # /inbox専用ハンドラが正常復帰した場合は、その結果を
                        # 完了の信頼できる根拠とする。確認待ちや不足情報を
                        # 含む正当な応答を汎用の計画語検出で失敗へ戻さない。
                        await emitter.complete(
                            response,
                            llm_client,
                            completion_confirmed=True,
                        )
                    else:
                        await emitter.fail("Work intake failed", response, llm_client)
                    if stream_callback:
                        await stream_callback("stream_end", {"content": safe_response})
                    if self.web_interface and not used_streaming:
                        self.web_interface.add_assistant_message(safe_response)
                    return response

                if "web_search" in normalized_command_capabilities:
                    response = await self._run_required_web_search_command(
                        llm_client=llm_client,
                        current_request=self._extract_command_current_request(message),
                        prompt_history=prompt_history,
                        stream_callback=stream_callback,
                        agent_run_service=agent_run_service,
                        agent_run_id=agent_run_id,
                        search_tool_results=search_tool_results,
                    )
                    safe_response = await persist_assistant_reply(response)
                    await emitter.complete(response, llm_client)
                    if stream_callback:
                        await stream_callback("stream_end", {"content": safe_response})
                    print(f"{self.character_name}: {response}")
                    if self.web_interface and not used_streaming:
                        self.web_interface.add_assistant_message(
                            safe_response,
                            session_id=session_id,
                        )
                    if self.response_handler:
                        self.response_handler.llm_client = original_handler_client
                    if llm_client:
                        llm_client.current_session_id = None
                        llm_client.current_project_id = None
                        llm_client.generation_policy = generation_policy_for_profile(None)
                        llm_client.current_include_project_context = None
                        llm_client.current_edit_message_id = None
                        llm_client.current_response_model = None
                        llm_client.current_command_capabilities = ()
                        llm_client.current_tool_required = None
                        llm_client.external_persistence_enabled = False
                        restore_turn_user_context_on_client(
                            llm_client,
                            turn_user_context_snapshot,
                        )
                    return

                task_id = self.response_handler._generate_task_id()
                try:
                    generation_kwargs = {
                        "image_data": image_data,
                        "stream_callback": stream_callback,
                    }
                    try:
                        generation_signature = inspect.signature(
                            self.response_handler._generate_response_only
                        )
                        parameters = generation_signature.parameters
                        accepts_kwargs = any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters.values()
                        )
                        if accepts_kwargs or "evidence_user_input" in parameters:
                            generation_kwargs["evidence_user_input"] = persist_content
                        if "steering_callback" in generation_signature.parameters:
                            generation_kwargs["steering_callback"] = steering_callback
                    except (TypeError, ValueError):
                        pass
                    response = await self.response_handler._generate_response_only(
                        task_id,
                        llm_message,
                        "web",
                        **generation_kwargs,
                    )
                finally:
                    if self.response_handler:
                        self.response_handler.llm_client = original_handler_client
                    if llm_client:
                        llm_client.current_session_id = None
                        llm_client.current_project_id = None
                        llm_client.generation_policy = generation_policy_for_profile(None)
                        llm_client.current_include_project_context = None
                        llm_client.current_edit_message_id = None
                        llm_client.current_response_model = None
                        llm_client.current_command_capabilities = ()
                        llm_client.current_tool_required = None
                        llm_client.external_persistence_enabled = False
                        restore_turn_user_context_on_client(
                            llm_client,
                            turn_user_context_snapshot,
                        )

                mutation_evidence_getter = getattr(
                    emitter,
                    "authoritative_mutation_completion",
                    None,
                )
                if callable(mutation_evidence_getter):
                    mutation_evidence = mutation_evidence_getter(llm_client)
                    if inspect.isawaitable(mutation_evidence):
                        mutation_evidence = await mutation_evidence
                else:
                    # Compatibility fakes/older integrations may still supply
                    # the pre-evidence emitter; they must retain the ordinary
                    # failure path rather than breaking the whole chat turn.
                    mutation_evidence = None
                mutation_summary = (
                    mutation_evidence.get("summary")
                    if isinstance(mutation_evidence, Mapping)
                    else None
                )
                generation_failure_marker = getattr(
                    self.response_handler,
                    "last_generation_failure",
                    None,
                )
                provider_failure_text = (
                    str(mutation_evidence.get("provider_finalization_failure") or "")
                    if isinstance(mutation_evidence, Mapping)
                    else ""
                )
                tool_loop_failed_marker = bool(
                    getattr(llm_client, "_last_tool_loop_failed", False)
                    or getattr(llm_client, "_last_turn_tool_loop_failed", False)
                    or getattr(llm_client, "_last_turn_tool_rounds_exhausted", False)
                )
                response_tool_loop_failure = str(response or "").strip().startswith(
                    "ツール処理を完了できませんでした"
                )
                mutation_fallback_requested = bool(
                    mutation_summary
                    and (
                        not response
                        or generation_failure_marker is not None
                        or bool(provider_failure_text)
                        or tool_loop_failed_marker
                        or response_tool_loop_failure
                    )
                )
                if response and not mutation_fallback_requested:
                    safe_response = await persist_assistant_reply(response)
                    await emitter.complete(response, llm_client)
                    print(f"{self.character_name}: {response}")
                    if self.web_interface and not used_streaming:
                        self.web_interface.add_assistant_message(
                            safe_response, session_id=session_id
                        )
                else:
                    # A provider can fail while sampling only the final text
                    # after a deterministic mutation already committed.  Use
                    # the run-keyed tool ledger as the source of truth for a
                    # bounded task-create summary; do not make the user retry
                    # (which could create a duplicate).  The emitter keeps all
                    # tool audit rows, including any optional failure, and
                    # records the provider failure as diagnostic metadata.
                    if mutation_fallback_requested and isinstance(
                        mutation_summary, str
                    ) and mutation_summary.strip():
                        safe_mutation_summary = await persist_assistant_reply(
                            mutation_summary,
                            generate_title=False,
                        )
                        await emitter.complete(
                            mutation_summary,
                            llm_client,
                            completion_confirmed=True,
                            allow_authoritative_mutation=True,
                        )
                        if stream_callback:
                            await stream_callback(
                                "stream_end",
                                {
                                    "content": safe_mutation_summary,
                                    "status": "completed",
                                    "message": safe_mutation_summary,
                                },
                            )
                        if self.web_interface and not used_streaming:
                            self.web_interface.add_assistant_message(
                                safe_mutation_summary,
                                session_id=session_id,
                            )
                        print(f"{self.character_name}: {mutation_summary}")
                        return mutation_summary

                    # 失敗理由を分類し、ユーザーには原因と次の行動が分かる文言を返す。
                    failure = getattr(
                        self.response_handler, "last_generation_failure", None
                    ) or empty_response_failure()
                    failure_reply = failure.user_message
                    safe_failure_reply = await persist_assistant_reply(
                        failure_reply,
                        generate_title=False,
                    )
                    # agent_runs.error は従来通り技術詳細のまま記録する。
                    if failure.is_empty_response:
                        fail_detail = failure.technical_detail
                    else:
                        fail_detail = (
                            f"Assistant generation failed: {failure.technical_detail}"
                        )
                    await emitter.fail(
                        fail_detail,
                        failure_reply,
                        llm_client,
                    )
                    if stream_callback:
                        # WebUIの生成ステータスにも分類済みの理由を届ける。
                        await stream_callback(
                            "stream_end",
                            {
                                "content": safe_failure_reply,
                                "status": "failed",
                                "message": safe_failure_reply,
                                "error": failure.technical_detail,
                            },
                        )
                    if self.web_interface:
                        self.web_interface.add_assistant_message(
                            safe_failure_reply, session_id=session_id
                        )
                    print("応答の生成に失敗しました")
        except PlanningInteractionTerminated as termination:
            safe_reply = sanitize_assistant_display_text(termination.user_message)
            print(
                "[TerminalMode] 計画承認フローを終了しました: "
                f"{termination.reason}"
            )
            if session_id:
                try:
                    safe_reply = await persist_assistant_reply(
                        safe_reply,
                        generate_title=False,
                    )
                except Exception as persist_error:
                    print(
                        "[TerminalMode] 計画終了応答の保存に失敗しました: "
                        f"{persist_error}"
                    )
            await emitter.fail(
                termination.reason,
                safe_reply,
                llm_client,
                status=termination.agent_run_status,
            )
            if stream_callback:
                await stream_callback(
                    "stream_end",
                    {
                        "content": safe_reply,
                        "status": termination.agent_run_status,
                        "message": safe_reply,
                        "error": termination.reason,
                    },
                )
            if self.web_interface and not used_streaming:
                self.web_interface.add_assistant_message(
                    safe_reply,
                    session_id=session_id,
                )
            return safe_reply
        except Exception as e:
            print(f"チャットメッセージ処理エラー: {e}")
            error_reply = f"申し訳ありません。応答生成中にエラーが発生しました: {e}"
            # Never let persistence failure leave the display reply
            # uninitialized. Reserved Help turns also keep provider/DB
            # details out of the user-visible stream; the raw exception is
            # logged above for operators only.
            safe_error_reply = sanitize_assistant_display_text(error_reply)
            if suppress_automatic_context:
                safe_error_reply = (
                    "AoiTalk Helpの回答生成に失敗しました。"
                    "ガイドを確認できないため、しばらくしてから再試行してください。"
                )
            if session_id:
                try:
                    safe_error_reply = await persist_assistant_reply(
                        safe_error_reply,
                        generate_title=False,
                    )
                except Exception as persist_error:
                    print(
                        f"[TerminalMode] エラー応答の保存に失敗しました: {persist_error}"
                    )
            failure_detail = (
                "AoiTalk Help generation failed"
                if suppress_automatic_context
                else str(e)
            )
            await emitter.fail(failure_detail, safe_error_reply, llm_client)
            if self.web_interface:
                self.web_interface.add_assistant_message(
                    safe_error_reply,
                    session_id=session_id,
                )
        finally:
            if suppress_automatic_context:
                # Help priming intentionally clears provider-local history, but
                # that is a turn-local view.  Restore the ordinary client
                # transcript (including the legacy no-session path) instead
                # of destructively clearing the shared provider.
                _restore_isolated_provider_state(isolated_provider_state_snapshot)
                if handler_client_assigned and self.response_handler:
                    try:
                        # Setup can fail before the generation branch's inner
                        # finally restores this pointer (for example while
                        # entering the planning scope).  Do the outer rollback
                        # as well so the next ordinary turn cannot use Help's
                        # response-model client.
                        self.response_handler.llm_client = original_handler_client
                    except Exception:
                        pass
            if planning_scope_cm is not None:
                planning_scope_cm.__exit__(None, None, None)
            if persisted_message_context_token is not None:
                reset_turn_context(persisted_message_context_token)
            reset_turn_context(turn_context_token)
            if agent_run_context_token is not None:
                reset_current_agent_run_id(agent_run_context_token)

    async def _cleanup_mode_specific(self):
        """Cleanup terminal mode specific resources"""
        # No specific cleanup needed for terminal mode
        pass
