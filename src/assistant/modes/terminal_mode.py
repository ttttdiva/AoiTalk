"""
Terminal mode for AoiTalk Voice Assistant Framework
"""

import asyncio
import copy
import inspect
import re
import time
from typing import Any, Dict, Optional
from ..base import BaseAssistant
from ..response_handler import ResponseHandler
from ..chat_turn_persistence import (
    ChatTurnPersistence,
    apply_turn_user_context_to_client,
    restore_turn_user_context_on_client,
)
from ..chat_attachment_utils import (
    build_message_with_attachment_context,
    sanitize_chat_attachments,
)
from ..conversation_title_events import maybe_generate_and_broadcast_session_title
from ...llm.context_budget import clip_text
from ...llm.agentic_completion import response_looks_like_unfinished_work
from ...llm.generation_policy import generation_policy_for_profile
from ...llm.tool_policy import (
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    command_capabilities_from_text,
    sanitize_command_capabilities,
)
from ...runtime_features import runtime_feature_manager
from ...services.agent_run_service import (
    AgentRunService,
    reset_current_agent_run_id,
    set_current_agent_run_id,
)
from ...services.agent_team_service import (
    AGENT_TEAM_MEMBER_LABELS,
    agent_team_delegate_member,
    agent_team_member_for,
    config_get,
)


_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_SEARCH_URL_LIMIT = 20
_AGENT_RUN_DELEGATION_TOOL_MEMBERS = {
    "advanced_reasoning_assistant": "advanced_reasoning",
    "utility_assistant": "utility",
    "media_assistant": "media",
    "spotify_assistant": "spotify",
    "scenario_assistant": "scenario",
    "writing_assistant": "writing",
    "import_assistant": "import",
}
_PROVIDER_MODEL_KEYS = {
    "openai": ("openai.model",),
    "gemini": ("gemini.model",),
    "openai_compatible_local": (
        "openai_compatible_local.model",
        "openai_compatible_local_model",
    ),
    "sglang": ("sglang.model", "sglang_model"),
    "openrouter": ("openrouter.model", "openrouter_model"),
    "ollama": ("ollama.model", "ollama_model"),
    "codex-cli": ("codex_cli.model",),
    "claude-cli": ("claude_cli.model",),
    "antigravity-cli": ("antigravity_cli.model",),
}


def _extract_search_tool_urls(output_text: str) -> list[str]:
    urls: list[str] = []
    for match in _SEARCH_TOOL_URL_RE.finditer(str(output_text or "")):
        url = match.group(0).rstrip(".,;:!?")
        if url not in urls:
            urls.append(url)
        if len(urls) >= _SEARCH_URL_LIMIT:
            break
    return urls


def _config_text(config: Any, key: str, default: str = "") -> str:
    return str(config_get(config, key, default) or "").strip()


def _main_agent_run_provider(config: Any) -> str:
    provider = _config_text(config, "llm_provider", "openai").lower()
    return provider or "openai"


def _main_agent_run_model(config: Any, provider: str) -> str:
    selected = _config_text(config, "llm_model")
    if selected:
        return selected
    for key in _PROVIDER_MODEL_KEYS.get(provider, ()):
        value = _config_text(config, key)
        if value:
            return value
    return ""


def _agent_run_member_context(config: Any, member_key: str) -> dict[str, str]:
    provider = ""
    model = ""
    member = agent_team_member_for(config, member_key)
    if member:
        provider = str(member.get("provider") or "").strip()
        model = str(member.get("model") or "").strip()
    if not provider:
        provider = _main_agent_run_provider(config)
    if not model:
        model = _main_agent_run_model(config, provider)
    return {
        "actor_type": "agent_team",
        "actor_key": member_key,
        "actor_label": AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key),
        "provider": provider,
        "model": model,
    }


def _agent_run_tool_context(config: Any, data: dict[str, Any]) -> dict[str, str]:
    tool_result = data.get("tool_result")
    tool_name = str(data.get("tool") or data.get("tool_name") or "").strip()
    if not tool_name and isinstance(tool_result, dict):
        tool_name = str(tool_result.get("tool") or tool_result.get("name") or "").strip()
    if not tool_name:
        return {}

    if tool_name == "agent_team_delegate":
        tool_args = (
            data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
        )
        member = agent_team_delegate_member(config, str(tool_args.get("role") or ""))
        if not member:
            return {}
        member_key = str(member.get("member_key") or tool_args.get("role") or "").strip()
        provider = str(member.get("provider") or "").strip()
        provider = provider or _main_agent_run_provider(config)
        model = str(member.get("model") or "").strip()
        model = model or _main_agent_run_model(config, provider)
        label = str(
            member.get("label")
            or AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key)
            or member_key
        )
        return {
            "actor_type": "agent_team",
            "actor_key": member_key,
            "actor_label": label,
            "provider": provider,
            "model": model,
        }

    member_key = _AGENT_RUN_DELEGATION_TOOL_MEMBERS.get(tool_name)
    if member_key:
        return _agent_run_member_context(config, member_key)
    return {}


def _enrich_agent_run_event_payload(
    config: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(data or {})
    context = _agent_run_tool_context(config, payload)
    if not context:
        return payload

    payload.setdefault("actor_type", context.get("actor_type"))
    payload.setdefault("actor_key", context.get("actor_key"))
    payload.setdefault("agent_member_key", context.get("actor_key"))
    payload.setdefault("actor_label", context.get("actor_label"))
    payload.setdefault("agent_label", context.get("actor_label"))
    payload.setdefault("provider", context.get("provider"))
    payload.setdefault("model", context.get("model"))

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        tool_result = dict(tool_result)
        tool_result.setdefault("actor_type", context.get("actor_type"))
        tool_result.setdefault("actor_key", context.get("actor_key"))
        tool_result.setdefault("actor_label", context.get("actor_label"))
        tool_result.setdefault("provider", context.get("provider"))
        tool_result.setdefault("model", context.get("model"))
        payload["tool_result"] = tool_result
    return payload


def _client_tool_calls(client) -> list[Any]:
    calls = getattr(client, "_last_tool_calls", None)
    if not calls:
        return []
    return list(calls)


def _agent_run_tool_call_payload(call: Any) -> dict[str, Any]:
    raw_arguments = getattr(call, "arguments", {}) or {}
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    return {
        "tool": str(getattr(call, "tool", "") or getattr(call, "name", "")),
        "arguments": dict(arguments),
        "result": str(getattr(call, "result", "") or getattr(call, "output", "")),
        "successful": bool(getattr(call, "successful", True)),
    }


def _agent_run_completion_result(
    *,
    reply: Optional[str],
    search_tool_results: list[dict[str, Any]],
    tool_calls: list[Any],
) -> dict[str, Any]:
    result_payload: dict[str, Any] = {
        "assistant_response": reply or "",
        "tool_result_count": len(search_tool_results) + len(tool_calls),
    }
    if tool_calls:
        result_payload["tool_calls"] = [
            _agent_run_tool_call_payload(call) for call in tool_calls
        ]
    return result_payload


def _should_fail_agent_run_completion(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
) -> bool:
    if _looks_like_cli_execution_error(reply):
        return True
    if search_tool_result_count > 0 and str(reply or "").strip():
        return False
    return response_looks_like_unfinished_work(user_input, reply)


def _looks_like_cli_execution_error(reply: Optional[str]) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    lowered = text.lower()
    cli_markers = (
        "cli error:",
        "cli execution failed",
        "cli returned no output",
        "returned no output from print mode",
        "codex cli error",
        "codex cli failed",
        "antigravity cli returned no output",
        "antigravity cli error",
        "antigravity cli failed",
        "gemini cli returned no output",
        "gemini cli error",
        "gemini cli failed",
    )
    if any(marker in lowered for marker in cli_markers):
        return True
    return lowered.startswith("エラーが発生しました:") and "cli" in lowered


def _agent_run_completion_failure_message(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
) -> Optional[str]:
    if _looks_like_cli_execution_error(reply):
        return "CLI execution failed"
    if search_tool_result_count > 0 and str(reply or "").strip():
        return None
    if response_looks_like_unfinished_work(user_input, reply):
        return "Assistant response did not complete the requested work"
    return None


class TerminalMode(BaseAssistant):
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
        self._response_model_clients: dict[tuple[str, str], Any] = {}

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
        if base_llm_client and self._active_client_matches_response_model(
            base_llm_client,
            provider,
            model,
        ):
            return base_llm_client

        cached = self._response_model_clients.get(identity)
        if cached is not None:
            return cached

        from ...llm.manager import create_llm_client

        client = create_llm_client(
            self._clone_config_for_response_model(provider, model)
        )
        personality = self.character_config.get("personality", {})
        system_prompt = personality.get(
            "details",
            "あなたは親切なAIアシスタントです。",
        )
        if hasattr(client, "set_system_prompt"):
            client.set_system_prompt(system_prompt)
        self._response_model_clients[identity] = client
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
    ) -> dict:
        metadata = {}
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
        sanitized_attachments = sanitize_chat_attachments(attachments)
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        if sanitized_attachments:
            metadata["attachments"] = sanitized_attachments
        if image_data:
            metadata.update(
                {
                    "has_image": True,
                    "image_mime_type": image_data.get("mimeType"),
                    "image_name": image_data.get("name"),
                }
            )
        return metadata

    async def _broadcast_conversation_persisted(
        self,
        *,
        session_id: Optional[str],
        role: str,
        message_id: Optional[str] = None,
    ) -> None:
        if not self.web_interface or not session_id:
            return
        broadcaster = getattr(self.web_interface, "broadcast_stream_event", None)
        if not broadcaster:
            return
        result = broadcaster(
            "conversation_persisted",
            {"session_id": session_id, "role": role, "message_id": message_id},
        )
        if inspect.isawaitable(result):
            await result

    def _setup_keyword_detection(self):
        """キーワード検出システムをセットアップ"""
        try:
            from ...tools.keyword.initializer import setup_keyword_detection
            setup_keyword_detection(self.config)
        except Exception as e:
            print(f"[TerminalMode] キーワード検出システムの初期化に失敗: {e}")
            # エラーが発生してもターミナルモードは動作を続行
        
    async def _initialize_mode_specific(self) -> bool:
        """Initialize terminal mode specific components"""
        print("[ターミナルモード] テキストチャットモードで開始")
        print("[ターミナルモード] TTS初期化をスキップ")
        
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
            print("🌐 Webチャットインターフェースを開始しました (テキスト専用)")
            print(f"📍 ブラウザで以下のURLにアクセスしてください: {server_url}")
            if self.web_interface:
                self.web_interface.set_voice_recognition_ready(False)
                self.web_interface.set_recording_state(False)
                self.web_interface.update_rms(0.0)
                self.web_interface.add_system_message("🖥️ ターミナルモード: 音声なしでチャットできます")
                self.web_interface.add_assistant_message(greeting)
        else:
            print("⚠️ Webインターフェースは利用できません（ターミナルのみ）")

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
                return raw.rsplit(marker, 1)[-1].strip()
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
            try:
                return str(
                    await asyncio.to_thread(
                        llm_client.chat,
                        messages,
                        tools_enabled=False,
                        fallback_user_input=prompt,
                    )
                )
            except TypeError:
                return str(await asyncio.to_thread(llm_client.chat, messages))
        if hasattr(llm_client, "generate_response_async"):
            had_caps = hasattr(llm_client, "current_command_capabilities")
            previous_caps = getattr(llm_client, "current_command_capabilities", None)
            try:
                llm_client.current_command_capabilities = ()
                return str(await llm_client.generate_response_async(prompt))
            finally:
                if had_caps:
                    llm_client.current_command_capabilities = previous_caps
                elif hasattr(llm_client, "current_command_capabilities"):
                    delattr(llm_client, "current_command_capabilities")
        if hasattr(llm_client, "generate_response"):
            return str(
                await asyncio.to_thread(
                    llm_client.generate_response,
                    prompt,
                    stream=False,
                )
            )
        return ""

    async def _run_required_web_search_command(
        self,
        *,
        llm_client: Any,
        current_request: str,
        prompt_history: list[dict[str, str]],
        stream_callback,
        agent_run_service: Optional[AgentRunService],
        agent_run_id: Optional[str],
        search_tool_results: list[dict[str, Any]],
    ) -> str:
        history_text = self._format_command_prompt_history(prompt_history)
        query_prompt = "\n".join(
            [
                "次の会話履歴と現在の要求から、web_search に渡す検索クエリを1つだけ作ってください。",
                "回答文は書かず、検索クエリだけを返してください。",
                "",
                "会話履歴:",
                history_text or "(なし)",
                "",
                "現在の要求:",
                current_request,
            ]
        )
        raw_query = await self._plain_llm_response_for_command(
            llm_client,
            query_prompt,
        )
        query = self._sanitize_generated_search_query(
            raw_query,
            fallback_request=current_request,
            prompt_history=prompt_history,
        )

        if stream_callback:
            await stream_callback(
                "tool_start",
                {
                    "tool": "web_search",
                    "tool_args": {"query": query},
                    "message": "web_search を実行しています",
                },
            )

        from ...tools.basic.web_search import web_search_with_config

        search_output = await asyncio.to_thread(
            web_search_with_config,
            query,
            self.config,
        )
        search_output_text = str(search_output or "")
        tool_result = {
            "tool": "web_search",
            "query": query,
            "arguments": {"query": query},
            "output": search_output_text,
        }
        urls = _extract_search_tool_urls(search_output_text)
        if urls:
            tool_result["urls"] = urls
        search_tool_results.append(tool_result)

        if agent_run_service and agent_run_id:
            try:
                await agent_run_service.record_tool_call(
                    agent_run_id,
                    tool_name="web_search",
                    arguments={"query": query},
                    result=str(search_output or ""),
                    success=bool(str(search_output or "").strip()),
                    metadata={"source": "slash_command"},
                )
            except Exception as exc:
                print(f"[TerminalMode] AgentRun required web_search record failed: {exc}")

        if stream_callback:
            await stream_callback(
                "tool_end",
                {
                    "tool": "web_search",
                    "tool_args": {"query": query},
                    "tool_result": tool_result,
                    "tool_result_already_recorded": True,
                    "message": "web_search が完了しました",
                },
            )

        answer_prompt = "\n".join(
            [
                "以下は /search コマンドで強制実行されたWeb検索結果です。",
                "検索結果を根拠に、日本語で直接回答してください。",
                "Tool Hints やツール利用指示は出力しないでください。",
                "",
                "会話履歴:",
                history_text or "(なし)",
                "",
                "現在の要求:",
                current_request,
                "",
                f"検索クエリ: {query}",
                "",
                "検索結果:",
                clip_text(str(search_output or ""), 8000),
            ]
        )
        answer = (
            await self._plain_llm_response_for_command(llm_client, answer_prompt)
        ).strip()
        if not answer or "Tool Hints" in answer:
            answer = "\n\n".join(
                [
                    f"検索クエリ: {query}",
                    "検索結果:",
                    clip_text(str(search_output or ""), 1800),
                ]
            )
        return answer

    async def _process_user_message_web(
        self,
        message: str,
        image_data=None,
        session_id=None,
        project_id=None,
        generation_profile=None,
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
    ):
        """Process user message sent from the WebUI
        
        Args:
            message: User message text
            image_data: Optional image data {data: base64, mimeType: str, name: str}
            session_id: Optional conversation session ID from frontend
            project_id: Optional project ID from frontend
        """
        llm_client = self._get_active_llm_client()
        llm_message = build_message_with_attachment_context(
            message,
            attachment_context,
        )
        normalized_command_capabilities = sanitize_command_capabilities(
            command_capabilities
        )
        if not normalized_command_capabilities:
            normalized_command_capabilities = tuple(
                command_capabilities_from_text(message)
            )
        chat_persistence = self._get_chat_turn_persistence(llm_client)
        user_message = None
        search_tool_results: list[dict[str, Any]] = []
        agent_run_service = AgentRunService() if agent_run_id else None
        agent_run_finished = False
        agent_run_context_token = (
            set_current_agent_run_id(agent_run_id) if agent_run_id else None
        )

        def _agent_run_model_context(client) -> dict[str, Any]:
            provider = None
            backend = getattr(client, "cli_backend", None)
            if backend and hasattr(backend, "get_provider_name"):
                try:
                    provider = backend.get_provider_name()
                except Exception:
                    provider = None
            provider = provider or getattr(client, "provider", None)
            provider = provider or getattr(client, "provider_label", None)
            model = getattr(backend, "_model", None) if backend else None
            model = (
                model
                or getattr(client, "model_name", None)
                or getattr(client, "model", None)
            )
            if str(model or "").strip().lower() == "default":
                model = None
            return {
                "provider": str(provider) if provider else None,
                "model": str(model) if model else None,
            }

        def _event_payload(data: dict[str, Any]) -> dict[str, Any]:
            payload = dict(data or {})
            for key in ("content", "delta", "text", "output"):
                value = payload.get(key)
                if isinstance(value, str) and len(value) > 4000:
                    payload[key] = value[:4000].rstrip() + "\n... (truncated)"
            return payload

        async def _safe_agent_run_event(
            event_type: str,
            data: dict[str, Any] | None = None,
            *,
            status: str | None = None,
            message_text: str | None = None,
        ) -> None:
            if not agent_run_service:
                return
            try:
                await agent_run_service.record_event(
                    agent_run_id,
                    event_type,
                    status=status,
                    message=message_text,
                    payload=_event_payload(data or {}),
                )
            except Exception as exc:
                print(f"[TerminalMode] AgentRun event record failed: {exc}")

        async def _safe_agent_run_start(client) -> None:
            if not agent_run_service:
                return
            model_context = _agent_run_model_context(client)
            try:
                await agent_run_service.mark_running(
                    agent_run_id,
                    message="Assistant generation started",
                    metadata={
                        "session_id": session_id,
                        "project_id": project_id,
                        "generation_profile": generation_profile,
                        "include_project_context": include_project_context,
                        "command_capabilities": list(
                            normalized_command_capabilities
                        ),
                    },
                    provider=model_context["provider"],
                    model=model_context["model"],
                )
            except Exception as exc:
                print(f"[TerminalMode] AgentRun start update failed: {exc}")

        async def _safe_agent_run_complete(
            reply: Optional[str],
            client=None,
        ) -> None:
            nonlocal agent_run_finished
            if not agent_run_service or agent_run_finished:
                return
            agent_run_finished = True
            try:
                tool_calls = _client_tool_calls(client)
                for call in tool_calls:
                    payload = _agent_run_tool_call_payload(call)
                    if not payload["tool"]:
                        continue
                    await agent_run_service.record_tool_call(
                        agent_run_id,
                        tool_name=payload["tool"],
                        arguments=payload["arguments"],
                        result=payload["result"],
                        success=payload["successful"],
                        mutation_confirmed=payload["tool"]
                        in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
                    )

                result_payload = _agent_run_completion_result(
                    reply=reply,
                    search_tool_results=search_tool_results,
                    tool_calls=tool_calls,
                )
                failure_message = _agent_run_completion_failure_message(
                    user_input=message,
                    reply=reply,
                    search_tool_result_count=len(search_tool_results),
                )
                if failure_message:
                    await agent_run_service.fail_run(
                        agent_run_id,
                        failure_message,
                        result=result_payload,
                    )
                    return
                await agent_run_service.complete_run(
                    agent_run_id,
                    result=result_payload,
                    message="Assistant generation completed",
                )
            except Exception as exc:
                print(f"[TerminalMode] AgentRun completion update failed: {exc}")

        async def _safe_agent_run_fail(
            error_text: str,
            reply: Optional[str] = None,
        ) -> None:
            nonlocal agent_run_finished
            if not agent_run_service or agent_run_finished:
                return
            agent_run_finished = True
            try:
                await agent_run_service.fail_run(
                    agent_run_id,
                    error_text,
                    result={"assistant_response": reply or ""},
                )
            except Exception as exc:
                print(f"[TerminalMode] AgentRun failure update failed: {exc}")

        await _safe_agent_run_start(llm_client)

        if session_id and not skip_user_persistence:
            try:
                user_message = await chat_persistence.save_user_message(
                    session_id=session_id,
                    content=message,
                    metadata=self._get_chat_turn_metadata(
                        llm_client,
                        image_data,
                        attachments,
                        client_message_id,
                    ),
                    branch_from_message_id=edit_message_id,
                    sender_type="user" if sender_user_id else None,
                    sender_id=sender_user_id,
                    sender_display_name=sender_display_name,
                )
                await self._broadcast_conversation_persisted(
                    session_id=session_id,
                    role="user",
                    message_id=str(user_message.id) if user_message else None,
                )
            except Exception as e:
                print(f"[TerminalMode] ユーザーメッセージ保存エラー: {e}")

        async def persist_assistant_reply(reply: Optional[str]) -> None:
            if not reply or not session_id:
                return
            try:
                metadata = self._get_chat_turn_metadata(
                    llm_client,
                    include_generation_metrics=True,
                )
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
                assistant_message = await chat_persistence.save_assistant_message(
                    session_id=session_id,
                    content=reply,
                    metadata=metadata,
                    sender_type=assistant_sender_type,
                    sender_id=assistant_sender_id,
                    sender_display_name=assistant_sender_display_name,
                )
                await self._broadcast_conversation_persisted(
                    session_id=session_id,
                    role="assistant",
                    message_id=str(assistant_message.id) if assistant_message else None,
                )
                await maybe_generate_and_broadcast_session_title(
                    web_interface=self.web_interface,
                    session_id=session_id,
                    chat_persistence=chat_persistence,
                    config=getattr(self, "config", None),
                    log_prefix="TerminalMode",
                )
            except Exception as e:
                print(f"[TerminalMode] アシスタントメッセージ保存エラー: {e}")

        try:
            try:
                from ...tools.keyword import process_keywords

                keyword_result = process_keywords(message)
                if keyword_result and keyword_result.detected:
                    if isinstance(keyword_result.message, dict):
                        msg_data = keyword_result.message
                        mode = msg_data.get("mode", "")

                        if mode == "selection_mode" and "goodbye_reply" in msg_data:
                            reply = msg_data["goodbye_reply"]
                            print(f"{self.character_name}: {reply}")
                            print(f"\n{msg_data.get('message', '')}")
                            if self.web_interface:
                                self.web_interface.add_assistant_message(
                                    reply, session_id=session_id
                                )
                                self.web_interface.add_system_message(msg_data.get("message", ""))
                            await persist_assistant_reply(reply)
                            await _safe_agent_run_complete(reply)
                            return

                        if mode == "character_switched" and "greeting" in msg_data:
                            from ...tools.keyword.character_manager import get_character_manager

                            manager = get_character_manager()
                            self.character_name = manager.get_current_character()
                            reply = msg_data["greeting"]
                            print(f"\n{msg_data.get('message', '')}")
                            print(f"{self.character_name}: {reply}")
                            if self.web_interface:
                                self.web_interface.add_system_message(msg_data.get("message", ""))
                                self.web_interface.add_assistant_message(
                                    reply, session_id=session_id
                                )
                            await persist_assistant_reply(reply)
                            await _safe_agent_run_complete(reply)
                            return

                        reply = msg_data.get("message", "")
                        if reply:
                            print(reply)
                            if self.web_interface:
                                self.web_interface.add_assistant_message(
                                    reply, session_id=session_id
                                )
                            await persist_assistant_reply(reply)

                    elif keyword_result.message:
                        reply = keyword_result.message
                        print(reply)
                        if self.web_interface:
                            self.web_interface.add_assistant_message(
                                reply, session_id=session_id
                            )
                        await persist_assistant_reply(reply)

                    if keyword_result.bypass_llm:
                        await _safe_agent_run_complete(locals().get("reply"))
                        return
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")

            async with self._chat_turn_lock:
                base_llm_client = self._get_active_llm_client()
                llm_client = self._get_response_model_client(
                    response_model,
                    base_llm_client,
                )
                await _safe_agent_run_start(llm_client)
                chat_persistence = self._get_chat_turn_persistence(base_llm_client)
                original_handler_client = getattr(
                    self.response_handler,
                    "llm_client",
                    None,
                )
                if self.response_handler:
                    self.response_handler.llm_client = llm_client
                turn_user_context_snapshot = None
                prompt_history: list[dict[str, str]] = []
                if llm_client and session_id:
                    exclude_message_id = (
                        str(user_message.id)
                        if user_message
                        else persisted_user_message_id
                    )
                    prompt_history = await chat_persistence.load_prompt_history(
                        session_id=session_id,
                        exclude_message_id=exclude_message_id,
                    )
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
                    if session_id:
                        llm_client.current_session_id = session_id
                        print(f"[TerminalMode] Set session_id for message storage: {session_id}")
                    if project_id:
                        llm_client.current_project_id = project_id
                        print(f"[TerminalMode] Set project_id for session creation: {project_id}")
                    llm_client.current_command_capabilities = (
                        normalized_command_capabilities
                    )
                    llm_client.generation_policy = generation_policy_for_profile(
                        generation_profile
                    )
                    llm_client.current_include_project_context = bool(
                        include_project_context
                    )
                    llm_client.current_edit_message_id = edit_message_id
                    llm_client.current_response_model = response_model
                    llm_client.external_persistence_enabled = bool(
                        user_message or (skip_user_persistence and session_id)
                    )

                stream_callback = None
                steering_callback = None
                used_streaming = False
                supports_streaming = bool(
                    llm_client and hasattr(llm_client, "_run_streamed_with_callback")
                )
                if (
                    session_id
                    and self.web_interface
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
                    supports_streaming
                    and self.web_interface
                    and hasattr(self.web_interface, "broadcast_stream_event")
                ):
                    web_iface = self.web_interface

                    async def _stream_callback(event_type: str, data: dict):
                        nonlocal used_streaming
                        used_streaming = True
                        try:
                            event_data = _enrich_agent_run_event_payload(
                                getattr(self, "config", None),
                                dict(data),
                            )
                            if session_id:
                                event_data["session_id"] = session_id
                            if agent_run_id:
                                event_data["agent_run_id"] = agent_run_id
                            await _safe_agent_run_event(
                                f"stream.{event_type}",
                                event_data,
                                status=event_data.get("status"),
                                message_text=event_data.get("message"),
                            )
                            tool_result = event_data.get("tool_result")
                            if (
                                event_type == "tool_end"
                                and isinstance(tool_result, dict)
                                and not event_data.get("tool_result_already_recorded")
                            ):
                                search_tool_results.append(tool_result)
                                if agent_run_service:
                                    tool_name = (
                                        tool_result.get("tool")
                                        or tool_result.get("name")
                                        or event_data.get("tool")
                                        or event_data.get("tool_name")
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
                                                success=not bool(tool_result.get("error")),
                                                mutation_confirmed=bool(
                                                    tool_result.get("mutation_confirmed")
                                                ),
                                                tool_call_id=(
                                                    str(tool_result.get("tool_call_id"))
                                                    if tool_result.get("tool_call_id")
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
                                                        "error",
                                                    }
                                                },
                                            )
                                        except Exception as record_error:
                                            print(
                                                "[TerminalMode] AgentRun tool record "
                                                f"failed: {record_error}"
                                            )
                            result = web_iface.broadcast_stream_event(
                                event_type, event_data
                            )
                            if inspect.isawaitable(result):
                                await result
                        except Exception as e:
                            print(f"[TerminalMode] ストリーミングイベント送信エラー: {e}")

                    stream_callback = _stream_callback

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
                    await persist_assistant_reply(response)
                    await _safe_agent_run_complete(response, llm_client)
                    if stream_callback:
                        await stream_callback("stream_end", {"content": response})
                    print(f"{self.character_name}: {response}")
                    if self.web_interface and not used_streaming:
                        self.web_interface.add_assistant_message(
                            response,
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
                        llm_client.external_persistence_enabled = False
                        restore_turn_user_context_on_client(
                            llm_client,
                            turn_user_context_snapshot,
                        )

                if response:
                    await persist_assistant_reply(response)
                    await _safe_agent_run_complete(response, llm_client)
                    print(f"{self.character_name}: {response}")
                    if self.web_interface and not used_streaming:
                        self.web_interface.add_assistant_message(
                            response, session_id=session_id
                        )
                else:
                    failure_reply = (
                        "応答生成に失敗しました。LLMサーバーまたはモデル設定を確認してください。"
                    )
                    await persist_assistant_reply(failure_reply)
                    await _safe_agent_run_fail(
                        "Assistant generation returned no response",
                        failure_reply,
                    )
                    if self.web_interface:
                        self.web_interface.add_assistant_message(
                            failure_reply, session_id=session_id
                        )
                    print("応答の生成に失敗しました")
        except Exception as e:
            print(f"チャットメッセージ処理エラー: {e}")
            error_reply = f"申し訳ありません。応答生成中にエラーが発生しました: {e}"
            if session_id:
                try:
                    await persist_assistant_reply(error_reply)
                except Exception as persist_error:
                    print(
                        f"[TerminalMode] エラー応答の保存に失敗しました: {persist_error}"
                    )
            await _safe_agent_run_fail(str(e), error_reply)
            if self.web_interface:
                self.web_interface.add_assistant_message(
                    error_reply,
                    session_id=session_id,
                )
        finally:
            if agent_run_context_token is not None:
                reset_current_agent_run_id(agent_run_context_token)

    async def _cleanup_mode_specific(self):
        """Cleanup terminal mode specific resources"""
        # No specific cleanup needed for terminal mode
        pass
