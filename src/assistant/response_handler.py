"""
Response handling for AoiTalk Voice Assistant Framework
"""

import asyncio
import time
from typing import Optional, Set, Dict, Any
from src.tools.keyword.character_manager import get_character_manager


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
                
            print(f"応答: {response}")
            
            # Add response to hallucination filter for echo detection (if available)
            if hasattr(self.llm_client, 'recognizer') and self.llm_client.recognizer:
                self.llm_client.recognizer.add_assistant_output(response)
            
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
            
    async def _generate_response_only(self, task_id: str, text: str, input_type: str, image_data: dict = None) -> Optional[str]:
        """Generate response without TTS/playback"""
        try:
            self.is_generating = True
            current_task = asyncio.current_task()
            
            # Check if task cancelled before generation
            if current_task and current_task.cancelled():
                print(f"[{task_id}] 応答生成前にキャンセル検出")
                return None
            
            print(f"[{task_id}] 応答生成中...")
            
            # Generate response using LLM
            response = self.llm_client.generate_response(text, image_data=image_data)
            
            if not response:
                print(f"[{task_id}] 応答生成失敗")
                return None
            
            print(f"応答: {response}")
            
            # Add response to hallucination filter for echo detection (if available)
            if hasattr(self.llm_client, 'recognizer') and self.llm_client.recognizer:
                self.llm_client.recognizer.add_assistant_output(response)
            
            # Process semantic memory (Mem0) - unified across all LLM providers
            await self._process_semantic_memory(text, response)
            
            return response
            
        except asyncio.CancelledError:
            print(f"[{task_id}] 応答生成がキャンセルされました")
            raise
        except Exception as e:
            print(f"[{task_id}] 応答生成エラー: {type(e).__name__}: {e}")
            return None
        finally:
            self.is_generating = False
    
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
                synthesis_timeout = 30.0  # 30 second timeout for TTS
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
    
    async def _process_semantic_memory(self, user_input: str, assistant_response: str):
        """Process conversation with Mem0 semantic memory - unified across all LLM providers
        
        This method ensures that semantic memory processing happens regardless of which
        LLM provider (OpenAI, Gemini, etc.) is being used, removing the dependency on
        provider-specific implementations.
        
        Args:
            user_input: The user's input text
            assistant_response: The assistant's response text
        """
        print(f"[ResponseHandler] _process_semantic_memory開始")
        print(f"[ResponseHandler] LLM client type: {type(self.llm_client).__name__}")
        print(f"[ResponseHandler] Has semantic_memory_manager: {hasattr(self.llm_client, 'semantic_memory_manager')}")
        
        try:
            # Check if semantic memory manager exists in LLM client
            if hasattr(self.llm_client, 'semantic_memory_manager') and self.llm_client.semantic_memory_manager:
                # Use the process_conversation method to extract and store semantic facts
                success = await self.llm_client.semantic_memory_manager.process_conversation(
                    user_input=user_input,
                    assistant_response=assistant_response,
                    user_id="default_user",
                    character_name=self.character_name  # Pass the actual character name
                )
                if success:
                    print(f"[ResponseHandler] Mem0自動抽出完了")
                else:
                    print(f"[ResponseHandler] Mem0自動抽出スキップ（短い会話）")
            else:
                print(f"[ResponseHandler] Semantic memory manager not available")
        except Exception as e:
            # Don't let memory processing errors affect the main response flow
            print(f"[ResponseHandler] Mem0処理エラー（無視）: {e}")