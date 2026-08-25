"""
Response handling for AoiTalk Voice Assistant Framework
"""

import asyncio
import inspect
import time
from typing import Optional, Set, Dict, Any
from src.tools.keyword.character_manager import get_character_manager
from src.llm.generation_error import (
    GenerationFailure,
    classify_generation_error,
    empty_response_failure,
)
from src.llm.generation_cancellation import (
    GenerationInterrupted,
    get_current_generation_cancellation,
    raise_if_generation_interrupted,
)
from src.llm.turn_stream_events import (
    bind_stream_callback_loop,
    make_sync_stream_emitter,
)
from src.services.agent_team_service import (
    apply_continuation_delta,
    continuation_state_for_prompt,
    get_current_continuation_state,
)


class ResponseHandler:
    """Handles response generation and task management"""
    
    def __init__(self, llm_client, tts_manager=None, player=None, character_name: str = "Assistant", voice_chat_mode=None):
        """Initialize response handler
        
        Args:
            llm_client: LLM client for response generation
            tts_manager: TTS manager for speech synthesis (optional)
            player: Audio player for speech playback (optional)
            character_name: Name of the character
            voice_chat_mode: Reference to VoiceChatMode for engine switching (optional)
        """
        self.llm_client = llm_client
        self.tts_manager = tts_manager
        self.player = player
        self.character_name = character_name
        self.voice_chat_mode = voice_chat_mode
        
        # Task management
        self.is_generating = False
        self.active_tasks: Set[asyncio.Task] = set()
        self.task_counter = 0

        # 直近の応答生成失敗の分類結果（呼び出し元がユーザー向け文言と技術詳細の
        # 両方を回収できるように公開する）。成功時は None。
        # 例外failureは classify_generation_error による分類結果、
        # 純粋な空応答は kind=EMPTY_RESPONSE の分類結果で区別する。
        self.last_generation_failure: Optional[GenerationFailure] = None
        
        # Resource locks for parallel processing
        if tts_manager and player:
            self.resource_locks = {
                'tts': asyncio.Lock(),     # TTS synthesis lock
                'playback': asyncio.Lock() # Audio playback lock
            }
            # Windows-specific timeout settings for resource locks
            self.lock_timeout = 5.0  # 5 second timeout for Windows
        else:
            self.resource_locks = {}
            self.lock_timeout = 5.0
        
        # Status callback for GUI updates
        self.status_callback: Optional[callable] = None
        
        # Register character switch callback
        self._register_character_switch_callback()
        
    def _register_character_switch_callback(self):
        """Register callback for character switching"""
        manager = get_character_manager()
        manager.register_callback(self._on_character_switch)
        
    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """Handle character switch event
        
        Args:
            character_name: New character name
            yaml_filename: YAML filename (without extension)
        """
        print(f"[ResponseHandler] キャラクター切り替え: {self.character_name} -> {character_name}")
        self.character_name = character_name
        
        # Update LLM client with new character context if possible
        if hasattr(self.llm_client, 'update_character'):
            try:
                self.llm_client.update_character(yaml_filename)
            except Exception as e:
                print(f"[ResponseHandler] LLMクライアントのキャラクター更新エラー: {e}")
        
    def set_status_callback(self, callback: callable):
        """Set callback for status updates
        
        Args:
            callback: Function to call with status updates
        """
        self.status_callback = callback
        
    def _update_status(self, status: str, color: str = "blue"):
        """Update status via callback"""
        if self.status_callback:
            try:
                self.status_callback(status, color)
            except Exception as e:
                print(f"[ステータス更新] エラー: {e}")
    
    async def handle_new_input(self, text: str, input_type: str = "normal", image_data: dict = None) -> Optional[str]:
        """Handle new user input with priority-based task management
        
        Args:
            text: User input text
            input_type: Type of input ('normal', 'interrupt', 'chat', 'web')
            image_data: Optional image data for multimodal input {data: base64, mimeType: str, name: str}
            
        Returns:
            Generated response or None if cancelled
        """
        # Cancel all existing tasks when new speech is detected for voice input
        if input_type in ['normal', 'interrupt']:
            await self._cancel_all_active_tasks()
        
        # Create new task with unique ID
        task_id = self._generate_task_id()
        print(f"[タスク管理] 新タスク開始: {task_id} - '{text}' (タイプ: {input_type})")
        
        # For chat/web input, use resource locks to prevent conflicts
        if input_type in ['chat', 'web']:
            return await self._generate_and_speak_response(task_id, text, input_type, image_data=image_data)
        
        # For voice input, generate response first, then handle TTS/playback in background
        response = await self._generate_response_only(task_id, text, input_type, image_data=image_data)
        
        # Create background task for TTS and playback (if available)
        if response and (self.tts_manager and self.player):
            task = asyncio.create_task(
                self._speak_response_background(task_id, response)
            )
            self.active_tasks.add(task)
        
        return response
    
    async def _generate_and_speak_response_with_id(self, task_id: str, text: str, input_type: str):
        """Generate and speak response with task ID tracking"""
        try:
            print(f"[{task_id}] 応答生成開始")
            self._update_status("LLM処理中", "red")
            
            await self._generate_and_speak_response(task_id, text, input_type)
        except asyncio.CancelledError:
            print(f"[{task_id}] タスクがキャンセルされました")
            self._update_status("キャンセル済み", "gray")
            raise
        except Exception as e:
            print(f"[{task_id}] エラー: {type(e).__name__}: {e}")
        finally:
            # Remove from active tasks when done
            task = asyncio.current_task()
            if task in self.active_tasks:
                self.active_tasks.remove(task)
            print(f"[{task_id}] タスク完了")
            self._update_status("待機中", "blue")
    
    async def _generate_and_speak_response(self, task_id: str, text: str, input_type: str, image_data: dict = None) -> Optional[str]:
        """Generate and speak response
        
        Args:
            task_id: Task identifier
            text: Input text
            input_type: Type of input
            image_data: Optional image data for multimodal input
            
        Returns:
            Generated response or None if cancelled
        """
        self.is_generating = True
        current_task = asyncio.current_task()
        
        try:
            # Check if task was cancelled before starting
            if current_task and current_task.cancelled():
                print(f"[{task_id}] タスク開始前にキャンセル検出")
                return None
                
            print(f"[{task_id}] 応答生成中...")
            
            # Generate response with task-specific cancellation check
            response = await self._generate_with_interrupt_check(text, task_id, current_task, image_data=image_data)
            
            # Check cancellation after generation
            if current_task and current_task.cancelled():
                print(f"[{task_id}] 応答生成後にキャンセル検出")
                return None
                
            if response is None:
                print(f"[{task_id}] 応答生成を中断しました")
                return None
                
            # Add response to hallucination filter for echo detection (if available)
            if hasattr(self.llm_client, 'recognizer') and self.llm_client.recognizer:
                self.llm_client.recognizer.add_assistant_output(response)
            self._schedule_deferred_project_fact_reflection(text, response)

            # For web input, proceed with TTS and playback using resource locks
            # For chat input, return response without TTS
            if input_type == 'chat':
                return response
            elif input_type == 'web' and self.tts_manager and self.player:
                await self._synthesize_and_play(task_id, response, current_task)
            elif input_type not in ['chat', 'web'] and self.tts_manager and self.player:
                # For voice input, proceed with TTS and playback
                await self._synthesize_and_play(task_id, response, current_task)
            
            return response
            
        except asyncio.CancelledError:
            print(f"[{task_id}] タスクがキャンセルされました")
            raise
        except Exception as e:
            print(f"\n[{task_id}] 応答処理エラー: {type(e).__name__}: {e}")
            return None
        finally:
            self.is_generating = False
            
    async def _generate_response_only(
        self,
        task_id: str,
        text: str,
        input_type: str,
        image_data: dict = None,
        stream_callback=None,
        steering_callback=None,
        evidence_user_input: Optional[str] = None,
    ) -> Optional[str]:
        """Generate response without TTS/playback.

        ``text`` may contain attachment or command context prepared by the
        application.  Dreaming Memory must only use the user's persisted raw
        utterance as evidence, so callers can pass it separately.
        """
        streamed_final_response: Optional[str] = None
        max_interrupts = 8
        cancellation_handle = get_current_generation_cancellation()
        generation_run_id = (
            str(cancellation_handle.run_id or "").strip()
            if cancellation_handle is not None
            else ""
        )
        # Tool execution evidence belongs to the logical generation run, not
        # to the final assistant text.  A provider can successfully execute a
        # mutation and then return an empty final response; keep that evidence
        # available for AgentRunEventEmitter.fail() instead of treating the
        # empty text as permission to retry/clear the run.
        tool_activity_observed = False

        def _normalize_generation_failure_marker(value: Any) -> Any:
            """Accept the optional provider-side empty-response marker.

            Native providers expose this additively because the public client
            API still returns a string.  Keep the handler tolerant of either a
            GenerationFailure instance or a small dict/object carrying the
            same fields; malformed diagnostics must never break generation.
            """

            if isinstance(value, GenerationFailure):
                return value
            if isinstance(value, dict):
                kind = value.get("kind")
                technical_detail = value.get("technical_detail")
                user_message = value.get("user_message")
                if kind and technical_detail and user_message:
                    try:
                        return GenerationFailure(
                            kind=kind,
                            technical_detail=str(technical_detail),
                            user_message=str(user_message),
                        )
                    except (TypeError, ValueError):
                        return None
            return value if isinstance(value, GenerationFailure) else None

        def _client_generation_failure_marker() -> Any:
            for attribute in (
                "generation_failure",
                "last_generation_failure",
                "_last_generation_failure",
                "_generation_failure",
            ):
                marker = getattr(self.llm_client, attribute, None)
                if marker is not None:
                    return _normalize_generation_failure_marker(marker)
            getter = getattr(self.llm_client, "get_generation_failure", None)
            if callable(getter):
                try:
                    return _normalize_generation_failure_marker(getter())
                except Exception:
                    return None
            return None

        def _consume_client_generation_failure_marker() -> Any:
            marker = _client_generation_failure_marker()
            if marker is None:
                return None
            consumer = getattr(
                self.llm_client,
                "consume_generation_failure",
                None,
            )
            if callable(consumer):
                try:
                    consumed = consumer()
                    if consumed is not None:
                        marker = _normalize_generation_failure_marker(consumed)
                except Exception:
                    pass
            for attribute in (
                "generation_failure",
                "last_generation_failure",
                "_last_generation_failure",
                "_generation_failure",
            ):
                try:
                    if getattr(self.llm_client, attribute, None) is not None:
                        setattr(self.llm_client, attribute, None)
                except Exception:
                    continue
            return marker

        async def _generation_tool_calls() -> list[Any]:
            """Read only this run's completed tool ledger when available."""

            if generation_run_id:
                getter = getattr(
                    self.llm_client,
                    "peek_completed_agent_run_state",
                    None,
                )
                if callable(getter):
                    try:
                        state = getter(generation_run_id)
                        if inspect.isawaitable(state):
                            state = await state
                        if isinstance(state, dict):
                            calls = state.get("tool_calls")
                            if isinstance(calls, (list, tuple)) and calls:
                                return list(calls)
                    except Exception:
                        # The fallback fields below are compatibility-only;
                        # a diagnostics getter must not fail the user turn.
                        pass

            for attribute in ("_last_tool_calls", "_last_turn_tool_records"):
                calls = getattr(self.llm_client, attribute, None)
                if not isinstance(calls, (list, tuple)) or not calls:
                    continue
                run_marker = getattr(
                    self.llm_client,
                    f"{attribute}_run_id",
                    None,
                )
                if (
                    generation_run_id
                    and run_marker
                    and str(run_marker).strip() != generation_run_id
                ):
                    continue
                return list(calls)
            return []

        async def _generation_has_tool_activity() -> bool:
            return tool_activity_observed or bool(await _generation_tool_calls())

        async def discard_generation_run_if_safe() -> None:
            # Do not erase run-keyed audit evidence before AgentRun failure
            # persistence has a chance to peek and acknowledge it.  Runs with
            # no tools retain the historical cleanup behavior.
            if await _generation_has_tool_activity():
                print(
                    f"[{task_id}] ツール監査を保持したままgeneration runを終了します"
                )
                return
            await notify_generation_lifecycle("discard_generation_run")

        async def notify_generation_lifecycle(method_name: str) -> Any:
            if not generation_run_id and method_name != "discard_generation_run":
                return None
            method = getattr(self.llm_client, method_name, None)
            if not callable(method):
                return None
            try:
                result = method(generation_run_id or None)
                if inspect.isawaitable(result):
                    return await result
                return result
            except Exception as exc:
                print(
                    f"[{task_id}] generation lifecycle update failed "
                    f"({method_name}): {exc}"
                )
                return None

        async def capture_stream_callback(event_type: str, data: Any) -> None:
            nonlocal streamed_final_response
            nonlocal tool_activity_observed
            # A sync provider can only observe a control request at its next
            # emitted event.  Check both sides of the callback so an interrupt
            # arriving while the UI callback is being awaited also aborts the
            # current attempt before its final response is accepted.
            raise_if_generation_interrupted()
            if event_type == "stream_end" and isinstance(data, dict):
                content = str(data.get("content") or "").strip()
                if content:
                    streamed_final_response = content
            if event_type in {
                "tool_start",
                "tool_end",
                "stream.tool_start",
                "stream.tool_end",
            }:
                tool_activity_observed = True
            marker = _client_generation_failure_marker()
            if marker is not None:
                self.last_generation_failure = marker
            if stream_callback is not None:
                callback_result = stream_callback(event_type, data)
                if inspect.isawaitable(callback_result):
                    await callback_result
            raise_if_generation_interrupted()

        async def run_generation_attempt(generation_text: str) -> Optional[str]:
            """LLM生成を1回実行し、応答文字列（空なら None）を返す。

            戻り値が空でも配信済みの最終ストリーム応答があればそれを回収する。
            例外はここでは握り潰さず呼び出し側へ送出する。
            """
            nonlocal streamed_final_response
            streamed_final_response = None

            current_task = asyncio.current_task()
            if current_task and current_task.cancelled():
                print(f"[{task_id}] 応答生成前にキャンセル検出")
                return None

            print(f"[{task_id}] 応答生成中...")

            if hasattr(self.llm_client, 'generate_response_async'):
                async_generate = self.llm_client.generate_response_async
                call_kwargs = {}
                try:
                    signature = inspect.signature(async_generate)
                    parameters = signature.parameters
                    accepts_kwargs = any(
                        param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in parameters.values()
                    )
                    if accepts_kwargs or "image_data" in parameters:
                        call_kwargs["image_data"] = image_data
                    if accepts_kwargs or "stream_callback" in parameters:
                        call_kwargs["stream_callback"] = (
                            capture_stream_callback if stream_callback is not None else None
                        )
                    if accepts_kwargs or "steering_callback" in parameters:
                        call_kwargs["steering_callback"] = steering_callback
                except (TypeError, ValueError):
                    call_kwargs = {
                        "image_data": image_data,
                        "stream_callback": (
                            capture_stream_callback if stream_callback is not None else None
                        ),
                    }
                attempt_response = await async_generate(generation_text, **call_kwargs)
            else:
                sync_generate = self.llm_client.generate_response
                call_kwargs = {}
                bound_stream_callback = (
                    bind_stream_callback_loop(capture_stream_callback)
                    if stream_callback is not None
                    else None
                )
                try:
                    signature = inspect.signature(sync_generate)
                    parameters = signature.parameters
                    accepts_kwargs = any(
                        param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in parameters.values()
                    )
                    if accepts_kwargs or "image_data" in parameters:
                        call_kwargs["image_data"] = image_data
                    if (
                        bound_stream_callback is not None
                        and (accepts_kwargs or "stream_callback" in parameters)
                    ):
                        call_kwargs["stream_callback"] = bound_stream_callback
                    if accepts_kwargs or "steering_callback" in parameters:
                        call_kwargs["steering_callback"] = steering_callback
                    if (
                        bound_stream_callback is not None
                        and getattr(
                            self.llm_client,
                            "supports_interruptible_steering",
                            False,
                        )
                        and (accepts_kwargs or "stream" in parameters)
                    ):
                        call_kwargs["stream"] = True
                except (TypeError, ValueError):
                    call_kwargs = {"image_data": image_data}
                def invoke_sync_generation() -> Any:
                    result = sync_generate(generation_text, **call_kwargs)
                    if (
                        bound_stream_callback is None
                        or result is None
                        or isinstance(result, str)
                        or not hasattr(result, "__iter__")
                    ):
                        return result

                    # Consume a synchronous provider generator in the worker so
                    # a blocking next() cannot stall the application loop.
                    emitter = make_sync_stream_emitter(bound_stream_callback)
                    chunks: list[str] = []
                    if emitter is not None:
                        emitter(
                            "stream_start",
                            {"message": "応答を生成しています"},
                        )
                    for chunk in result:
                        raise_if_generation_interrupted()
                        content = str(chunk or "")
                        if not content:
                            continue
                        chunks.append(content)
                        if emitter is not None:
                            emitter("stream_token", {"content": content})
                    full_response = "".join(chunks)
                    if emitter is not None:
                        emitter("stream_end", {"content": full_response})
                    return full_response

                attempt_response = await asyncio.to_thread(invoke_sync_generation)

            marker = _consume_client_generation_failure_marker()
            if marker is not None:
                self.last_generation_failure = marker

            # Non-streaming providers cannot be stopped between tokens, but
            # they still must not publish an old answer after an interrupt was
            # received while the request was in flight.
            raise_if_generation_interrupted(final=True)

            if not attempt_response and streamed_final_response:
                print(
                    f"[{task_id}] LLM戻り値が空のため、配信済みの最終応答を回収しました"
                )
                attempt_response = streamed_final_response

            return attempt_response

        async def run_generation_with_interrupts(
            initial_text: str,
        ) -> Optional[str]:
            """Restart the same turn when Ctrl+Enter interrupts the attempt."""
            nonlocal streamed_final_response
            generation_text = initial_text
            interrupt_count = 0
            while True:
                try:
                    return await run_generation_attempt(generation_text)
                except GenerationInterrupted as interrupt:
                    await notify_generation_lifecycle("prepare_generation_retry")
                    instructions = await interrupt.resolve_instructions()
                    if not instructions:
                        if interrupt.reservations:
                            # Persistence failed after the active attempt was
                            # reserved. Retry the original attempt without
                            # applying an unpersisted steering instruction.
                            streamed_final_response = None
                            continue
                        raise
                    continuation_state = getattr(
                        interrupt,
                        "continuation_state",
                        None,
                    ) or get_current_continuation_state()
                    if continuation_state is not None:
                        # The steer message is a delta over the original
                        # goal.  Apply cancellation semantics before deciding
                        # whether a provider retry is safe.
                        apply_continuation_delta(
                            continuation_state,
                            "\n".join(instructions),
                        )
                        if continuation_state.explicit_cancelled:
                            continuation_state.mutation_state = "cancelled"
                            return "操作を中止しました。未完了のDocs変更は再開していません。"
                    interrupt_count += 1
                    if interrupt_count > max_interrupts:
                        raise RuntimeError(
                            "追加指示の割り込み回数が上限に達しました"
                        ) from interrupt
                    instruction_block = "追加指示:\n" + "\n".join(
                        f"- {instruction}" for instruction in instructions
                    )
                    continuation_snapshot = continuation_state_for_prompt(
                        continuation_state
                    )
                    if continuation_snapshot:
                        instruction_block += (
                            "\n\n継続状態（システム管理; 推測で変更しない）:\n"
                            + continuation_snapshot
                            + "\n解決済みIDとpending_destination_parent_idを保持し、"
                            "追加指示は元の目標へのdeltaとして扱うこと。"
                            "『引っ越し先』等の補足語を検索語にせず、"
                            "書込み前に同じproject/親/権限を再検証すること。"
                        )
                    if getattr(
                        self.llm_client,
                        "steering_retry_uses_existing_history",
                        False,
                    ):
                        generation_text = instruction_block
                    else:
                        generation_text = (
                            f"{generation_text}\n\n{instruction_block}"
                        )
                    streamed_final_response = None
                    if stream_callback is not None:
                        restart_callback_result = stream_callback(
                            "stream_start",
                            {
                                "status": "steering_applied",
                                "message": "追加指示を反映します",
                            },
                        )
                        if inspect.isawaitable(restart_callback_result):
                            await restart_callback_result
                    print(
                        f"[{task_id}] 追加指示を反映して応答生成を再開します "
                        f"(interrupt={interrupt_count})"
                    )

        try:
            self.is_generating = True
            self.last_generation_failure = None

            # 1回目の生成。例外は捕捉して分類し、ユーザー向け文言と技術詳細の
            # 両方を保持する（文字列へ潰さない）。
            try:
                response = await run_generation_with_interrupts(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failure = classify_generation_error(e)
                print(
                    f"[{task_id}] 応答生成エラー[{failure.kind}]: "
                    f"{failure.technical_detail}"
                )
                import traceback
                traceback.print_exc()
                self.last_generation_failure = failure
                if streamed_final_response:
                    print(f"[{task_id}] 配信済みの最終応答を例外後に回収しました")
                    response = streamed_final_response
                    self.last_generation_failure = None
                else:
                    response = None

            # プロジェクトコンテキスト起因の失敗フォールバック:
            # include_project_context が有効な状態で失敗した場合、コンテキストなしで1回だけ再試行する。
            if not response and self._project_context_enabled() and not (
                await _generation_has_tool_activity()
            ):
                print(
                    f"[{task_id}] プロジェクトコンテキスト有効時に生成失敗。"
                    f"コンテキストなしで再試行します"
                )
                from ..services.turn_context import (
                    override_turn_context,
                    reset_turn_context,
                )

                turn_context_token = override_turn_context(
                    include_project_context=False
                )
                try:
                    retry_response = await run_generation_with_interrupts(text)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failure = classify_generation_error(e)
                    print(
                        f"[{task_id}] 再試行の応答生成エラー[{failure.kind}]: "
                        f"{failure.technical_detail}"
                    )
                    import traceback
                    traceback.print_exc()
                    self.last_generation_failure = failure
                    retry_response = streamed_final_response or None
                finally:
                    reset_turn_context(turn_context_token)
                if retry_response:
                    print(
                        f"[{task_id}] プロジェクトコンテキストなしで再試行して成功"
                    )
                    self.last_generation_failure = None
                    response = retry_response

            if not response:
                # 例外が記録されていなければ純粋な空応答として区別する。
                if self.last_generation_failure is None:
                    self.last_generation_failure = empty_response_failure()
                if (
                    not generation_run_id
                    or self.last_generation_failure.is_empty_response
                ):
                    await discard_generation_run_if_safe()
                print(f"[{task_id}] 応答生成失敗")
                return None

            await notify_generation_lifecycle("accept_generation_run")
            if hasattr(self.llm_client, 'recognizer') and self.llm_client.recognizer:
                self.llm_client.recognizer.add_assistant_output(response)

            dreaming_context = self._capture_dreaming_memory_context()
            dreaming_user_input = (
                evidence_user_input if evidence_user_input is not None else text
            )
            # Persist the extraction request before returning the answer. The
            # worker may be cancelled during shutdown, but the pending row can
            # then be retried on the next active turn instead of disappearing.
            memory_job = await self._enqueue_dreaming_memory(
                dreaming_user_input,
                response,
                user_id=dreaming_context["user_id"],
                session_id=dreaming_context["session_id"],
                project_id=dreaming_context["project_id"],
                message_id=dreaming_context["message_id"],
            )
            processor_overridden = (
                getattr(self._process_dreaming_memory, "__func__", None)
                is not ResponseHandler._process_dreaming_memory
            )
            memory_coroutine = None
            if processor_overridden:
                memory_coroutine = self._process_dreaming_memory(
                    dreaming_user_input,
                    response,
                    user_id=dreaming_context["user_id"],
                    session_id=dreaming_context["session_id"],
                    project_id=dreaming_context["project_id"],
                    llm_client=self.llm_client,
                )
            elif memory_job and dreaming_context["user_id"]:
                memory_coroutine = self._process_dreaming_memory_job(
                    memory_job["id"],
                    user_id=str(dreaming_context["user_id"]),
                    llm_client=self.llm_client,
                )
            if memory_coroutine is not None:
                memory_task = asyncio.create_task(memory_coroutine)
                self.active_tasks.add(memory_task)
                memory_task.add_done_callback(self.active_tasks.discard)
            self._schedule_deferred_project_fact_reflection(text, response)

            return response

        except asyncio.CancelledError:
            await discard_generation_run_if_safe()
            print(f"[{task_id}] 応答生成がキャンセルされました")
            raise
        finally:
            self.is_generating = False

    def _project_context_enabled(self) -> bool:
        """現在の LLM クライアントでプロジェクトコンテキスト注入が有効かどうか。"""
        try:
            from ..services.project_context import project_context_enabled_for_client

            return project_context_enabled_for_client(self.llm_client)
        except Exception:
            # Keep the response path resilient for lightweight/test clients
            # that do not expose the provider context helpers.
            return bool(getattr(self.llm_client, "current_include_project_context", True))

    def _set_project_context_enabled(self, enabled: bool) -> None:
        """LLM クライアントのプロジェクトコンテキスト注入フラグを切り替える。"""
        if self.llm_client is not None and hasattr(
            self.llm_client, "current_include_project_context"
        ):
            self.llm_client.current_include_project_context = enabled

    def _schedule_deferred_project_fact_reflection(
        self,
        user_input: str,
        assistant_response: str,
    ) -> None:
        """No-op: project information is updated in-turn through root direct tools."""
        return

    def _capture_dreaming_memory_context(self) -> Dict[str, Optional[str]]:
        """Capture mutable LLM session context before background tasks run."""
        user_id: Optional[str] = None
        getter = getattr(self.llm_client, "_get_session_user_id", None)
        if callable(getter):
            try:
                resolved_user_id = getter()
                if resolved_user_id:
                    user_id = str(resolved_user_id)
            except Exception as e:
                print(f"[DreamingMemory] user_id取得エラー（抽出を保留）: {e}")

        session_id = getattr(self.llm_client, "current_session_id", None)
        project_id = getattr(self.llm_client, "current_project_id", None)
        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
            if not self._project_context_enabled():
                # Dreaming-memory writes are scoped data, not runtime auth.
                # Explicit OFF (including provider-local direct calls without
                # a bound TurnContext) must not persist the selected Project
                # as an implicit memory target in a background task.
                project_id = None
            message_id = turn.message_id or turn.client_message_id
        except Exception:
            message_id = None
        return {
            "user_id": user_id,
            "session_id": str(session_id) if session_id else None,
            "project_id": str(project_id) if project_id else None,
            "message_id": str(message_id) if message_id else None,
        }

    def _capture_project_context_for_background(self) -> Optional[Dict[str, Any]]:
        resolver = getattr(self.llm_client, "_resolve_project_context_sync", None)
        if callable(resolver):
            try:
                context = resolver()
                if isinstance(context, dict) and context.get("id"):
                    return dict(context)
            except Exception as e:
                print(f"[ProjectInformationReflection] project context 解決エラー（継続）: {e}")

        project_id = getattr(self.llm_client, "current_project_id", None)
        if project_id:
            return {"id": str(project_id)}
        return None

    @staticmethod
    def _log_background_task_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[ProjectInformationReflection] バックグラウンドエラー（無視）: {e}")

    async def _process_deferred_project_fact_reflection(
        self,
        user_input: str,
        assistant_response: str,
        config: Any,
        project_context: Optional[Dict[str, Any]],
    ) -> None:
        return

    async def _speak_response_background(self, task_id: str, response: str):
        """Handle TTS and playback in background"""
        try:
            current_task = asyncio.current_task()
            
            if current_task and current_task.cancelled():
                print(f"[{task_id}] 音声合成開始前にキャンセル検出")
                return
            
            print(f"[{task_id}] 音声合成開始...")
            self._update_status("音声合成中", "orange")
            
            # Use TTS resource lock to prevent conflicts
            async with self.resource_locks['tts']:
                if current_task and current_task.cancelled():
                    print(f"[{task_id}] TTS開始前にキャンセル")
                    return
                
                # Add timeout wrapper for TTS synthesis
                try:
                    # Use voice_chat_mode's _synthesize_with_engine_check if available
                    if self.voice_chat_mode and hasattr(self.voice_chat_mode, '_synthesize_with_engine_check'):
                        synthesis_task = asyncio.create_task(
                            self.voice_chat_mode._synthesize_with_engine_check(response)
                        )
                    else:
                        synthesis_task = asyncio.create_task(
                            self.tts_manager.synthesize(
                                response,
                                character_name=self.character_name
                            )
                        )
                    
                    audio_data = await asyncio.wait_for(synthesis_task, timeout=30.0)
                    
                except asyncio.TimeoutError:
                    print(f"[{task_id}] TTS合成タイムアウト")
                    synthesis_task.cancel()
                    return
            
            if audio_data and not (current_task and current_task.cancelled()):
                print(f"[{task_id}] 音声再生中...")
                self._update_status("再生中", "green")
                
                # Use playback resource lock with timeout
                try:
                    # Acquire lock with timeout
                    lock_task = asyncio.create_task(self.resource_locks['playback'].__aenter__())
                    await asyncio.wait_for(lock_task, timeout=self.lock_timeout)
                    
                    try:
                        if current_task and current_task.cancelled():
                            print(f"[{task_id}] 再生開始前にキャンセル")
                            return
                            
                        # Play audio with error handling and timeout
                        try:
                            playback_task = asyncio.get_event_loop().run_in_executor(
                                None, self.player.play, audio_data
                            )
                            await asyncio.wait_for(playback_task, timeout=30.0)
                        except asyncio.TimeoutError:
                            print(f"[{task_id}] 音声再生タイムアウト")
                            # Force stop playback
                            try:
                                self.player.stop()
                            except:
                                pass
                        except Exception as play_error:
                            # Handle audio playback errors gracefully
                            error_msg = str(play_error)
                            if any(err in error_msg for err in ['Unanticipated host error', 'ALSA', 'poll_descriptors']):
                                print(f"[{task_id}] 音声システムエラー（無視）: {type(play_error).__name__}")
                            else:
                                print(f"[{task_id}] 音声再生エラー: {play_error}")
                    finally:
                        # Release lock
                        await self.resource_locks['playback'].__aexit__(None, None, None)
                        
                except asyncio.TimeoutError:
                    print(f"[{task_id}] 再生ロック取得タイムアウト")
                    return
                    
                print(f"[{task_id}] 応答完了")
                self._update_status("完了", "blue")
            else:
                print(f"[{task_id}] 音声データなし、またはキャンセル済み")
        
        except asyncio.CancelledError:
            print(f"[{task_id}] 音声処理がキャンセルされました")
            raise
        except Exception as e:
            print(f"[{task_id}] 音声処理エラー: {type(e).__name__}: {e}")
        finally:
            # Remove task from active tasks
            if current_task in self.active_tasks:
                self.active_tasks.discard(current_task)
    
    async def _synthesize_and_play(self, task_id: str, response: str, current_task: Optional[asyncio.Task]):
        """Synthesize and play response"""
        # Check if task cancelled before synthesis
        if current_task and current_task.cancelled():
            print(f"[{task_id}] 音声合成前にキャンセル検出")
            return
        
        # Synthesize speech with resource lock and timeout
        print(f"[{task_id}] 音声合成中...")
        self._update_status("音声合成中", "orange")
        
        self.is_generating = False  # Generation complete, now synthesizing
        
        # Use TTS resource lock to prevent conflicts
        async with self.resource_locks['tts']:
            if current_task and current_task.cancelled():
                print(f"[{task_id}] TTS開始前にキャンセル")
                return
        
            # Add timeout wrapper for TTS synthesis
            try:
                # Use voice_chat_mode's _synthesize_with_engine_check if available
                if self.voice_chat_mode and hasattr(self.voice_chat_mode, '_synthesize_with_engine_check'):
                    synthesis_task = asyncio.create_task(
                        self.voice_chat_mode._synthesize_with_engine_check(response)
                    )
                else:
                    synthesis_task = asyncio.create_task(
                        self.tts_manager.synthesize(
                            response,
                            character_name=self.character_name
                        )
                    )
                
                # Wait for synthesis with timeout and cancellation checking
                synthesis_timeout = self._get_synthesis_timeout()
                start_time = time.time()
                
                while not synthesis_task.done():
                    if current_task and current_task.cancelled():
                        print(f"[{task_id}] 音声合成を中断します")
                        synthesis_task.cancel()
                        return
                    
                    if time.time() - start_time > synthesis_timeout:
                        print(f"[{task_id}] 音声合成がタイムアウトしました")
                        synthesis_task.cancel()
                        return
                        
                    await asyncio.sleep(0.1)
                
                audio_data = await synthesis_task
            
            except asyncio.CancelledError:
                print(f"[{task_id}] 音声合成タスクがキャンセルされました")
                return
            except Exception as synthesis_error:
                print(f"[{task_id}] 音声合成エラー: {type(synthesis_error).__name__}: {synthesis_error}")
                import traceback
                traceback.print_exc()
                return
        
        if audio_data and (not current_task or not current_task.cancelled()):
            await self._play_audio(task_id, audio_data, current_task)
        else:
            if not audio_data:
                print(f"[{task_id}] 音声合成結果が空です - TTSエンジンに問題がある可能性があります")

    def _get_synthesis_timeout(self) -> float:
        default_timeout = 30.0
        config = getattr(self.voice_chat_mode, "config", None)
        tts_manager = getattr(self.voice_chat_mode, "tts_manager", None)
        engine_name = getattr(tts_manager, "current_engine", None)

        if config is not None and hasattr(config, "get"):
            default_timeout = float(config.get("tts.synthesis_timeout", default_timeout) or default_timeout)
            if engine_name:
                engine_settings = config.get(f"tts_settings.{engine_name}", {}) or {}
                if isinstance(engine_settings, dict):
                    engine_timeout = (
                        engine_settings.get("synthesis_timeout")
                        or engine_settings.get("timeout")
                    )
                    if engine_timeout:
                        return float(engine_timeout)

        return default_timeout

    async def _play_audio(self, task_id: str, audio_data: bytes, current_task: Optional[asyncio.Task]):
        """Play synthesized audio"""
        # Use playback resource lock to ensure only one audio plays at a time
        async with self.resource_locks['playback']:
            if current_task and current_task.cancelled():
                print(f"[{task_id}] 再生開始前にキャンセル")
                return
                
            print(f"[{task_id}] 再生中... (話しかけると割り込めます)")
            self._update_status("再生中", "green")
            
            # Play with interrupt support
            try:
                # Use non-blocking playback for better interrupt responsiveness
                self.player.play(audio_data, blocking=False)
            
                # Wait for playback or interrupt with proper thread synchronization
                playback_timeout = 60.0  # Maximum playback time
                playback_start = time.time()
                
                # Use proper thread waiting with task cancellation check
                while (self.player.play_thread and 
                       self.player.play_thread.is_alive() and 
                       (not current_task or not current_task.cancelled())):
                    
                    # Check for playback timeout
                    if time.time() - playback_start > playback_timeout:
                        print(f"\n[{task_id}] 再生がタイムアウトしました")
                        self.player.stop()
                        break
                        
                    await asyncio.sleep(0.01)  # Check every 10ms for faster response
                
                # Handle cancellation case
                if current_task and current_task.cancelled():
                    print(f"\n[{task_id}] 再生を即座に中断します")
                    self.player.stop()
                elif (not self.player.play_thread or 
                      not self.player.play_thread.is_alive()):
                    print(f"\n[{task_id}] 再生完了")
                
            except Exception as playback_error:
                print(f"\n[{task_id}] 再生エラー: {playback_error}")
                # Ensure player is stopped even if error occurs
                try:
                    self.player.stop()
                except:
                    pass
    
    async def _generate_with_interrupt_check(self, text: str, task_id: str = "unknown", parent_task = None, image_data: dict = None) -> Optional[str]:
        """Generate response with task-specific cancellation checking"""
        # Check if parent task was cancelled before starting
        if parent_task and parent_task.cancelled():
            print(f"[{task_id}] 親タスクキャンセル済み - 応答生成をスキップ")
            return None
            
        try:
            # Direct async call instead of executor to allow proper cancellation
            if hasattr(self.llm_client, 'generate_response_async'):
                # Use async version if available
                response = await self.llm_client.generate_response_async(text, image_data=image_data)
            else:
                # Fallback: create a task that can be cancelled
                generation_task = asyncio.create_task(
                    asyncio.to_thread(lambda: self.llm_client.generate_response(text, stream=False, image_data=image_data))
                )
                
                # Monitor for parent task cancellation during generation
                while not generation_task.done():
                    if parent_task and parent_task.cancelled():
                        print(f"[{task_id}] 応答生成中に親タスクキャンセル検出")
                        generation_task.cancel()
                        try:
                            await generation_task
                        except asyncio.CancelledError:
                            pass
                        return None
                    await asyncio.sleep(0.05)  # Check every 50ms
                
                response = await generation_task
                
            # Final cancellation check
            if parent_task and parent_task.cancelled():
                print(f"[{task_id}] 応答生成完了後に親タスクキャンセル検出")
                return None
                
            return response
            
        except asyncio.CancelledError:
            print(f"[{task_id}] 応答生成タスクがキャンセルされました")
            return None
        except Exception as e:
            print(f"[{task_id}] 応答生成エラー: {e}")
            return None
    
    def _generate_task_id(self) -> str:
        """Generate unique task ID"""
        self.task_counter += 1
        return f"task_{self.task_counter}"
        
    async def _cancel_all_active_tasks(self):
        """Cancel all currently active response tasks"""
        if not self.active_tasks:
            return
            
        print(f"[タスク管理] {len(self.active_tasks)}個のアクティブタスクをキャンセル中")
        
        # Stop current playback immediately with error handling
        if self.player and hasattr(self.player, 'stop'):
            try:
                self.player.stop()
            except Exception as stop_error:
                # Handle audio stop errors gracefully
                error_msg = str(stop_error)
                if any(err in error_msg for err in ['Unanticipated host error', 'ALSA', 'poll_descriptors']):
                    print(f"[タスク管理] 音声停止時システムエラー（無視）: {type(stop_error).__name__}")
                else:
                    print(f"[タスク管理] 音声停止エラー: {stop_error}")
            
        # Cancel all tasks
        cancelled_tasks = list(self.active_tasks)
        for task in cancelled_tasks:
            if not task.done():
                task.cancel()
                
        # Wait for tasks to complete cancellation with timeout
        if cancelled_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cancelled_tasks, return_exceptions=True),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                print("[警告] 一部のタスクのキャンセルがタイムアウトしました")
                
        # Clear task set
        self.active_tasks.clear()
        
        # Reset flags
        self.is_generating = False
        
        print("[タスク管理] 全タスクのキャンセル完了")
    
    async def _enqueue_dreaming_memory(
        self,
        user_input: str,
        assistant_response: str,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist one settings-aware extraction job before background work."""
        try:
            from ..services.scoped_memory_job_service import enqueue_scoped_memory_job

            context = self._capture_dreaming_memory_context()
            user_id = user_id or context["user_id"]
            session_id = session_id or context["session_id"]
            message_id = message_id or context["message_id"]
            if not self._project_context_enabled():
                project_id = None
            if not user_id or not session_id:
                return None
            return await enqueue_scoped_memory_job(
                user_id=str(user_id),
                session_id=str(session_id),
                project_id=project_id,
                user_input=user_input,
                assistant_response=assistant_response,
                message_id=message_id,
                privacy_config=getattr(self.llm_client, "config", None),
                session_context=getattr(self.llm_client, "_privacy_session_context", None),
                project_metadata=getattr(self.llm_client, "_privacy_project_metadata", None),
            )
        except Exception as e:
            print(f"[DreamingMemory] ジョブ登録エラー（無視）: {e}")
            return None

    async def _process_dreaming_memory_job(
        self,
        job_id: str,
        *,
        user_id: str,
        llm_client=None,
    ) -> None:
        """Process one durable job and opportunistically recover older jobs."""
        try:
            from ..services.scoped_memory_job_service import (
                process_pending_scoped_memory_jobs,
                process_scoped_memory_job,
            )

            active_client = llm_client or self.llm_client
            await process_scoped_memory_job(job_id, llm_client=active_client)
            await process_pending_scoped_memory_jobs(
                llm_client=active_client,
                user_id=str(user_id),
                limit=3,
            )
        except Exception as e:
            print(f"[DreamingMemory] 抽出エラー（無視）: {e}")

    async def _process_dreaming_memory(
        self,
        user_input: str,
        assistant_response: str,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        message_id: Optional[str] = None,
        llm_client=None,
    ):
        """Compatibility entry point: enqueue durably, then process.

        Args:
            user_input: ユーザーの入力テキスト
            assistant_response: アシスタントの応答テキスト
            user_id: 応答生成時点で確定したユーザーID
            session_id: 応答生成時点で確定した会話セッションID
            project_id: 応答生成時点で確定したプロジェクトID
        """
        context = self._capture_dreaming_memory_context()
        resolved_user_id = user_id or context["user_id"]
        if not self._project_context_enabled():
            project_id = None
        job = await self._enqueue_dreaming_memory(
            user_input,
            assistant_response,
            user_id=resolved_user_id,
            session_id=session_id or context["session_id"],
            project_id=project_id,
            message_id=message_id or context["message_id"],
        )
        if job and resolved_user_id:
            await self._process_dreaming_memory_job(
                job["id"], user_id=str(resolved_user_id), llm_client=llm_client
            )
