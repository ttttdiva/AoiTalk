"""ブロードキャスト・メッセージ追加・各種コールバック設定・LLM クライアント/モード・
外部 LLM 許可・キャラクター切り替え・音声ステータス関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403


class MessagingMixin:
    """WebChatServer のメッセージング/ライフサイクル系メソッド群。"""

    async def _handle_clear_chat(self):
        """Handle clear chat request"""
        self.manager.clear_history()
        await self.manager.broadcast({"type": "chat_cleared"})
        logger.info("Chat history cleared")

        # Call the clear chat callback to start a new session
        if self.on_clear_chat:
            try:
                self.on_clear_chat()
            except Exception as e:
                logger.error(f"Clear chat callback error: {e}")

    def _init_external_llm_permission_manager(self):
        """Initialize the external LLM permission manager"""
        if not EXTERNAL_LLM_PERMISSION_AVAILABLE:
            return

        try:
            # Create permission manager with config
            self._external_llm_permission_manager = ExternalLLMPermissionManager(
                self.config
            )

            # Set broadcast callback
            async def broadcast_permission_request(message: dict):
                target_loop = self._permission_broadcast_loop
                current_loop = asyncio.get_running_loop()
                if (
                    target_loop
                    and target_loop.is_running()
                    and target_loop is not current_loop
                ):
                    future = asyncio.run_coroutine_threadsafe(
                        self.manager.broadcast(message),
                        target_loop,
                    )
                    await asyncio.wrap_future(future)
                    return
                await self.manager.broadcast(message)

            self._external_llm_permission_manager.set_broadcast_callback(
                broadcast_permission_request
            )

            # Register as global instance
            set_permission_manager(self._external_llm_permission_manager)

            logger.info("[WebChatServer] External LLM permission manager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize external LLM permission manager: {e}")

    async def _handle_external_llm_permission_response(self, data: dict):
        """Handle user response to external LLM permission request"""
        if not self._external_llm_permission_manager:
            logger.warning("External LLM permission manager not available")
            return

        request_id = data.get("request_id")
        approved = data.get("approved", False)

        if not request_id:
            logger.warning("Permission response missing request_id")
            return

        self._external_llm_permission_manager.handle_permission_response(
            request_id, approved
        )
        logger.info(
            f"External LLM permission response: {request_id} -> {'approved' if approved else 'denied'}"
        )

    async def _handle_external_model_prompt_response(self, data: dict):
        """Handle approval or edited prompt for an external model call."""
        if not self._external_llm_permission_manager:
            logger.warning("External LLM permission manager not available")
            return

        request_id = data.get("request_id")
        approved = bool(data.get("approved", False))
        prompt = str(data.get("prompt") or "")

        if not request_id:
            logger.warning("External model prompt response missing request_id")
            return

        self._external_llm_permission_manager.handle_external_model_prompt_response(
            request_id,
            approved,
            prompt,
        )
        logger.info(
            "External model prompt response: %s -> %s",
            request_id,
            "approved" if approved else "denied",
        )

    async def _handle_set_llm_mode(self, data: dict):
        """Handle LLM mode change from WebSocket"""
        mode = str(data.get("mode", "fast")).strip()
        state = build_llm_mode_state(self.config, client=self._llm_client)
        available_modes = state.get("available_modes") or []

        if mode not in available_modes:
            logger.warning(f"Invalid LLM mode: {mode}")
            return

        def _apply_config(key: str, next_value: Any) -> None:
            if hasattr(self.config, "save_to_file"):
                if not self.config.save_to_file(key, next_value):
                    raise RuntimeError(f"Failed to persist {key}")
            else:
                self.config.set(key, next_value)

        provider = str(state.get("provider") or "").strip()
        kind = str(state.get("kind") or "response_mode")
        if kind == "reasoning_effort":
            if provider == "codex-cli":
                _apply_config("codex_cli.reasoning_effort", mode)
            elif provider == "claude-cli":
                _apply_config("claude_cli.reasoning_effort", mode)
            elif provider == "openai":
                _apply_config("openai.reasoning_effort", mode)
            from ...llm.manager import create_llm_client

            self.set_llm_client(create_llm_client(self.config))
        elif (
            self._llm_client
            and hasattr(self._llm_client, "set_llm_mode")
        ):
            self._llm_client.set_llm_mode(mode)

        # Store mode for reference
        self._current_llm_mode = mode
        self.config.set("llm_runtime_mode", mode)

        # Broadcast to all clients
        await self.manager.broadcast(
            {
                "type": "llm_mode_change",
                "data": build_llm_mode_state(self.config, client=self._llm_client),
            }
        )

        logger.info(f"LLM mode set via WebSocket: {mode}")

    async def broadcast_stream_event(self, event_type: str, data: dict):
        """Broadcast streaming events (stream_start, stream_token, tool_start, etc.)"""
        self._update_generation_status_from_stream_event(event_type, data)
        await self.manager.broadcast({"type": event_type, **data})

    async def add_assistant_message(
        self, message: str, session_id: Optional[str] = None
    ):
        """Add assistant message"""
        entry = {
            "type": "assistant",
            "message": message,
            "character": self.character_name,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "session_id": session_id,
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"Assistant: {message}")

    async def add_system_message(self, message: str):
        """Add system message"""
        entry = {
            "type": "system",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"System: {message}")

    async def add_user_message(self, message: str):
        """Add user message (for voice input)"""
        # Check for duplicate messages
        current_time = time.time()
        if (
            message == self._last_user_message
            and current_time - self._last_user_message_time < self._duplicate_threshold
        ):
            logger.info(f"Duplicate user message ignored: {message}")
            return

        # Update last message tracking
        self._last_user_message = message
        self._last_user_message_time = current_time

        entry = {
            "type": "user",
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

        self.manager.add_to_history(entry)
        await self.manager.broadcast({"type": "new_message", "data": entry})
        logger.info(f"User (voice): {message}")

    def set_user_input_callback(self, callback, event_loop=None):
        """Set user input callback"""
        self.on_user_input = callback
        self.main_event_loop = event_loop

    def set_clear_chat_callback(self, callback):
        """Set clear chat callback (called when user starts a new conversation)"""
        self.on_clear_chat = callback

    def set_llm_client_change_callback(self, callback):
        """Set callback called when the active LLM client changes."""
        self.on_llm_client_change = callback

    def set_llm_client(self, llm_client):
        """Set LLM client reference for mode management

        Args:
            llm_client: LLM client instance (SGLangClient, AgentLLMClient, etc.)
        """
        self._llm_client = llm_client
        logger.info(f"LLM client set: {type(llm_client).__name__}")
        if self.on_llm_client_change:
            try:
                self.on_llm_client_change(llm_client)
            except Exception as exc:
                logger.exception("LLM client change callback failed: %s", exc)

        # HeartbeatRunnerにもLLMクライアントとブロードキャスト関数を注入
        if self._heartbeat_runner:
            self._heartbeat_runner.set_llm_client(llm_client)
            self._heartbeat_runner.set_broadcast_fn(self.manager.broadcast)
    def _register_character_switch_callback(self):
        """キャラクター切り替え通知を登録"""
        try:
            character_manager = CharacterSwitchManager()
            character_manager.register_callback(self._on_character_switch)
            logger.info("WebChatServer: キャラクター切り替えコールバックを登録しました")
        except Exception as e:
            logger.error(
                f"WebChatServer: キャラクター切り替えコールバック登録エラー: {e}"
            )
    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """キャラクター切り替え時のコールバック"""
        try:
            logger.info(
                f"WebChatServer: キャラクター切り替えを受信 - {self.character_name} -> {character_name}"
            )
            old_character = self.character_name
            self.character_name = character_name

            # WebSocketで接続中のクライアントに通知
            if hasattr(self, "manager") and self.manager:
                try:
                    # 実行中のイベントループを取得
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.manager.broadcast(
                            {
                                "type": "character_switch",
                                "data": {
                                    "old_character": old_character,
                                    "new_character": character_name,
                                    "yaml_filename": yaml_filename,
                                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                                },
                            }
                        )
                    )
                except RuntimeError:
                    # イベントループが実行されていない場合
                    logger.warning(
                        "WebChatServer: イベントループが実行されていないため、WebSocket通知をスキップします"
                    )

            logger.info(
                f"WebChatServer: キャラクター名を更新しました - {character_name}"
            )

        except Exception as e:
            logger.error(f"WebChatServer: キャラクター切り替え処理エラー: {e}")

    def set_voice_recognition_ready(self, ready: bool):
        """Set voice recognition ready state"""
        self.voice_recognition_ready = ready
        # Broadcast to all clients
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast(
                        {
                            "type": "voice_status_change",
                            "data": {
                                "ready": ready,
                                "rms": self.current_rms,
                                "recording": self.is_recording,
                            },
                        }
                    )
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass

    def update_rms(self, rms: float):
        """Update microphone RMS level"""
        self.current_rms = rms
        # Broadcast to all clients
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast({"type": "rms_update", "data": {"rms": rms}})
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass

    def set_recording_state(self, recording: bool):
        """Set recording state"""
        self.is_recording = recording
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self.manager.broadcast(
                        {
                            "type": "voice_status_change",
                            "data": {
                                "ready": self.voice_recognition_ready,
                                "rms": self.current_rms,
                                "recording": recording,
                            },
                        }
                    )
                )
        except RuntimeError:
            # No event loop, skip broadcast
            pass
