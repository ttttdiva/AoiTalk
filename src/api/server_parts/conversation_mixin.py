"""会話生成の制御・ステータス・ステアリング・音声ディスパッチ・
WebSocket ペイロード正規化関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403

from src.llm.generation_error import DEFAULT_GENERATION_FAILURE_MESSAGE


class ConversationMixin:
    """WebChatServer の会話制御メソッド群。"""

    def _queue_user_message(self, data: dict):
        """Process a REST-dispatched user message without blocking the client."""
        task = asyncio.create_task(self._handle_user_message_background(data))
        self._conversation_dispatch_tasks.add(task)
        task.add_done_callback(self._conversation_dispatch_tasks.discard)

    async def _attach_project_to_conversation_if_missing(
        self, session_id: Optional[str], project_id: Optional[str]
    ) -> None:
        if not session_id or not project_id:
            return
        try:
            parsed_project_id = UUID(str(project_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid conversation project_id: %s", project_id)
            return

        try:
            from ...memory.conversation_repository import ConversationRepository

            repository = ConversationRepository()
            conversation = await repository.get_session_by_id(
                session_id, with_messages=False
            )
            if conversation and conversation.project_id is None:
                await repository.update_session(
                    session_id,
                    touch_activity=False,
                    project_id=parsed_project_id,
                )
        except Exception:
            logger.exception(
                "Failed to attach project %s to conversation %s",
                project_id,
                session_id,
            )

    async def _handle_user_message_background(self, data: dict):
        try:
            await self._handle_user_message(data)
        except Exception:
            logger.exception("Failed to process queued conversation message")

    def _conversation_control_key(self, session_id: Optional[str]) -> str:
        session_key = str(session_id or "").strip()
        return session_key or "__default__"

    def _generation_status_key(self, session_id: Optional[str]) -> str:
        return self._conversation_control_key(session_id)

    def _ensure_generation_status_store(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_conversation_generation_status"):
            self._conversation_generation_status = {}
        return self._conversation_generation_status

    def _now_iso(self) -> str:
        return f"{datetime.utcnow().isoformat(timespec='milliseconds')}Z"

    def _extract_generation_status_message(self, data: Dict[str, Any]) -> Optional[str]:
        nested = data.get("data")
        nested_data = nested if isinstance(nested, dict) else {}
        for key in ("message", "content", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            nested_value = nested_data.get(key)
            if isinstance(nested_value, str) and nested_value.strip():
                return nested_value.strip()
        return None

    def _set_conversation_generation_status(
        self,
        session_id: Optional[str],
        *,
        running: bool,
        status: str,
        message: Optional[str] = None,
        active_tool: Optional[str] = None,
        agent_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = self._generation_status_key(session_id)
        store = self._ensure_generation_status_store()
        previous = store.get(key, {})
        now = self._now_iso()
        payload = {
            "session_id": session_id,
            "running": running,
            "status": status,
            "message": message,
            "active_tool": active_tool,
            "agent_run_id": agent_run_id or previous.get("agent_run_id"),
            "started_at": previous.get("started_at") if previous else now,
            "updated_at": now,
        }
        store[key] = payload
        return payload

    def get_conversation_generation_status(
        self, session_id: Optional[str]
    ) -> Dict[str, Any]:
        key = self._generation_status_key(session_id)
        status = self._ensure_generation_status_store().get(key)
        tasks = self._conversation_generation_tasks.get(key, set())
        running = any(not task.done() for task in tasks if hasattr(task, "done"))
        if status:
            return dict(status)
        return {
            "session_id": session_id,
            "running": running,
            "status": "running" if running else "idle",
            "message": "応答を生成しています" if running else None,
            "active_tool": None,
            "started_at": None,
            "updated_at": None,
        }

    def _update_generation_status_from_stream_event(
        self, event_type: str, data: Dict[str, Any]
    ) -> None:
        nested = data.get("data")
        nested_data = nested if isinstance(nested, dict) else {}
        session_id = data.get("session_id", nested_data.get("session_id"))
        if not session_id:
            return

        message = self._extract_generation_status_message(data)
        tool = data.get("tool", nested_data.get("tool"))
        active_tool = str(tool) if isinstance(tool, str) and tool else None
        raw_agent_run_id = data.get("agent_run_id", nested_data.get("agent_run_id"))
        agent_run_id = str(raw_agent_run_id) if raw_agent_run_id else None

        if event_type == "stream_start":
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status=str(data.get("status") or "running"),
                message=message or "応答を生成しています",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "tool_start":
            tool_message = message or (
                f"{active_tool} を実行しています"
                if active_tool
                else "ツールを実行しています"
            )
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="tool",
                message=tool_message,
                active_tool=active_tool,
                agent_run_id=agent_run_id,
            )
        elif event_type == "tool_end":
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="running",
                message=message or "ツール実行が完了しました",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type in {"status_update", "reasoning_progress", "steering_update"}:
            previous = self._ensure_generation_status_store().get(
                self._generation_status_key(session_id), {}
            )
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status=str(data.get("status") or event_type),
                message=message or previous.get("message") or "応答を生成しています",
                active_tool=previous.get("active_tool"),
                agent_run_id=agent_run_id,
            )
        elif event_type in {"stream_end", "response"}:
            failed = str(data.get("status") or nested_data.get("status") or "").lower() == "failed"
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="failed" if failed else "completed",
                # 失敗時は emitter が渡す分類済み文言を優先する。
                # 分類済み文言が無い場合だけ、原因を誤誘導しない既定文言を使う。
                message=message
                or (
                    DEFAULT_GENERATION_FAILURE_MESSAGE
                    if failed
                    else "応答生成が完了しました"
                ),
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "stream_cancelled":
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="cancelled",
                message=message or "応答生成を停止しました",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        elif event_type == "conversation_persisted" and data.get("role") == "assistant":
            self._set_conversation_generation_status(
                session_id,
                running=False,
                status="completed",
                message="応答を保存しました",
                active_tool=None,
                agent_run_id=agent_run_id,
            )

    def _register_conversation_generation_task(
        self, session_id: Optional[str], task: Any
    ) -> None:
        key = self._conversation_control_key(session_id)
        tasks = self._conversation_generation_tasks.setdefault(key, set())
        tasks.add(task)

        def _discard(done_task: Any) -> None:
            current_tasks = self._conversation_generation_tasks.get(key)
            no_current_tasks = False
            if current_tasks is not None:
                current_tasks.discard(done_task)
                if not current_tasks:
                    self._conversation_generation_tasks.pop(key, None)
                    no_current_tasks = True
            try:
                done_task.result()
            except (asyncio.CancelledError, FutureCancelledError):
                logger.info("Conversation generation cancelled: %s", key)
                if no_current_tasks:
                    self._set_conversation_generation_status(
                        session_id,
                        running=False,
                        status="cancelled",
                        message="応答生成を停止しました",
                        active_tool=None,
                    )
            except Exception:
                logger.exception("Conversation generation failed: %s", key)
                if no_current_tasks:
                    self._set_conversation_generation_status(
                        session_id,
                        running=False,
                        status="failed",
                        message="応答生成中にエラーが発生しました",
                        active_tool=None,
                    )
            else:
                if no_current_tasks:
                    current_status = self.get_conversation_generation_status(session_id)
                    if current_status.get("running"):
                        self._set_conversation_generation_status(
                            session_id,
                            running=False,
                            status="completed",
                            message="応答生成が完了しました",
                            active_tool=None,
                        )

        if hasattr(task, "add_done_callback"):
            task.add_done_callback(_discard)

    def _schedule_user_input_callback(
        self,
        *,
        message: str,
        persist_content: Optional[str] = None,
        image_data: Optional[dict],
        session_id: Optional[str],
        project_id: Optional[str],
        generation_profile: Optional[str],
        include_project_context: bool,
        edit_message_id: Optional[str],
        response_model: Optional[Dict[str, str]],
        client_message_id: Optional[str],
        attachments: List[Dict[str, Any]],
        attachment_context: Optional[str],
        media_recognition_metadata: Optional[List[Dict[str, Any]]] = None,
        skip_user_persistence: bool = False,
        persisted_user_message_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        assistant_sender_type: Optional[str] = None,
        assistant_sender_id: Optional[str] = None,
        assistant_sender_display_name: Optional[str] = None,
        sender_user_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
        response_started_at_monotonic: Optional[float] = None,
        command_capabilities: Optional[List[str]] = None,
        tools_required: Optional[bool] = None,
    ) -> None:
        if session_id:
            self._set_conversation_generation_status(
                session_id,
                running=True,
                status="queued",
                message="応答生成を開始しています",
                active_tool=None,
                agent_run_id=agent_run_id,
            )
        callback_coro = self.on_user_input(
            message,
            persist_content=persist_content,
            image_data=image_data,
            session_id=session_id,
            project_id=project_id,
            generation_profile=generation_profile,
            include_project_context=include_project_context,
            edit_message_id=edit_message_id,
            response_model=response_model,
            client_message_id=client_message_id,
            attachments=attachments,
            attachment_context=attachment_context,
            media_recognition_metadata=media_recognition_metadata,
            skip_user_persistence=skip_user_persistence,
            persisted_user_message_id=persisted_user_message_id,
            agent_run_id=agent_run_id,
            assistant_sender_type=assistant_sender_type,
            assistant_sender_id=assistant_sender_id,
            assistant_sender_display_name=assistant_sender_display_name,
            sender_user_id=sender_user_id,
            sender_display_name=sender_display_name,
            response_started_at_monotonic=response_started_at_monotonic,
            command_capabilities=command_capabilities,
            tools_required=tools_required,
        )
        if self.main_event_loop:
            future = asyncio.run_coroutine_threadsafe(
                callback_coro,
                self.main_event_loop,
            )
            self._register_conversation_generation_task(session_id, future)
        else:
            task = asyncio.create_task(callback_coro)
            self._register_conversation_generation_task(session_id, task)

    async def _handle_stop_generation(self, data: dict) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        key = self._conversation_control_key(session_id)
        tasks = list(self._conversation_generation_tasks.get(key, set()))
        cancelled = 0
        for task in tasks:
            if hasattr(task, "done") and task.done():
                continue
            if hasattr(task, "cancel"):
                task.cancel()
                cancelled += 1

        self._conversation_steering_queues.pop(key, None)
        current_status = self.get_conversation_generation_status(session_id)
        agent_run_id = current_status.get("agent_run_id")
        if agent_run_id:
            try:
                from ...services.agent_run_service import AgentRunService

                await AgentRunService().cancel_run(str(agent_run_id))
            except Exception:
                logger.exception("Failed to cancel agent run: %s", agent_run_id)
        event_data = {
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "status": "cancelled",
            "message": "応答生成を停止しました",
            "cancelled": cancelled,
        }
        await self.broadcast_stream_event("stream_cancelled", event_data)
        logger.info(
            "Stop generation requested: session=%s cancelled=%s", key, cancelled
        )
        return {"session_id": session_id, "cancelled": cancelled}

    async def _handle_steer_generation(self, data: dict) -> Dict[str, Any]:
        session_id = data.get("session_id") if isinstance(data, dict) else None
        message = str(data.get("message") or data.get("instruction") or "").strip()
        if not message:
            return {"session_id": session_id, "queued": False}

        key = self._conversation_control_key(session_id)
        self._conversation_steering_queues.setdefault(key, []).append(message)
        await self.broadcast_stream_event(
            "steering_update",
            {
                "session_id": session_id,
                "status": "queued",
                "message": "追加指示を受け取りました",
            },
        )
        logger.info("Steering instruction queued: session=%s", key)
        return {"session_id": session_id, "queued": True}

    def consume_generation_steering(self, session_id: Optional[str]) -> List[str]:
        key = self._conversation_control_key(session_id)
        return self._conversation_steering_queues.pop(key, [])

    def get_voice_input_session_id(self) -> Optional[str]:
        context = self.get_voice_input_session_context()
        return context.get("session_id") if context else None

    def get_voice_input_session_context(self) -> Optional[Dict[str, Optional[str]]]:
        context_resolver = getattr(self.manager, "get_latest_session_context", None)
        if callable(context_resolver):
            return context_resolver()

        resolver = getattr(self.manager, "get_latest_session_id", None)
        if callable(resolver):
            session_id = resolver()
            if session_id:
                return {"session_id": session_id, "user_id": None}
        return None

    async def dispatch_voice_message(self, message: str) -> bool:
        """Route a local voice transcription into the active WebUI chat session."""
        text = str(message or "").strip()
        if not text:
            return False

        context = self.get_voice_input_session_context()
        session_id = context.get("session_id") if context else None
        if not session_id:
            logger.warning("Voice input skipped WebUI dispatch: no active session")
            return False
        sender_user_id = str((context or {}).get("user_id") or "default_user")

        await self._handle_user_message(
            {
                "message": text,
                "session_id": session_id,
                "_sender_user_id": sender_user_id,
                "_sender_display_name": sender_user_id,
            }
        )
        return True

    def _normalize_websocket_images(self, raw_images: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_images, list):
            return []
        return normalize_image_payloads(raw_images)

    def _normalize_websocket_audio(self, raw_audio: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw_audio, dict):
            return None
        payload = raw_audio.get("data") or raw_audio.get("dataUrl")
        if not isinstance(payload, str) or not payload:
            return None
        return {
            "data": payload,
            "mimeType": raw_audio.get("mimeType") or raw_audio.get("mime_type"),
            "name": raw_audio.get("name"),
        }

    def _main_model_supports_vision(self) -> bool | None:
        provider = str(self.config.get("llm_provider", "") or "").strip()
        model = str(self.config.get("llm_model", "") or "").strip()
        return model_supports_vision(provider, model)
