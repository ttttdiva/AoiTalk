"""AgentLLMClient のメモリ統合（履歴同期・要約・永続化・cleanup）Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..native_runtime import AgentDefinition as Agent

logger = logging.getLogger(__name__)


class MemoryIntegrationMixin:
    """会話履歴の同期・要約・永続化、セッションメタデータ、リソース cleanup。"""

    async def _sync_history_with_current_session(self) -> None:
        """Load persisted session messages before building prompt context."""
        session_id = self.current_session_id
        if not session_id:
            if self._loaded_history_session_id is not None:
                self.history_manager.clear()
                self._loaded_history_session_id = None
            return

        if session_id == self._loaded_history_session_id:
            return

        self.history_manager.clear()
        # A provider response id is scoped to the loaded session/branch.  Do
        # not let state from a previous session survive when the new session
        # has no persisted provider state.
        self._provider_state = {"previous_response_id": None, "fingerprint": None}

        if not self.memory_manager or not self._memory_enabled:
            self._loaded_history_session_id = session_id
            return

        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        try:
            get_active = getattr(
                self.memory_manager.repository,
                "get_active_branch_messages",
                None,
            )
            messages = await (
                get_active(session_id)
                if callable(get_active)
                else self.memory_manager.repository.get_session_messages(session_id)
            )
        except Exception as e:
            self._loaded_history_session_id = None
            print(f"[AgentLLMClient] Failed to load session history: {e}")
            return

        session = None
        get_session = getattr(
            self.memory_manager.repository,
            "get_session_by_id",
            None,
        )
        if callable(get_session):
            try:
                session = await get_session(session_id, with_messages=False)
            except TypeError:
                session = await get_session(session_id)
        if session is not None:
            if hasattr(self.history_manager, "set_summary"):
                self.history_manager.set_summary(
                    getattr(session, "current_summary", "") or ""
                )
            session_context = getattr(session, "context", None) or {}
            provider_state = (
                session_context.get("llm_provider_state")
                if isinstance(session_context, dict)
                else None
            )
            if isinstance(provider_state, dict):
                self._provider_state = {
                    "previous_response_id": provider_state.get("previous_response_id"),
                    "fingerprint": provider_state.get("fingerprint"),
                }

        max_messages = getattr(self.history_manager, "hard_limit", 100)
        model_messages: list[dict[str, Any]] = []
        for msg in messages[-max_messages:]:
            if msg.role not in {"user", "assistant", "system"}:
                continue
            self.history_manager.add_message(msg.role, msg.content)
            metadata = getattr(msg, "message_metadata", None) or {}
            transcript = metadata.get("model_transcript") if isinstance(metadata, dict) else None
            if isinstance(transcript, list) and transcript:
                first = transcript[0] if isinstance(transcript[0], dict) else {}
                if (
                    model_messages
                    and first.get("role") == "user"
                    and model_messages[-1].get("role") == "user"
                ):
                    model_messages.pop()
                model_messages.extend(
                    dict(item)
                    for item in transcript
                    if isinstance(item, dict)
                    and item.get("role") in {"user", "assistant", "tool"}
                )
            else:
                model_messages.append({"role": msg.role, "content": msg.content})

        if hasattr(self.history_manager, "set_model_messages"):
            self.history_manager.set_model_messages(model_messages)

        self._loaded_history_session_id = session_id
        print(
            f"[AgentLLMClient] Loaded {len(messages)} persisted messages for session: {session_id}"
        )

    def set_session_context(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ):
        """Update session identifiers used for memory logging."""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def _get_memory_metadata(self) -> Dict[str, Any]:
        metadata = self.session_metadata.copy() if self.session_metadata else {}
        transcript = getattr(self, "_last_model_transcript", None) or []
        if transcript:
            metadata["model_transcript"] = [
                dict(message)
                for message in transcript
                if isinstance(message, dict)
            ]
        return metadata

    async def _build_past_conversation_recall(self, user_input: str) -> str:
        """ユーザー入力で過去会話をベクトル検索し、関連する抜粋だけを整形して返す。

        キーワードゲートは使わず、意味的関連度(内部で min_relevance_score=0.3)で
        フィルタ済みの結果だけを注入する。関連が無ければ空文字列を返し、
        いかなる例外でもターンを壊さず "" を返す。
        """
        if not self.memory_manager or not self._memory_enabled:
            return ""
        if not getattr(self.memory_manager.config, "enable_search", True):
            return ""

        try:
            from ...memory.cross_session_memory import get_cross_session_memory

            csm = get_cross_session_memory()
            results = await csm.search_relevant_conversations(
                user_id=self._get_session_user_id(),
                query=user_input,
                current_session_id=self.current_session_id,
                limit=3,
            )
            if not results:
                return ""
            return csm.format_memory_context(results, max_chars=1200)
        except Exception as e:
            logger.debug(f"過去会話の自動注入に失敗(注入をスキップ): {e}")
            return ""

    def check_and_summarize_history(self, history_manager=None) -> None:
        """Check if history needs summarization and start background task.

        Args:
            history_manager: HistoryManager instance to check. Defaults to self.history_manager.
        """
        if history_manager is None:
            history_manager = self.history_manager

        # Threshold: keep context_window_size + buffer
        threshold = self._summarize_threshold

        if len(history_manager.history) > threshold:
            # Create background task
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._summarize_history_task(history_manager))
                self._active_summarization_tasks.add(task)
                task.add_done_callback(self._active_summarization_tasks.discard)

                print(
                    f"[AgentLLMClient] Summarization task started (History: {len(history_manager.history)})"
                )
            except RuntimeError:
                # No running loop (shouldn't happen in async context usually)
                pass

    async def _summarize_history_task(self, history_manager):
        """Background task to summarize old history.

        Args:
            history_manager: HistoryManager instance to summarize.
        """
        try:
            # Snapshot first.  Deleting before the LLM succeeds can permanently
            # lose the only copy of the conversation when summarization fails.
            messages_to_summarize = list(
                history_manager.history[: self._summarize_batch_size]
            )
            if not messages_to_summarize:
                return

            print(
                f"[AgentLLMClient] Summarizing {len(messages_to_summarize)} messages..."
            )

            # Get current summary
            current_summary = history_manager.summary

            # Generate new summary
            new_summary = await self._generate_summary(
                messages_to_summarize, current_summary
            )

            # Commit the checkpoint only when the prefix is still unchanged.
            # New messages may have arrived while the summary was generated.
            current_prefix = history_manager.history[: len(messages_to_summarize)]
            if current_prefix != messages_to_summarize:
                logger.info("履歴要約を保留: 要約中に履歴の先頭が変更されました")
                return
            history_manager.update_summary(new_summary)
            history_manager.history = history_manager.history[len(messages_to_summarize) :]
            if hasattr(history_manager, "set_model_messages"):
                history_manager.set_model_messages(history_manager.get_all())
            if self.current_session_id and self.memory_manager:
                try:
                    await self.memory_manager.repository.update_session_summary(
                        self.current_session_id,
                        history_manager.summary,
                    )
                except Exception:
                    logger.warning("要約checkpointの永続化に失敗しました", exc_info=True)
            print(
                f"[AgentLLMClient] Summary updated. New history length: {len(history_manager.history)}"
            )

        except Exception as e:
            print(f"[AgentLLMClient] Summarization failed: {e}")
            import traceback

            traceback.print_exc()

    async def _generate_summary(
        self, messages: List[Dict[str, Any]], current_summary: str
    ) -> str:
        """Generate summary using the LLM."""

        # Format messages
        conversation_text = ""
        for msg in messages:
            role = "User" if msg["role"] == "user" else "Assistant"
            conversation_text += f"{role}: {msg['content']}\n"

        # Build prompt
        prompt = f"""
Summarize the conversation below while preserving important facts, decisions, and open items.
Keep the summary concise and useful for continuing the conversation later.

Current summary:
{current_summary if current_summary else "None"}

New conversation:
{conversation_text}

Updated summary:
"""
        try:
            summary_agent = Agent(
                name=f"{self.agent.name}Summary",
                instructions=self.agent.instructions,
                model=self.agent.model,
                tools=[],
                model_settings=self.agent.model_settings,
            )
            result = await self._turn_runner.run(summary_agent, prompt)
            return result.final_output

        except Exception as e:
            print(f"[AgentLLMClient] Error generating summary: {e}")
            raise e

    def clear_history(self):
        """Clear conversation history"""
        self.history_manager.clear()

    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history

        Returns:
            List of conversation messages
        """
        return self.history_manager.get_all()

    async def cleanup(self):
        """Clean up resources, especially MCP connections and memory manager"""
        # Clean up memory manager
        if self.memory_manager:
            try:
                await self.memory_manager.cleanup()
                print("[AgentLLMClient] Memory manager cleaned up")
            except Exception as e:
                print(f"[AgentLLMClient] Error during memory cleanup: {e}")

    def _register_cleanup(self):
        """Register cleanup handler for process exit"""
        import atexit
        import os
        import signal

        def sync_cleanup():
            """Synchronous cleanup wrapper"""
            if not self._cleanup_registered:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.cleanup())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

        # Register cleanup on exit
        atexit.register(sync_cleanup)

        # Also handle signals
        def signal_handler(signum, frame):
            sync_cleanup()
            # Re-raise the signal to let default handler run
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
        except:
            pass  # Ignore errors in signal registration

        self._cleanup_registered = True
