"""
Terminal mode for AoiTalk Voice Assistant Framework
"""

import asyncio
import copy
import inspect
from typing import Any, Dict, Optional
from ..base import BaseAssistant
from ..response_handler import ResponseHandler
from ..chat_turn_persistence import ChatTurnPersistence
from ..chat_attachment_utils import (
    build_message_with_attachment_context,
    sanitize_chat_attachments,
)
from ..conversation_title_events import maybe_generate_and_broadcast_session_title
from ...llm.generation_policy import generation_policy_for_profile
from ...runtime_features import runtime_feature_manager


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
            "gemini-cli": ("gemini_cli.model",),
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
    ) -> dict:
        metadata = {}
        if llm_client and hasattr(llm_client, "_get_memory_metadata"):
            try:
                metadata.update(llm_client._get_memory_metadata() or {})
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
        assistant_sender_type=None,
        assistant_sender_id=None,
        assistant_sender_display_name=None,
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
        chat_persistence = self._get_chat_turn_persistence(llm_client)
        user_message = None

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
                assistant_message = await chat_persistence.save_assistant_message(
                    session_id=session_id,
                    content=reply,
                    metadata=self._get_chat_turn_metadata(llm_client),
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
                    config=self.config,
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
                        return
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")

            async with self._chat_turn_lock:
                base_llm_client = self._get_active_llm_client()
                llm_client = self._get_response_model_client(
                    response_model,
                    base_llm_client,
                )
                chat_persistence = self._get_chat_turn_persistence(base_llm_client)
                original_handler_client = getattr(
                    self.response_handler,
                    "llm_client",
                    None,
                )
                if self.response_handler:
                    self.response_handler.llm_client = llm_client
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
                    if session_id:
                        llm_client.current_session_id = session_id
                        print(f"[TerminalMode] Set session_id for message storage: {session_id}")
                    if project_id:
                        llm_client.current_project_id = project_id
                        print(f"[TerminalMode] Set project_id for session creation: {project_id}")
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
                            event_data = dict(data)
                            if session_id:
                                event_data["session_id"] = session_id
                            result = web_iface.broadcast_stream_event(
                                event_type, event_data
                            )
                            if inspect.isawaitable(result):
                                await result
                        except Exception as e:
                            print(f"[TerminalMode] ストリーミングイベント送信エラー: {e}")

                    stream_callback = _stream_callback

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
                        llm_client.external_persistence_enabled = False

                if response:
                    await persist_assistant_reply(response)
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
            if self.web_interface:
                self.web_interface.add_assistant_message(
                    error_reply,
                    session_id=session_id,
                )

    async def _cleanup_mode_specific(self):
        """Cleanup terminal mode specific resources"""
        # No specific cleanup needed for terminal mode
        pass
