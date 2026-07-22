"""メディア認識・ユーザーメッセージ処理・共有グループメッセージ関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403


async def _resolve_authorized_skill_slash_command(
    message: str,
    *,
    project_id: Optional[str],
    sender_user_id: str,
) -> Optional[str]:
    """Resolve a slash skill using only a project visible to the sender."""
    if not message.lstrip().startswith("/"):
        return None

    from ...services.project_context import ProjectContextResolver
    from ...skills.slash import resolve_skill_slash_command

    authorized_project_id: Optional[str] = None
    if project_id:
        try:
            context = await ProjectContextResolver().get_project_context(
                str(project_id),
                user_id=sender_user_id,
            )
        except Exception as exc:
            # Project skill discovery must fail closed without preventing a
            # matching global skill from being resolved.
            logger.warning("Project skill authorization failed: %s", exc)
        else:
            if context:
                authorized_project_id = str(context.get("id") or "").strip() or None

    return resolve_skill_slash_command(
        message,
        project_id=authorized_project_id,
    )


class ChatMessageMixin:
    """WebChatServer のメッセージ処理メソッド群。"""

    async def _prepare_media_recognition(
        self,
        *,
        llm_message: str,
        images: list[dict[str, Any]],
        audio: Optional[dict[str, Any]],
        attachment_context: Optional[str],
        session_id: Optional[str],
    ) -> tuple[Optional[dict], Optional[str], list[dict[str, Any]]]:
        """Return (direct_image_data, augmented_attachment_context, metadata)."""
        direct_image_data: Optional[dict] = {"images": images} if images else None
        results: list[Any] = []
        metadata: list[dict[str, Any]] = []
        image_mode = str(
            self.config.get("model_routing.media.image_mode", "auto") or "auto"
        ).strip()
        should_delegate_images = False
        if images:
            vision_route = self.config.get("model_routing.classes.vision", {}) or {}
            vision_is_explicit = bool(
                not vision_route.get("inherit")
                and vision_route.get("provider")
                and vision_route.get("model")
            )
            if image_mode == "off":
                direct_image_data = None
            elif image_mode == "always":
                should_delegate_images = True
                direct_image_data = None
            elif vision_is_explicit:
                should_delegate_images = True
                direct_image_data = None
            elif self._main_model_supports_vision() is not True:
                should_delegate_images = True
                direct_image_data = None

        service = MediaRecognitionService(self.config)
        if should_delegate_images:
            await self.broadcast_stream_event(
                "status_update",
                {
                    "session_id": session_id,
                    "stage": "media_recognition",
                    "status": "image",
                    "message": "画像を解析中…",
                },
            )
            image_results = await service.recognize_images(llm_message, images)
            results.extend(image_results)
        if audio:
            await self.broadcast_stream_event(
                "status_update",
                {
                    "session_id": session_id,
                    "stage": "media_recognition",
                    "status": "audio",
                    "message": "音声を解析中…",
                },
            )
            results.append(await service.recognize_audio(llm_message, audio))

        if results:
            attachment_context = inject_media_recognition_results(
                attachment_context,
                results,
            )
            metadata = [result.to_metadata() for result in results if hasattr(result, "to_metadata")]
        return direct_image_data, attachment_context, metadata

    async def _handle_user_message(self, data: dict):
        """Handle user message with optional image, session_id, and project_id"""
        message = data.get("message", "").strip()
        raw_user_message = message
        raw_response_started_at = data.get("_response_started_at_monotonic")
        response_started_at_monotonic = (
            raw_response_started_at
            if isinstance(raw_response_started_at, (int, float))
            else time.monotonic()
        )
        images = self._normalize_websocket_images(data.get("images"))
        audio_data = self._normalize_websocket_audio(data.get("audio"))
        image_data = {"images": images} if images else None
        session_id = data.get("session_id")  # Extract session_id from message data
        agent_run_id = data.get("agent_run_id")
        if not isinstance(agent_run_id, str) or not agent_run_id:
            agent_run_id = None
        project_id = data.get("project_id")  # Extract project_id from message data
        requested_include_project_context = data.get("include_project_context") is True
        include_project_context = effective_include_project_context(
            message=message,
            requested=requested_include_project_context,
        )
        await self._attach_project_to_conversation_if_missing(session_id, project_id)
        edit_message_id = data.get("edit_message_id")
        response_model = sanitize_response_model_selection(data.get("response_model"))
        client_message_id = data.get("client_message_id")
        if not isinstance(client_message_id, str) or not client_message_id:
            client_message_id = None
        skip_user_persistence = data.get("skip_user_persistence") is True
        persisted_user_message_id = data.get("persisted_user_message_id")
        if not isinstance(persisted_user_message_id, str) or not persisted_user_message_id:
            persisted_user_message_id = None
        attachments = sanitize_chat_attachments(data.get("attachments"))
        attachment_context = data.get("attachment_context")
        if not isinstance(attachment_context, str):
            attachment_context = None
        mentions = data.get("mentions", [])  # @mentions: [{type, id, name}]
        sender_user_id = str(data.get("_sender_user_id") or "default_user")
        sender_display_name = str(
            data.get("_sender_display_name")
            or data.get("_sender_user_id")
            or "default_user"
        )
        generation_profile = resolve_generation_profile(
            data.get("generation_profile")
        ).value
        command_capabilities = command_capabilities_for_current_turn_text(
            raw_user_message,
            sanitize_command_capabilities(data.get("command_capabilities")),
        )
        tools_required = data.get("tools_required")
        if not isinstance(tools_required, bool):
            tools_required = None

        # 生成プロファイルをセッションデータに保存（同一プロセス内の参照用）
        if generation_profile:
            if not hasattr(self, "_session_generation_profiles"):
                self._session_generation_profiles = {}
            if session_id:
                self._session_generation_profiles[session_id] = generation_profile

        if not message and not image_data and not audio_data and not attachments and not attachment_context:
            return

        if session_id and not agent_run_id:
            try:
                from ...services.agent_run_service import AgentRunService

                agent_run = await AgentRunService().create_run(
                    session_id=session_id,
                    user_id=sender_user_id,
                    project_id=project_id,
                    trigger_message_id=persisted_user_message_id,
                    objective=message,
                    run_type="chat_turn",
                    generation_profile=generation_profile,
                    metadata={
                        "client_message_id": client_message_id,
                        "include_project_context": include_project_context,
                        "requested_include_project_context": (
                            requested_include_project_context
                        ),
                        "command_capabilities": list(command_capabilities),
                        "tools_required": tools_required,
                        "edit_message_id": edit_message_id,
                        "response_model": response_model,
                        "attachment_count": len(attachments),
                        "dispatch_source": "server_fallback",
                    },
                )
                agent_run_id = str(agent_run["id"])
            except Exception:
                logger.exception("Failed to create fallback agent run")

        # @メンション処理: ファイル参照の内容をメッセージに追加
        if mentions and FILE_EXPLORER_AVAILABLE:
            mention_context_parts = []
            for mention in mentions:
                m_type = mention.get("type")
                m_id = mention.get("id", "")
                m_name = mention.get("name", "")
                if m_type == "file":
                    try:
                        result = explorer_get_full_content(m_id)
                        if result.get("success"):
                            content_text = result["content"]
                            # 大きすぎるファイルは先頭だけ
                            if len(content_text) > 10000:
                                content_text = content_text[:10000] + "\n...(省略)"
                            mention_context_parts.append(
                                f"[参照ファイル: {m_name}]\n```\n{content_text}\n```"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to read mentioned file {m_id}: {e}")
                elif m_type == "task":
                    mention_context_parts.append(f"[参照タスク: {m_name} (ID: {m_id})]")
                elif m_type == "project":
                    mention_context_parts.append(
                        f"[参照プロジェクト: {m_name} (ID: {m_id})]"
                    )

            if mention_context_parts:
                message = message + "\n\n" + "\n\n".join(mention_context_parts)

        # スラッシュコマンドによるスキル明示呼び出し
        # 先頭が /skill名 のとき、LLM自動判断を待たずスキルを強制発火する。
        # 表示・永続化は生の入力のまま、LLM へ渡すメッセージのみ展開する。
        llm_message = message
        if message:
            skill_prompt = await _resolve_authorized_skill_slash_command(
                message,
                project_id=project_id,
                sender_user_id=sender_user_id,
            )
            if skill_prompt is not None:
                llm_message = skill_prompt

        if command_capabilities:
            llm_message = build_command_capability_context(
                llm_message,
                command_capabilities,
            )
        else:
            llm_message = protect_untrusted_command_context(llm_message)

        image_data, attachment_context, media_recognition_metadata = (
            await self._prepare_media_recognition(
                llm_message=llm_message,
                images=images,
                audio=audio_data,
                attachment_context=attachment_context,
                session_id=session_id,
            )
        )

        # Set Knowledge Workspace project context for this message
        if KNOWLEDGE_PROJECT_CONTEXT_AVAILABLE and set_knowledge_project_context:
            set_knowledge_project_context(project_id)

        # Log session ID and project ID for debugging
        log_parts = [f"User message: {raw_user_message}"]
        if image_data:
            log_parts.append(f"(with images:{len(images)})")
        if audio_data:
            log_parts.append("(with audio)")
        if session_id:
            log_parts.append(f"[session_id: {session_id}]")
        if project_id:
            log_parts.append(f"[project_id: {project_id}]")
        if include_project_context:
            log_parts.append("[project_context:on]")
        if attachments:
            log_parts.append(f"[attachments:{len(attachments)}]")
        if command_capabilities:
            log_parts.append(f"[commands:{','.join(command_capabilities)}]")
        if not session_id:
            log_parts.append("[new conversation]")
        logger.info(" ".join(log_parts))

        if session_id and await self._handle_shared_group_message(
            session_id=session_id,
            message=llm_message,
            persist_content=raw_user_message,
            command_capabilities=list(command_capabilities),
            project_id=project_id,
            sender_user_id=sender_user_id,
            sender_display_name=sender_display_name,
            generation_profile=generation_profile,
            client_message_id=client_message_id,
            attachments=attachments,
            has_image=bool(image_data),
            image_data=image_data,
            attachment_context=attachment_context,
            media_recognition_metadata=media_recognition_metadata,
        ):
            return

        # Create message entry with image info for display
        user_entry = {
            "type": "user",
            "message": raw_user_message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "session_id": session_id,
            "has_image": bool(image_data),
            "image_preview": images[0].get("data") if images else None,
            "client_message_id": client_message_id,
            "attachments": sanitize_chat_attachments(attachments, include_binary=False),
            "media_recognition": media_recognition_metadata,
            "command_capabilities": list(command_capabilities),
        }

        # Broadcast to clients
        self.manager.add_to_history(user_entry)
        await self.manager.broadcast({"type": "new_message", "data": user_entry})
        if skip_user_persistence and persisted_user_message_id:
            await self.broadcast_stream_event(
                "conversation_persisted",
                {
                    "session_id": session_id,
                    "role": "user",
                    "message_id": persisted_user_message_id,
                },
            )

        # Call user input callback with session_id and project_id
        if self.on_user_input:
            try:
                self._schedule_user_input_callback(
                    message=llm_message,
                    persist_content=raw_user_message,
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
                    sender_user_id=sender_user_id,
                    sender_display_name=sender_display_name,
                    response_started_at_monotonic=response_started_at_monotonic,
                    command_capabilities=list(command_capabilities),
                    tools_required=tools_required,
                )
            except Exception as e:
                logger.error(f"Callback error: {e}")
                await self.add_assistant_message(
                    f"エラーが発生しました: {str(e)}", session_id=session_id
                )

    async def _handle_shared_group_message(
        self,
        *,
        session_id: str,
        message: str,
        persist_content: Optional[str] = None,
        command_capabilities: Optional[List[str]] = None,
        project_id: Optional[str],
        sender_user_id: str,
        sender_display_name: str,
        generation_profile: Optional[str],
        client_message_id: Optional[str],
        attachments: List[Dict[str, Any]],
        has_image: bool,
        image_data: Optional[dict],
        attachment_context: Optional[str],
        media_recognition_metadata: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Persist and fan out a shared group message if the session is shared.

        DB へは生入力（``persist_content``）を保存し、LLM / GroupChatManager へは
        展開済みの ``message`` を渡す。
        """
        try:
            from ...memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)
            if not session or not getattr(session, "is_group_chat", False):
                return False
            if not await repo.user_has_session_access(session_id, sender_user_id):
                logger.warning("Shared group access denied: %s", session_id)
                return True

            metadata: Dict[str, Any] = {
                "client_message_id": client_message_id,
                "attachments": sanitize_chat_attachments(
                    attachments,
                    include_binary=False,
                ),
                "has_image": has_image,
            }
            if command_capabilities:
                metadata["command_capabilities"] = list(command_capabilities)
            if image_data:
                images = normalize_image_payloads(image_data)
                metadata["image_count"] = len(images)
                if images:
                    metadata["image_mime_type"] = images[0].get("mimeType")
                    metadata["image_name"] = images[0].get("name")
            if media_recognition_metadata:
                metadata["media_recognition"] = media_recognition_metadata
            persisted = await repo.add_message(
                session_id=session_id,
                role="user",
                content=persist_content if persist_content is not None else message,
                metadata={k: v for k, v in metadata.items() if v is not None},
                sender_type="user",
                sender_id=sender_user_id,
                sender_display_name=sender_display_name,
            )
            await self.broadcast_stream_event(
                "conversation_persisted",
                {
                    "session_id": session_id,
                    "role": "user",
                    "message_id": str(persisted.id),
                },
            )

            participants = await repo.get_session_participants(session_id)
            character_slugs = [
                p.participant_id
                for p in participants
                if p.participant_type == "character"
                and p.status in {"joined", "invited"}
                and p.auto_respond
            ]
            agent_ids = [
                p.participant_id
                for p in participants
                if p.participant_type == "agent"
                and p.status in {"joined", "invited"}
                and p.auto_respond
            ]

            if character_slugs:
                from ...llm.group_chat_manager import GroupChatManager

                messages = await repo.get_session_messages(session_id, limit=50)
                history = []
                for item in messages:
                    sender = item.sender_display_name or (
                        (item.message_metadata or {}).get("character_name")
                    )
                    content = item.content
                    if sender:
                        content = f"[{sender}]: {content}"
                    history.append({"role": item.role, "content": content})

                manager = GroupChatManager(
                    config=self.config,
                    character_slugs=character_slugs,
                )
                response_input = build_message_with_attachment_context(
                    message,
                    attachment_context,
                )
                responses = await manager.generate_responses(
                    user_message=response_input,
                    history=history,
                    strategy="round_robin",
                )
                for response in responses:
                    saved = await repo.add_message(
                        session_id=session_id,
                        role="assistant",
                        content=response["content"],
                        metadata={"character_name": response["character_slug"]},
                        sender_type="character",
                        sender_id=response["character_slug"],
                        sender_display_name=response.get("character_name"),
                    )
                    await self.broadcast_stream_event(
                        "conversation_persisted",
                        {
                            "session_id": session_id,
                            "role": "assistant",
                            "message_id": str(saved.id),
                        },
                    )

            if agent_ids and self.on_user_input:
                self._schedule_user_input_callback(
                    message=message,
                    image_data=image_data,
                    session_id=session_id,
                    project_id=project_id,
                    generation_profile="autonomous_work",
                    include_project_context=bool(project_id),
                    edit_message_id=None,
                    response_model=None,
                    client_message_id=None,
                    attachments=attachments,
                    attachment_context=attachment_context,
                    media_recognition_metadata=media_recognition_metadata,
                    skip_user_persistence=True,
                    assistant_sender_type="agent",
                    assistant_sender_id=agent_ids[0],
                    assistant_sender_display_name=agent_ids[0],
                    sender_user_id=sender_user_id,
                    sender_display_name=sender_display_name,
                    response_started_at_monotonic=time.monotonic(),
                )

            return True
        except Exception:
            logger.exception("Shared group message handling failed")
            await self.add_system_message(
                "グループチャットの送信処理でエラーが発生しました。"
            )
            return True
